"""Cliente de etcd del MetaServer, por la pasarela HTTP/JSON de etcd v3 (DA-01).

Solo el MetaServer habla con etcd. No usamos un cliente de etcd para Python: cada
operación es un POST con JSON a /v3/kv, /v3/lease, /v3/lock o /v3/election, sobre
httpx.AsyncClient. En AWS va por mTLS con el certificado del nodo (docs/contratos.md,
sección 8); el contexto TLS lo arma quien crea el cliente.

Lo propio de la pasarela se resuelve aquí y en ningún otro lado (comprobado en etcd 3.5.35):

- claves y valores viajan en base64;
- los enteros de 64 bits llegan como texto ("revision": "12"); se envían como número;
- los campos en cero o en falso no llegan: kvs, succeeded, count, deleted, lease, TTL...;
- un error llega como {"error", "code", "message"}, con el código gRPC en code;
- los ID de lease van en decimal. Nuestra API y las claves de lock los muestran en
  hexadecimal: la conversión está solo en lease_hex y lease_from_hex.
"""

import base64
import json
import logging
import re
import ssl
from dataclasses import dataclass
from typing import Any

import httpx

from dfs_common.errors import DFSError, ErrorCode

logger = logging.getLogger("dfsha")

Key = str | bytes  # nuestras claves son texto UTF-8; prefix_end devuelve bytes
Value = dict | bytes  # un dict se guarda como JSON compacto en UTF-8 (sección 8)

# Errores gRPC con los que se prueba el siguiente nodo de etcd: sin líder, sin mayoría
# o sin respuesta a tiempo (codes.Unavailable y codes.DeadlineExceeded).
GRPC_UNAVAILABLE = 14
GRPC_DEADLINE_EXCEEDED = 4

# Errores de httpx en los que la petición no llegó a enviarse: probar otro nodo no tiene riesgo.
_NOT_SENT = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)

# Mensajes de etcd que se reconocen por su texto. El de lease inexistente llega con
# code 5 en /v3/kv y /v3/lease, pero con code 2 en /v3/lock y /v3/election.
LEASE_NOT_FOUND = "requested lease not found"
ELECTION_NO_LEADER = "election: no leader"
# Un nodo sin líder rechaza la petición antes de proponerla: es seguro reenviarla, aun una txn.
# Los demás Unavailable y DeadlineExceeded ("request timed out", "leader changed"...) no lo son.
ETCD_NO_LEADER = "etcdserver: no leader"


class EtcdError(Exception):
    """etcd respondió un error que no es una caída del nodo; el mensaje es el de etcd.

    Es interna del MetaServer, no un error de la API: no lleva código de error. Si
    nadie la atrapa, el manejador de errores inesperados responde 500 E_INTERNAL.
    """


class LeaseNotFound(EtcdError):
    """El lease ya no existe: venció, se revocó o nunca existió.

    Es interna del MetaServer, como EtcdError: no llega al cliente. Las rutas la
    convierten en un DFSError (409 E_LOCK_LOST o 404 E_NOT_FOUND, tareas 11 y 13).
    """


class ResponseLost(EtcdError):
    """etcd pudo haber recibido la petición, pero no se sabe si la aplicó.

    Pasa cuando no llegó la respuesta (por ejemplo, ReadTimeout) o cuando etcd respondió
    Unavailable o DeadlineExceeded sin decir que la rechazó. Solo sale de Etcd._call con
    resend=False; txn la convierte en 503 E_UNAVAILABLE y lock con espera, en None.
    """


@dataclass(frozen=True)
class KeyValue:
    """Una clave leída de etcd. value son los bytes guardados; json() los interpreta."""

    key: str
    value: bytes  # b"" si se leyó con keys_only
    create_revision: int  # revisión en que se creó la clave
    mod_revision: int  # revisión de su último cambio
    version: int  # cuántas veces cambió desde que se creó
    lease: int  # lease al que está atada; 0 = ninguno

    def json(self) -> Any:
        return json.loads(self.value)


