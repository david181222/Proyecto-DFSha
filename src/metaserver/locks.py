"""Locks de escritura (DA-08): pedir, renovar y liberar el lock de un archivo (tarea 11).

Cada intento de lock usa su propio lease de etcd, de DFS_LOCK_LEASE_S. lock_id es el ID
de ese lease en hexadecimal (lease_hex), y la clave del intento en la fila del servicio de
lock de etcd es /locks/{user}{path}/{lock_id} (docs/contratos.md, sección 8). Por eso no
hace falta ninguna clave más para saber de quién es un lock: resolve_lock le pide a etcd
las claves atadas al lease (lease_keys) y lee el usuario y la ruta en la de lock.

Decisiones de la tarea 11. No cambian el contrato: precisan casos que ya cubre.

1. Un lease sin clave de lock es un lock que no existe. PUT responde 409 E_LOCK_LOST y
   DELETE 200, sin renovar ni revocar ese lease. Es una protección: con el ID de otro
   lease, como el de los latidos de un DataNode, un usuario no puede revocarlo ni
   mantenerlo vivo.
2. Si el lease de un intento vence mientras espera en la fila, es una espera que se agotó:
   423 E_LOCKED. Si en una vuelta de la renovación etcd no responde, se anota en el log y
   se intenta de nuevo en la siguiente.
3. Si falla la revocación del lease de un intento que no obtuvo el lock, igual responde
   423: el lease vence solo en DFS_LOCK_LEASE_S (como en rm, sección 11, punto 6).
4. lease_s es DFS_LOCK_LEASE_S tanto al tomar el lock (POST) como al renovarlo (PUT).

Si el cliente se va mientras espera en la fila (por ejemplo, cerró la conexión), el
MetaServer sigue esperando por él. Si después el lock se obtiene, nadie lo renueva y vence
solo en DFS_LOCK_LEASE_S. Es aceptable.
"""

import asyncio
import logging
from dataclasses import dataclass

from dfs_common.errors import DFSError, ErrorCode
from metaserver.etcd import Etcd, EtcdError, LeaseNotFound, lease_from_hex, lease_hex
from metaserver.keys import lock_name, ns_key

logger = logging.getLogger("dfsha")

_LOCKS_PREFIX = "/locks/"  # lock_name(user, path) = /locks/{user}{path} (metaserver.keys)


@dataclass(frozen=True)
class LockInfo:
    """Un lock que existe: su clave en etcd y el usuario y la ruta que bloquea."""

    key: str  # /locks/{user}{path}/{lock_id}
    user: str
    path: str


def parse_lock_key(key: str, lock_id: str) -> LockInfo | None:
    """Lee el usuario y la ruta de la clave de lock de lock_id: /locks/{user}{path}/{lock_id}.

    Devuelve None si key no es esa clave: un lease también puede tener otras atadas, como
    /uploads/{upload_id} (tarea 12) o /nodes/dn/{node_id} (latidos de un DataNode).
    El usuario no tiene "/" (USER_PATTERN), así que llega hasta la primera "/".
    """
    suffix = f"/{lock_id}"
    if not (key.startswith(_LOCKS_PREFIX) and key.endswith(suffix)):
        return None
    user, slash, rest = key[len(_LOCKS_PREFIX) : -len(suffix)].partition("/")
    if not (user and slash and rest):
        return None
    return LockInfo(key=key, user=user, path=f"/{rest}")


async def find_lock(etcd: Etcd, lock_id: str) -> LockInfo | None:
    """El lock que nombra lock_id, sea de quien sea; None si no existe.

    No existe si lock_id está mal formado (sección 11, punto 3), si su lease ya no existe o
    si el lease no tiene clave de lock (decisión 1).
    """
    try:
        lease_id = lease_from_hex(lock_id)
    except LeaseNotFound:
        return None
    for key in await etcd.lease_keys(lease_id) or []:
        info = parse_lock_key(key, lock_id)
        if info is not None:
            return info
    return None


async def resolve_lock(etcd: Etcd, lock_id: str, user: str) -> LockInfo:
    """El lock que nombra lock_id, si existe y es de user. La usan PUT /locks y la tarea 12.

    Si no existe: 409 E_LOCK_LOST ("perdiste el lock, empieza de nuevo"). Si es de otro
    usuario: 403 E_FORBIDDEN.
    """
    info = await find_lock(etcd, lock_id)
    if info is None:
        raise DFSError(ErrorCode.E_LOCK_LOST, f"el lock {lock_id} ya no existe")
    if info.user != user:
        raise DFSError(ErrorCode.E_FORBIDDEN, f"el lock {lock_id} es de otro usuario")
    return info


# --- Pedir, renovar y liberar ---


