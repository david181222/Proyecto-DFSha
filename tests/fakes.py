"""Dobles de prueba compartidos: FakeEtcd y la API pública del MetaServer lista para probar.

Se importan con `from fakes import ...` (pytest pone la carpeta tests/ en sys.path).
"""

import asyncio
import base64
import operator
import time
from contextlib import contextmanager
from typing import Any

from fastapi.testclient import TestClient

from dfs_common.config import Settings
from dfs_common.security import create_jwt
from metaserver.deps import get_etcd, get_jwt_secret, get_settings
from metaserver.etcd import KeyValue, LeaseNotFound, TxnResult, _encode, lock_key, prefix_end
from metaserver.public_api import app as metaserver_public

# 32 bytes, como los secretos que genera la tarea 04 (PyJWT avisa si la clave HS256 es corta).
JWT_SECRET = b"j" * 32

_FIELDS = {"CREATE": "create_revision", "MOD": "mod_revision", "VERSION": "version"}
_RESULTS = {"EQUAL": operator.eq, "NOT_EQUAL": operator.ne, "GREATER": operator.gt, "LESS": operator.lt}


def _unb64(text: str) -> bytes:
    return base64.b64decode(text)


class FakeEtcd:
    """etcd en memoria con la interfaz de metaserver.etcd.Etcd: lo que usan las tareas 07, 08 y 11.

    Imita lo que importa de etcd: una revisión global que sube con cada escritura;
    create_revision, mod_revision y version por clave; leases que al revocarse borran sus
    claves; transacciones atómicas con comparación de rango; y el lock, con espera o sin ella.
    Las comparaciones y operaciones de txn llegan como las arman compare(), op_put(),
    op_delete() y op_range(), en el formato de la pasarela (base64), y aquí se decodifican.

    No lleva la cuenta del tiempo: un lease no vence solo. Para simular que venció, una
    prueba lo revoca.
    """

    LOCK_POLL_S = 0.01  # cada cuánto revisa la fila un intento de lock que espera

    def __init__(self) -> None:
        self.kvs: dict[str, KeyValue] = {}
        self.revision = 1  # como un etcd recién creado
        self.leases: set[int] = set()
        self.ttls: dict[int, int] = {}  # TTL con que se pidió cada lease
        self.keepalives: list[int] = []  # el lease de cada llamada a lease_keepalive, en orden
        self.next_lease = 0x694D8A1C2E3B4F00
        self.closed = False

    # --- Para preparar y revisar las pruebas ---

    def seed(self, key: str, value: dict | bytes, *, lease: int = 0) -> None:
        """Escribe una clave directo, como un put (para preparar el estado de una prueba)."""
        self.revision += 1
        self._put(key, value, lease)

    def value(self, key: str) -> Any:
        """El JSON guardado en key, o None si no existe."""
        kv = self.kvs.get(key)
        return kv.json() if kv else None

    def keys(self, prefix: str = "") -> list[str]:
        return sorted(key for key in self.kvs if key.startswith(prefix))

    # --- Internos ---

    def _put(self, key: str, value: dict | bytes, lease: int) -> None:
        if lease and lease not in self.leases:
            raise LeaseNotFound("etcdserver: requested lease not found")
        old = self.kvs.get(key)
        self.kvs[key] = KeyValue(
            key=key,
            value=_encode(value),
            create_revision=old.create_revision if old else self.revision,
            mod_revision=self.revision,
            version=old.version + 1 if old else 1,
            lease=lease,
        )

    def _range(self, key: bytes, range_end: bytes | None) -> list[KeyValue]:
        if range_end is None:
            kv = self.kvs.get(key.decode())
            return [kv] if kv else []
        # range_end "\0" quiere decir "hasta la última clave".
        return [
            kv
            for name, kv in sorted(self.kvs.items())
            if key <= name.encode() and (range_end == b"\0" or name.encode() < range_end)
        ]

    def _delete(self, key: bytes, range_end: bytes | None) -> int:
        doomed = self._range(key, range_end)
        for kv in doomed:
            del self.kvs[kv.key]
        return len(doomed)

    def _holds(self, condition: dict) -> bool:
        """Una comparación de txn, como la evalúa etcd: con rango, debe cumplirse para cada clave
        del rango; una clave que no existe (o un rango vacío) compara con revisiones y versión 0."""
        key = _unb64(condition["key"])
        range_end = _unb64(condition["range_end"]) if "range_end" in condition else None
        target, check = condition["target"], _RESULTS[condition["result"]]
        kvs = self._range(key, range_end)
        if target == "VALUE":
            expected = _unb64(condition["value"])
            return bool(kvs) and all(check(kv.value, expected) for kv in kvs)
        field = _FIELDS[target]
        actual = [getattr(kv, field) for kv in kvs] or [0]
        return all(check(value, condition[field]) for value in actual)

    def _apply(self, op: dict) -> list[KeyValue] | int | None:
        if "request_put" in op:
            body = op["request_put"]
            self._put(_unb64(body["key"]).decode(), _unb64(body["value"]), body.get("lease", 0))
            return None
        if "request_delete_range" in op:
            body = op["request_delete_range"]
            return self._delete(_unb64(body["key"]), _unb64(body["range_end"]) if "range_end" in body else None)
        body = op["request_range"]
        return self._range(_unb64(body["key"]), _unb64(body["range_end"]) if "range_end" in body else None)

    # --- Interfaz de Etcd ---

    async def aclose(self) -> None:
        self.closed = True

    async def get(self, key: str) -> KeyValue | None:
        return self.kvs.get(key)

    async def get_prefix(self, prefix: str, *, keys_only: bool = False, limit: int = 0) -> list[KeyValue]:
        kvs = self._range(prefix.encode(), prefix_end(prefix))
        if keys_only:
            kvs = [KeyValue(kv.key, b"", kv.create_revision, kv.mod_revision, kv.version, kv.lease) for kv in kvs]
        return kvs[:limit] if limit else kvs

    async def put(self, key: str, value: dict | bytes, *, lease: int = 0) -> None:
        self.seed(key, value, lease=lease)

    async def delete(self, key: str) -> int:
        deleted = self._delete(key.encode(), None)
        self.revision += 1 if deleted else 0
        return deleted

    async def txn(self, compare: list[dict], success: list[dict], failure: list[dict]) -> TxnResult:
        succeeded = all(self._holds(condition) for condition in compare)
        ops = success if succeeded else failure
        # Como en etcd: si un put usa un lease inexistente, la transacción entera falla sin escribir.
        for op in ops:
            lease = op.get("request_put", {}).get("lease", 0)
            if lease and lease not in self.leases:
                raise LeaseNotFound("etcdserver: requested lease not found")
        if any("request_range" not in op for op in ops):
            self.revision += 1  # todas las escrituras de una transacción comparten una revisión
        return TxnResult(succeeded=succeeded, responses=[self._apply(op) for op in ops])

    async def lease_grant(self, ttl_s: int) -> int:
        self.next_lease += 1
        self.leases.add(self.next_lease)
        self.ttls[self.next_lease] = ttl_s
        return self.next_lease

    async def lease_keepalive(self, lease_id: int) -> int:
        """Anota la llamada en keepalives y devuelve el TTL con que se pidió el lease."""
        self.keepalives.append(lease_id)
        if lease_id not in self.leases:
            raise LeaseNotFound(f"el lease {lease_id:x} ya no existe")
        return self.ttls[lease_id]

    async def lease_revoke(self, lease_id: int) -> None:
        if lease_id not in self.leases:
            raise LeaseNotFound("etcdserver: requested lease not found")
        self.leases.remove(lease_id)
        attached = [key for key, kv in self.kvs.items() if kv.lease == lease_id]
        for key in attached:
            del self.kvs[key]
        self.revision += 1 if attached else 0

    async def lease_keys(self, lease_id: int) -> list[str] | None:
        if lease_id not in self.leases:
            return None
        return sorted(key for key, kv in self.kvs.items() if kv.lease == lease_id)

    async def lock(self, name: str, lease_id: int, *, wait_s: float) -> str | None:
        """La misma fila que Etcd.lock: la clave {name}/{lease en hex}, y entra la más antigua.

        Revisa la fila cada LOCK_POLL_S hasta que su clave es la más antigua (devuelve la
        clave), hasta que se agota wait_s (borra su clave y devuelve None, como etcd cuando se
        corta la espera) o hasta que su clave desaparece porque el lease se revocó (LeaseNotFound,
        como "mutex: session is expired"; etcd lo responde recién cuando se suelta el lock).
        Con wait_s == 0 revisa una sola vez: es el mismo algoritmo de Etcd.lock sin espera.
        """
        key = lock_key(name, lease_id)
        await self.put(key, b"", lease=lease_id)
        deadline = time.monotonic() + wait_s
        while True:
            waiting = await self.get_prefix(f"{name}/", keys_only=True)
            if key not in [kv.key for kv in waiting]:
                raise LeaseNotFound("mutex: session is expired")
            if min(waiting, key=lambda kv: kv.create_revision).key == key:
                return key
            if time.monotonic() >= deadline:
                await self.delete(key)
                return None
            await asyncio.sleep(self.LOCK_POLL_S)


