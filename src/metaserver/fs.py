"""Directorios y archivos del espacio de cada usuario: ls, stat, mkdir, rmdir y rm (RF1).

Cada usuario tiene su árbol en /ns/{user}{path} (docs/contratos.md, sección 8): un
directorio guarda {type: "dir", created_at} y un archivo {type: "file", file_id, version}.
El tamaño y la fecha de un archivo están en su manifiesto actual, /files/{file_id}/v/{version}.
La raíz / no tiene clave: existe siempre y es un directorio.

mkdir y rmdir usan las transacciones de "Crear y borrar sin carreras": si un mkdir
dentro de un directorio y el rmdir de ese directorio se cruzan, el segundo ve el cambio
y falla. rm toma el lock del archivo sin esperar (DA-08) y deja retenida la versión
actual (DA-14).
"""

import logging
import time
from pathlib import PurePosixPath

from dfs_common.errors import DFSError, ErrorCode
from dfs_common.models import DirEntry, LsResponse, StatResponse
from metaserver.etcd import Etcd, EtcdError, KeyValue, LeaseNotFound, compare, op_delete, op_put, prefix_end
from metaserver.keys import children_prefix, lock_name, manifest_key, ns_key

logger = logging.getLogger("dfsha")


def now_ms() -> int:
    """Instante actual en milisegundos desde epoch (sección 1)."""
    return time.time_ns() // 1_000_000


def _parent(path: str) -> str:
    return str(PurePosixPath(path).parent)


def _not_found(path: str) -> DFSError:
    return DFSError(ErrorCode.E_NOT_FOUND, f"{path} no existe")


async def _unchanged(etcd: Etcd, kv: KeyValue) -> bool:
    """Si la clave sigue siendo la que se leyó: existe y nadie la tocó desde entonces.

    Cualquier cambio (reescribirla, o borrarla y volver a crearla) le da otra mod_revision.
    Sirve para saber, cuando una transacción falla, cuál de sus comparaciones falló.
    """
    current = await etcd.get(kv.key)
    return current is not None and current.mod_revision == kv.mod_revision


async def _entry(etcd: Etcd, name: str, kv: KeyValue) -> DirEntry:
    """Una entrada de ls. Un directorio tiene size 0, version 0 y modified_at = created_at;
    un archivo toma size y modified_at (el created_at) de su manifiesto actual."""
    node = kv.json()
    if node["type"] == "dir":
        return DirEntry(name=name, type="dir", size=0, version=0, modified_at=node["created_at"])
    manifest = (await etcd.get(manifest_key(node["file_id"], node["version"]))).json()
    return DirEntry(
        name=name, type="file", size=manifest["size"], version=node["version"], modified_at=manifest["created_at"]
    )


async def ls(etcd: Etcd, user: str, path: str) -> LsResponse:
    """Hijos directos de un directorio, por nombre; sobre un archivo, solo ese archivo.

    Como decidió la tarea 08, lee todo lo que hay bajo el directorio (también dentro de sus
    subdirectorios) y descarta lo que no es hijo directo; después, un manifiesto por archivo.
    El costo crece con todo el subárbol, no solo con las entradas del directorio.
    """
    if path != "/":
        kv = await etcd.get(ns_key(user, path))
        if kv is None:
            raise _not_found(path)
        if kv.json()["type"] == "file":
            return LsResponse(entries=[await _entry(etcd, PurePosixPath(path).name, kv)])
    prefix = children_prefix(user, path)
    entries = []
    for kv in await etcd.get_prefix(prefix):
        name = kv.key.removeprefix(prefix)
        if "/" not in name:  # lo que tiene otra / está dentro de un subdirectorio
            entries.append(await _entry(etcd, name, kv))
    return LsResponse(entries=sorted(entries, key=lambda entry: entry.name))


async def stat(etcd: Etcd, user: str, path: str) -> StatResponse:
    """Tipo, tamaño, versión y fecha, con los mismos valores que en ls. La raíz tiene modified_at 0."""
    if path == "/":
        return StatResponse(path="/", type="dir", size=0, version=0, modified_at=0)
    kv = await etcd.get(ns_key(user, path))
    if kv is None:
        raise _not_found(path)
    entry = await _entry(etcd, PurePosixPath(path).name, kv)
    return StatResponse(path=path, **entry.model_dump(exclude={"name"}))