async def acquire_lock(etcd: Etcd, user: str, path: str, *, wait_s: float, lease_s: int, renew_s: float) -> str:
    """Toma el lock de path y devuelve su lock_id (POST /locks).

    La raíz o un directorio: 400 E_BAD_REQUEST. Una ruta que no existe sí se puede bloquear,
    porque w la crea. Pide un lease de lease_s (DFS_LOCK_LEASE_S) y espera en la fila hasta
    wait_s segundos, renovando el lease cada renew_s (DFS_LOCK_RENEW_S). Si no lo obtiene,
    revoca el lease para sacar el intento de la fila y responde 423 E_LOCKED.
    """
    if path == "/" or await _is_dir(etcd, user, path):
        raise DFSError(ErrorCode.E_BAD_REQUEST, f"{path} es un directorio: el lock es solo para archivos")

    lease_id = await etcd.lease_grant(lease_s)
    key = None
    try:
        key = await _wait_in_line(etcd, lock_name(user, path), lease_id, wait_s, renew_s)
    finally:
        # Si no lo obtuvo, o si la espera falló (por ejemplo, etcd dejó de responder),
        # el intento sale de la fila para no quitarle el turno a nadie.
        if key is None:
            await _revoke_attempt(etcd, lease_id)
    if key is None:
        raise DFSError(ErrorCode.E_LOCKED, f"otro cliente tiene el lock de {path}")
    return lease_hex(lease_id)


async def renew_lock(etcd: Etcd, lock_id: str, user: str) -> None:
    """Renueva el lease del lock (PUT /locks/{lock_id}). Si venció: 409 E_LOCK_LOST."""
    await resolve_lock(etcd, lock_id, user)
    try:
        await etcd.lease_keepalive(lease_from_hex(lock_id))
    except LeaseNotFound:
        # Venció entre resolve_lock y la renovación.
        raise DFSError(ErrorCode.E_LOCK_LOST, f"el lock {lock_id} ya no existe") from None


async def release_lock(etcd: Etcd, lock_id: str, user: str) -> None:
    """Libera el lock revocando su lease; entra el siguiente de la fila (DELETE /locks/{lock_id}).

    Es idempotente: si el lock no existe, no hace nada. Si es de otro usuario: 403 E_FORBIDDEN.
    """
    info = await find_lock(etcd, lock_id)
    if info is None:
        return
    if info.user != user:
        raise DFSError(ErrorCode.E_FORBIDDEN, f"el lock {lock_id} es de otro usuario")
    try:
        await etcd.lease_revoke(lease_from_hex(lock_id))
    except LeaseNotFound:
        pass  # venció justo antes: el lock ya está suelto


async def _is_dir(etcd: Etcd, user: str, path: str) -> bool:
    entry = await etcd.get(ns_key(user, path))
    return entry is not None and entry.json()["type"] == "dir"


# --- La espera en la fila ---


async def _wait_in_line(etcd: Etcd, name: str, lease_id: int, wait_s: float, renew_s: float) -> str | None:
    """etcd.lock, renovando el lease del intento mientras espera en la fila.

    El servicio de lock de etcd no renueva el lease, y la espera (hasta 60 s) puede durar
    más que el lease (30 s): si vence, el intento pierde su puesto. Por eso una tarea aparte
    lo renueva cada renew_s, y se cancela apenas termina la espera, haya obtenido el lock o no.
    Devuelve la clave del lock, o None si no lo obtuvo.
    """
    renewer = asyncio.create_task(_renew_every(etcd, lease_id, renew_s))
    try:
        return await etcd.lock(name, lease_id, wait_s=wait_s)
    except LeaseNotFound:
        return None  # el lease venció en la fila: es una espera que se agotó (decisión 2)
    finally:
        renewer.cancel()


async def _renew_every(etcd: Etcd, lease_id: int, renew_s: float) -> None:
    """Renueva el lease cada renew_s hasta que la cancelen. Termina sola si el lease ya no existe."""
    while True:
        await asyncio.sleep(renew_s)
        try:
            await etcd.lease_keepalive(lease_id)
        except LeaseNotFound:
            return  # venció: el intento ya salió de la fila y etcd.lock no lo obtendrá
        except (DFSError, EtcdError) as exc:
            logger.warning("no se pudo renovar el lease %x mientras espera el lock; se reintenta: %s", lease_id, exc)


async def _revoke_attempt(etcd: Etcd, lease_id: int) -> None:
    """Revoca el lease de un intento que no obtuvo el lock. Si falla, no cambia la respuesta.

    Un error aquí cambiaría el 423 por un 503 (decisión 3). Si la revocación falla, el lease
    vence solo en DFS_LOCK_LEASE_S y el intento sale de la fila.
    """
    try:
        await etcd.lease_revoke(lease_id)
    except LeaseNotFound:
        pass  # ya venció: el intento ya salió de la fila
    except (DFSError, EtcdError) as exc:
        logger.warning("no se pudo revocar el lease %x de un intento de lock; vencerá solo: %s", lease_id, exc)