@dataclass(frozen=True)
class TxnResult:
    """Resultado de una transacción.

    succeeded: si se cumplieron todas las comparaciones (corrió success y no failure).
    responses: una por operación de la rama que corrió, en orden: la lista de KeyValue
    de un op_range, el número de claves borradas de un op_delete y None de un op_put.
    """

    succeeded: bool
    responses: list[list[KeyValue] | int | None]


@dataclass(frozen=True)
class LeaderKey:
    """La candidatura que ganó una elección: la devuelve campaign y la recibe resign."""

    name: str
    key: str  # {name}/{lease en hex}, la misma que muestra leader()
    rev: int
    lease: int


# --- Codificación de la pasarela ---


def _b64(data: Key) -> str:
    if isinstance(data, str):
        data = data.encode()
    return base64.b64encode(data).decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.b64decode(text)


def _encode(value: Value) -> bytes:
    """Un dict se guarda como JSON compacto en UTF-8; los bytes, tal cual."""
    if isinstance(value, dict):
        return json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode()
    return value


def _kv(raw: dict) -> KeyValue:
    """Convierte un kv de la pasarela: base64 a bytes, enteros en texto a int y campos ausentes a 0."""
    return KeyValue(
        key=_unb64(raw["key"]).decode(),
        value=_unb64(raw.get("value", "")),
        create_revision=int(raw.get("create_revision", 0)),
        mod_revision=int(raw.get("mod_revision", 0)),
        version=int(raw.get("version", 0)),
        lease=int(raw.get("lease", 0)),
    )


def _range(key: Key, range_end: Key | None) -> dict:
    """Clave y, si hay, fin de rango, como los espera la pasarela."""
    body = {"key": _b64(key)}
    if range_end is not None:
        body["range_end"] = _b64(range_end)
    return body


def _grpc_error(data: Any) -> tuple[int | None, str] | None:
    """El error que trae una respuesta de la pasarela, como (código gRPC, mensaje), o None.

    Casi siempre llega como {"error", "code", "message"}. En un endpoint de streaming
    (keepalive), un error de etcd llega como {"error": {"grpc_code", "message", ...}}.
    """
    if not isinstance(data, dict):
        return None
    error = data.get("error")
    if error is None:
        return None
    if isinstance(error, dict):
        return error.get("grpc_code"), error.get("message", "")
    return data.get("code"), data.get("message", error)


# --- Funciones auxiliares públicas ---


def prefix_end(prefix: Key) -> bytes:
    """Fin de rango de un prefijo: el prefijo con su último byte + 1 ("/ns/ana/" → b"/ns/ana0").

    El rango [prefix, prefix_end(prefix)) tiene justo las claves que empiezan por prefix.
    Un byte 0xff no se puede subir: se quita y se sube el anterior, como hace
    clientv3.GetPrefixRangeEnd. Nuestras claves son texto UTF-8, que nunca tiene 0xff.
    """
    data = (prefix.encode() if isinstance(prefix, str) else prefix).rstrip(b"\xff")
    if not data:
        return b"\0"  # para etcd, range_end "\0" quiere decir "hasta la última clave"
    return data[:-1] + bytes([data[-1] + 1])


# Exactamente lo que produce lease_hex: hexadecimal en minúsculas, sin ceros a la izquierda y sin el 0.
_LEASE_HEX = re.compile(r"[1-9a-f][0-9a-f]{0,15}")


def lease_hex(lease_id: int) -> str:
    """ID de lease como lo muestran nuestra API (lock_id, lease_id) y la clave del lock.

    etcd entrega IDs positivos de 63 bits; el 0 quiere decir "sin lease".
    """
    return format(lease_id, "x")


def lease_from_hex(text: str) -> int:
    """Lo contrario de lease_hex. Un texto que lease_hex no pudo producir no nombra ningún lease."""
    if not _LEASE_HEX.fullmatch(text):
        raise LeaseNotFound(f"lease inválido: {text!r}")
    return int(text, 16)


def lock_key(name: str, lease_id: int) -> str:
    """Clave de un intento de lock: {name}/{lease en hex}, el mismo formato de /v3/lock/lock."""
    return f"{name}/{lease_hex(lease_id)}"