# --- API pública del MetaServer con FakeEtcd ---


def bearer(user: str = "ana", *, admin: bool = False, ttl_s: int = 3600, now: int | None = None) -> dict:
    """Encabezado Authorization con un JWT firmado con la clave de pruebas."""
    return {"Authorization": f"Bearer {create_jwt(user, admin, JWT_SECRET, ttl_s, now=now)}"}


@contextmanager
def public_client(etcd: FakeEtcd | None = None, **settings: Any):
    """TestClient de la API pública del MetaServer con FakeEtcd, la clave de pruebas y Settings.

    No corre el lifespan (TestClient fuera de un with): las dependencias se reemplazan con
    app.dependency_overrides y se restauran al salir. settings cambia valores de Settings,
    por ejemplo jwt_ttl_s=1234.
    """
    etcd = etcd if etcd is not None else FakeEtcd()
    config = Settings(_env_file=None, **settings)
    overrides = metaserver_public.dependency_overrides
    overrides.update({get_etcd: lambda: etcd, get_settings: lambda: config, get_jwt_secret: lambda: JWT_SECRET})
    try:
        yield TestClient(metaserver_public)
    finally:
        overrides.clear()


def add_dir(etcd: FakeEtcd, user: str, path: str, *, created_at: int = 1793178900000) -> None:
    etcd.seed(f"/ns/{user}{path}", {"type": "dir", "created_at": created_at})


def add_file(
    etcd: FakeEtcd,
    user: str,
    path: str,
    *,
    file_id: str = "3f0c9a7e-1b2d-4c5e-8f9a-0b1c2d3e4f50",
    version: int = 4,
    size: int = 20971520,
    created_at: int = 1793628090000,
) -> None:
    """Un archivo ya publicado: su entrada en /ns y el manifiesto de su versión actual (sección 8).

    Las subidas son de la tarea 12; aquí se escriben las dos claves directo.
    """
    etcd.seed(f"/ns/{user}{path}", {"type": "file", "file_id": file_id, "version": version})
    etcd.seed(
        f"/files/{file_id}/v/{version}",
        {
            "user": user,
            "size": size,
            "block_size": 8388608,
            "hashes": ["6ce5a55b446b43e4e935c44695a9a09f161e8e87d1ff2359adebc5f9ee5ee5cf"],
            "file_sha256": "247a8174ec0c61e4f6c2f0299b27648c9ef3dc8482f7083d01c8a389976a6a76",
            "created_at": created_at,
            "replaced_at": None,
        },
    )