async def mkdir(etcd: Etcd, user: str, path: str) -> None:
    """Crea un directorio; no crea los padres.

    Transacción: la clave no existe (create_revision = 0) y el padre no cambió desde que se
    leyó (mod_revision). La raíz no tiene clave, así que con ella de padre no se compara.
    """
    if path == "/":
        raise DFSError(ErrorCode.E_EXISTS, "la raíz / existe siempre")
    key = ns_key(user, path)
    parent = _parent(path)
    conditions = [compare(key, "CREATE", "EQUAL", 0)]
    if parent != "/":
        parent_kv = await etcd.get(ns_key(user, parent))
        if parent_kv is None or parent_kv.json()["type"] != "dir":
            raise DFSError(ErrorCode.E_NOT_FOUND, f"el directorio padre {parent} no existe o es un archivo")
        conditions.append(compare(parent_kv.key, "MOD", "EQUAL", parent_kv.mod_revision))

    created = await etcd.txn(conditions, [op_put(key, {"type": "dir", "created_at": now_ms()})], [])
    if created.succeeded:
        return
    # Falló una comparación: se vuelve a leer una vez para saber cuál. Si el padre sigue
    # igual (o es la raíz, que no se compara), falló la otra: la ruta ya existía.
    if parent == "/" or await _unchanged(etcd, parent_kv):
        raise DFSError(ErrorCode.E_EXISTS, f"{path} ya existe")
    raise DFSError(ErrorCode.E_NOT_FOUND, f"el directorio padre {parent} cambió o ya no existe")


async def rmdir(etcd: Etcd, user: str, path: str) -> None:
    """Borra un directorio vacío. La raíz ya la rechaza la validación de la ruta (400).

    Transacción: el directorio no cambió (mod_revision) y no hay claves bajo él. Esto
    último es una comparación de rango con create_revision = 0, verdadera solo si el
    rango está vacío.
    """
    kv = await etcd.get(ns_key(user, path))
    if kv is None:
        raise _not_found(path)
    if kv.json()["type"] != "dir":
        raise DFSError(ErrorCode.E_BAD_REQUEST, f"{path} es un archivo: se borra con rm")
    prefix = children_prefix(user, path)
    empty = compare(prefix, "CREATE", "EQUAL", 0, range_end=prefix_end(prefix))
    unchanged = compare(kv.key, "MOD", "EQUAL", kv.mod_revision)

    deleted = await etcd.txn([empty, unchanged], [op_delete(kv.key)], [])
    if deleted.succeeded:
        return
    # Falló una comparación: se vuelve a leer una vez para saber cuál. Si el directorio
    # sigue igual, falló la otra: tenía entradas al hacer la transacción (aunque ya no las tenga).
    if await _unchanged(etcd, kv):
        raise DFSError(ErrorCode.E_NOT_EMPTY, f"{path} no está vacío")
    raise DFSError(ErrorCode.E_NOT_FOUND, f"{path} cambió o ya no existe")


async def rm(etcd: Etcd, user: str, path: str, lock_lease_s: int) -> None:
    """Borra un archivo; su versión actual queda retenida (replaced_at) hasta que la quite la limpieza.

    Toma el lock del archivo sin esperar, con un lease propio de lock_lease_s (DFS_LOCK_LEASE_S):
    si otro cliente lo tiene, 423 E_LOCKED. Con el lock, una transacción compara la
    mod_revision leída de /ns, borra la entrada y llena replaced_at en el manifiesto actual.
    Al final siempre revoca el lease, y eso suelta el lock.
    """
    kv = await etcd.get(ns_key(user, path))
    if kv is None:
        raise _not_found(path)
    node = kv.json()
    if node["type"] != "file":
        raise DFSError(ErrorCode.E_BAD_REQUEST, f"{path} es un directorio: se borra con rmdir")
    manifest = await etcd.get(manifest_key(node["file_id"], node["version"]))

    lease_id = await etcd.lease_grant(lock_lease_s)
    try:
        if await etcd.lock(lock_name(user, path), lease_id, wait_s=0) is None:
            raise DFSError(ErrorCode.E_LOCKED, f"otro cliente tiene el lock de {path}")
        retained = {**manifest.json(), "replaced_at": now_ms()}
        removed = await etcd.txn(
            [compare(kv.key, "MOD", "EQUAL", kv.mod_revision)],
            [op_delete(kv.key), op_put(manifest.key, retained)],
            [],
        )
        if not removed.succeeded:
            raise DFSError(ErrorCode.E_LOCKED, f"{path} cambió mientras se borraba; se puede reintentar")
    finally:
        await _release_lock(etcd, lease_id)


async def _release_lock(etcd: Etcd, lease_id: int) -> None:
    """Revoca el lease del lock de rm. Si falla, no cambia lo que responde rm.

    Un error aquí taparía el resultado: un rm que ya borró respondería 503, o un 423 se
    volvería 503. Si la revocación falla, el lease vence solo en DFS_LOCK_LEASE_S y suelta el lock.
    """
    try:
        await etcd.lease_revoke(lease_id)
    except LeaseNotFound:
        pass  # ya venció: el lock ya está suelto
    except (DFSError, EtcdError) as exc:
        logger.warning("no se pudo revocar el lease %x del lock de rm; vencerá solo: %s", lease_id, exc)