_COMPARE_FIELDS = {"CREATE": "create_revision", "MOD": "mod_revision", "VERSION": "version", "VALUE": "value"}


def compare(key: Key, target: str, result: str, value: int | Value, *, range_end: Key | None = None) -> dict:
    """Una comparación para txn.

    target: CREATE, MOD o VERSION, con un entero; o VALUE, con un dict o bytes.
    result: EQUAL, NOT_EQUAL, GREATER o LESS.
    Con range_end se compara cada clave del rango: compare(p, "CREATE", "EQUAL", 0,
    range_end=prefix_end(p)) es verdadera solo si no hay claves bajo p (rmdir, sección 8).
    """
    field = _COMPARE_FIELDS[target]
    return {
        **_range(key, range_end),
        "target": target,
        "result": result,
        field: _b64(_encode(value)) if target == "VALUE" else value,
    }


def op_put(key: Key, value: Value, *, lease: int = 0) -> dict:
    return {"request_put": {"key": _b64(key), "value": _b64(_encode(value)), "lease": lease}}


def op_delete(key: Key, *, range_end: Key | None = None) -> dict:
    return {"request_delete_range": _range(key, range_end)}


def op_range(key: Key, *, range_end: Key | None = None) -> dict:
    return {"request_range": _range(key, range_end)}


def _op_result(response: dict) -> list[KeyValue] | int | None:
    if "response_range" in response:
        return [_kv(raw) for raw in response["response_range"].get("kvs", [])]
    if "response_delete_range" in response:
        return int(response["response_delete_range"].get("deleted", 0))
    return None  # response_put


# --- Cliente ---


class Etcd:
    """Cliente asíncrono de etcd para el MetaServer."""

    def __init__(
        self,
        endpoints: list[str],
        ssl_context: ssl.SSLContext | None = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        """endpoints: los de DFS_ETCD_ENDPOINTS, en el orden en que se prueban.

        ssl_context: client_ssl_context(ca, cert, key) con el certificado del nodo (tarea 07).
        Sin él, httpx usa su verificación por defecto; los endpoints http:// van sin TLS y
        solo existen para la prueba de integración.
        transport: solo para las pruebas (httpx.MockTransport).
        """
        self._endpoints = [endpoint.rstrip("/") for endpoint in endpoints]
        self._current = 0  # índice del último endpoint que respondió
        # Timeouts por defecto de httpx; solo lock y campaign usan otros.
        self._client = httpx.AsyncClient(verify=ssl_context or True, transport=transport)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "Etcd":
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    async def _call(
        self, path: str, body: dict, *, timeout: Any = httpx.USE_CLIENT_DEFAULT, resend: bool = True
    ) -> dict:
        """POST a la pasarela, probando los endpoints desde el último que respondió.

        Pasa al siguiente si la petición no llegó a enviarse (sin conexión) o si etcd responde
        Unavailable o DeadlineExceeded; si ninguno responde, 503 E_UNAVAILABLE. Cualquier otro
        error de etcd sale como LeaseNotFound o EtcdError.

        Hay casos en que etcd pudo haber recibido la petición y no se sabe si la aplicó: no
        llegó la respuesta (por ejemplo, ReadTimeout), o etcd respondió Unavailable o
        DeadlineExceeded sin decir "no leader". Con resend=True se reenvía igual al siguiente:
        sirve para las lecturas y para lo que repetido da lo mismo. Con resend=False (txn, y
        lock con espera, donde el timeout es la espera) sale ResponseLost, y la siguiente
        llamada empieza por otro nodo.
        """
        count = len(self._endpoints)
        for step in range(count):
            index = (self._current + step) % count
            endpoint = self._endpoints[index]
            try:
                response = await self._client.post(endpoint + path, json=body, timeout=timeout)
            except _NOT_SENT as exc:
                logger.warning("etcd %s no respondió a %s: %r", endpoint, path, exc)
                continue
            except httpx.TransportError as exc:
                logger.warning("etcd %s no terminó de responder a %s: %r", endpoint, path, exc)
                if not resend:
                    self._current = (index + 1) % count
                    raise ResponseLost(f"etcd {endpoint} no terminó de responder a {path}") from exc
                continue
            try:
                data = response.json()
            except ValueError:
                data = None
            error = _grpc_error(data)
            if error is not None:
                code, message = error
                if code in (GRPC_UNAVAILABLE, GRPC_DEADLINE_EXCEEDED):
                    logger.warning("etcd %s no está disponible para %s: %s", endpoint, path, message)
                    if not resend and ETCD_NO_LEADER not in message:
                        self._current = (index + 1) % count
                        raise ResponseLost(message)
                    continue
                self._current = index  # respondió, aunque sea con un error
                if LEASE_NOT_FOUND in message:
                    raise LeaseNotFound(message)
                raise EtcdError(message)
            self._current = index
            # Lo que no es un 2xx con el JSON de la pasarela nunca se toma como respuesta válida:
            # un cuerpo vacío se leería como "la clave no existe" o "la transacción falló".
            if not response.is_success or not isinstance(data, dict):
                raise EtcdError(f"respuesta inesperada de etcd a {path}: HTTP {response.status_code}")
            return data
        raise DFSError(ErrorCode.E_UNAVAILABLE, "ningún nodo de etcd respondió")

    # --- KV ---

    async def get(self, key: Key) -> KeyValue | None:
        data = await self._call("/v3/kv/range", _range(key, None))
        kvs = data.get("kvs", [])
        return _kv(kvs[0]) if kvs else None

    async def get_prefix(self, prefix: Key, *, keys_only: bool = False, limit: int = 0) -> list[KeyValue]:
        """Claves que empiezan por prefix, en orden de clave. keys_only: sin valores; limit 0: todas."""
        body = {**_range(prefix, prefix_end(prefix)), "keys_only": keys_only, "limit": limit}
        data = await self._call("/v3/kv/range", body)
        return [_kv(raw) for raw in data.get("kvs", [])]

    async def put(self, key: Key, value: Value, *, lease: int = 0) -> None:
        """Escribe la clave; con lease, queda atada a él y desaparece cuando el lease vence."""
        await self._call("/v3/kv/put", {"key": _b64(key), "value": _b64(_encode(value)), "lease": lease})

    async def delete(self, key: Key) -> int:
        """Borra la clave. Devuelve cuántas borró: 0 si no existía."""
        data = await self._call("/v3/kv/deleterange", _range(key, None))
        return int(data.get("deleted", 0))

    async def txn(self, compare: list[dict], success: list[dict], failure: list[dict]) -> TxnResult:
        """Transacción: si se cumplen todas las comparaciones corre success; si no, failure.

        Las comparaciones se arman con compare(); las operaciones, con op_put, op_delete y op_range.

        Solo se reenvía a otro nodo si es seguro que etcd no la aplicó (no llegó a enviarse, o
        etcd respondió "no leader"). Si no, repetida podría tomar la otra rama (por ejemplo, un
        mkdir que sí creó el directorio respondería 409): se responde 503 E_UNAVAILABLE, que es
        reintentable (ResponseLost).
        """
        body = {"compare": compare, "success": success, "failure": failure}
        try:
            data = await self._call("/v3/kv/txn", body, resend=False)
        except ResponseLost:
            message = "etcd no respondió a una transacción; pudo haberse aplicado"
            raise DFSError(ErrorCode.E_UNAVAILABLE, message) from None
        return TxnResult(
            succeeded=data.get("succeeded", False),
            responses=[_op_result(response) for response in data.get("responses", [])],
        )

    # --- Lease ---

    async def lease_grant(self, ttl_s: int) -> int:
        """Crea un lease de ttl_s segundos y devuelve su ID (lease_hex para mostrarlo).

        Se reenvía si no llega la respuesta: en el peor caso queda un lease sin claves que vence solo.
        """
        data = await self._call("/v3/lease/grant", {"TTL": ttl_s})
        return int(data["ID"])

    async def lease_keepalive(self, lease_id: int) -> int:
        """Renueva el lease y devuelve su TTL en segundos; LeaseNotFound si ya no existe.

        La respuesta viene dentro de result. Para un lease que ya no existe etcd no da
        error: responde sin TTL, que es lo mismo que TTL 0.
        """
        data = await self._call("/v3/lease/keepalive", {"ID": lease_id})
        ttl = int(data.get("result", {}).get("TTL", 0))
        if ttl == 0:
            raise LeaseNotFound(f"el lease {lease_hex(lease_id)} ya no existe")
        return ttl

    async def lease_revoke(self, lease_id: int) -> None:
        """Revoca el lease, y etcd borra las claves atadas a él; LeaseNotFound si ya no existe."""
        await self._call("/v3/lease/revoke", {"ID": lease_id})

    # --- Lock (DA-08) ---

    async def lock(self, name: str, lease_id: int, *, wait_s: float) -> str | None:
        """Toma el lock name con el lease del intento. Devuelve la clave del lock, o None si no lo obtuvo.

        Por los dos caminos la clave es {name}/{lease en hex} (lock_key), así que comparten la fila:

        - wait_s > 0: el servicio de lock de etcd pone el intento en la fila y responde
          cuando le toca. Si wait_s se agota antes, o la espera se corta (ResponseLost),
          None. Quien llama revoca el lease para sacarlo de la fila (tarea 11); eso también
          borra la clave si el lock llegó justo cuando se agotaba la espera.
        - wait_s == 0: intento sin esperar. Pone su clave atada al lease y mira si bajo
          {name}/ hay otra más antigua (menor create_revision): la del que tiene el lock o
          va antes en la fila. Si la hay, borra la suya y devuelve None.
        """
        if wait_s == 0:
            return await self._try_lock(name, lease_id)
        body = {"name": _b64(name), "lease": lease_id}
        try:
            data = await self._call("/v3/lock/lock", body, timeout=wait_s, resend=False)
        except ResponseLost:
            return None
        return _unb64(data["key"]).decode()

    async def _try_lock(self, name: str, lease_id: int) -> str | None:
        key = lock_key(name, lease_id)
        await self.put(key, b"", lease=lease_id)
        waiting = await self.get_prefix(f"{name}/", keys_only=True)
        first = min(waiting, key=lambda kv: kv.create_revision, default=None)
        if first is not None and first.key == key:
            return key
        await self.delete(key)
        return None

    async def unlock(self, key: str) -> None:
        """Suelta el lock borrando su clave; entra el siguiente de la fila."""
        await self._call("/v3/lock/unlock", {"key": _b64(key)})

    # --- Elección (DA-12) ---

    async def campaign(self, name: str, lease_id: int, value: Value) -> LeaderKey:
        """Se presenta a la elección name y espera, sin timeout, hasta ganarla.

        value identifica al candidato: es lo que muestra leader(). La tarea 19 la corre
        en segundo plano. Si el lease vence, el candidato sale de la fila.
        """
        body = {"name": _b64(name), "lease": lease_id, "value": _b64(_encode(value))}
        data = await self._call("/v3/election/campaign", body, timeout=None)
        leader = data["leader"]
        return LeaderKey(
            name=_unb64(leader["name"]).decode(),
            key=_unb64(leader["key"]).decode(),
            rev=int(leader["rev"]),
            lease=int(leader["lease"]),
        )

    async def leader(self, name: str) -> KeyValue | None:
        """El líder actual de la elección name, con el value con que se presentó; None si no hay."""
        try:
            data = await self._call("/v3/election/leader", {"name": _b64(name)})
        except EtcdError as error:
            if str(error) == ELECTION_NO_LEADER:
                return None
            raise
        return _kv(data["kv"])

    async def resign(self, leader_key: LeaderKey) -> None:
        """Deja de ser líder: borra su clave y gana el siguiente candidato de la fila."""
        leader = {
            "name": _b64(leader_key.name),
            "key": _b64(leader_key.key),
            "rev": leader_key.rev,
            "lease": leader_key.lease,
        }
        await self._call("/v3/election/resign", {"leader": leader})
