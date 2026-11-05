"""Subidas: reservar, pedir otros destinos y publicar la versión nueva (tarea 12).

El camino de una escritura (docs/contratos.md, sección 1, "Cómo se encadenan las rutas"):

1. POST /uploads (create_upload): con el lock del archivo, dice qué bloques ya están
   guardados (present) y, para cada uno que falta, a qué DataNode enviarlo, con un pase de
   escritura que lleva los secundarios (DA-04, DA-05). Guarda la subida en /uploads/{id},
   atada al lease del lock.
2. POST /uploads/{id}/realloc (realloc_upload): otros destinos para los bloques que fallaron,
   o pases nuevos para los que vencieron.
3. POST /uploads/{id}/commit (commit_upload): comprueba que los bloques estén confirmados y
   formen el archivo, y publica la versión con una transacción de etcd (DA-08, G1, G4).

La forma del archivo se comprueba en check_commit, una función pura. Las decisiones de esta
tarea están en docs/contratos.md, sección 13.
"""

import random
import time
import uuid
from pathlib import PurePosixPath

from dfs_common.config import Settings
from dfs_common.errors import DFSError, ErrorCode
from dfs_common.models import (
    BlockAssignment,
    BlockPass,
    CommitRequest,
    CommitResponse,
    NodeAddress,
    ReallocRequest,
    ReallocResponse,
    UploadRequest,
    UploadResponse,
)
from dfs_common.security import sign_pass
from metaserver.blocks import in_two_live_zones, touch_block
from metaserver.etcd import Etcd, KeyValue, LeaseNotFound, compare, lease_from_hex, op_delete, op_put
from metaserver.fs import now_ms
from metaserver.keys import block_key, manifest_key, ns_key, upload_key
from metaserver.locks import LockInfo, resolve_lock
from metaserver.nodes import Node, by_id, live_nodes
from metaserver.placement import choose_nodes

MIB = 1024 * 1024


def new_start() -> int:
    """Inicio aleatorio de los turnos de choose_nodes: uno por reserva y uno por realloc (sección 13)."""
    return random.randrange(2**31)


# --- Reserva ---


async def create_upload(
    etcd: Etcd, user: str, body: UploadRequest, settings: Settings, pass_key: bytes
) -> UploadResponse:
    """POST /uploads: comprueba la reserva, separa los bloques presentes de los que faltan y
    guarda la subida atada al lease del lock."""
    lock = await _check_reservation(etcd, user, body, settings)

    nodes = await live_nodes(etcd)
    alive = by_id(nodes)
    start = new_start()
    exp = int(time.time()) + settings.pass_ttl_s
    present, missing = [], []
    for block in sorted(body.blocks, key=lambda block: block.index):
        stored = await etcd.get(block_key(user, block.hash))
        if stored is not None and in_two_live_zones(stored.json()["stored_on"], alive):
            await touch_block(etcd, user, block.hash)  # para que la limpieza no lo borre (DA-02)
            present.append(block.index)
        else:
            chosen = choose_nodes(nodes, block.index, start, (), body.block_size, settings.w)
            missing.append(_assignment(user, block.index, block.hash, chosen, exp, pass_key))

    upload_id = str(uuid.uuid4())
    upload = {
        "user": user,
        "path": body.path,
        "mode": body.mode,
        "base_version": body.base_version,
        "block_size": body.block_size,
        "lock_key": lock.key,
        "blocks": [block.model_dump() for block in body.blocks],
        "created_at": now_ms(),
    }
    try:
        # Atada al lease del lock: si el lock vence, la subida desaparece sola.
        await etcd.put(upload_key(upload_id), upload, lease=lease_from_hex(body.lock_id))
    except LeaseNotFound:
        raise DFSError(ErrorCode.E_LOCK_LOST, f"el lock {body.lock_id} venció") from None
    return UploadResponse(upload_id=upload_id, present=present, missing=missing)


async def _check_reservation(etcd: Etcd, user: str, body: UploadRequest, settings: Settings) -> LockInfo:
    """Las comprobaciones de POST /uploads, en este orden; la primera que falla responde:

    1. la raíz o un directorio: 400; el padre no existe o es un archivo: 404;
    2. el lock (409 o 403), que además debe ser de esta ruta (409 E_LOCK_LOST);
    3. la versión base debe ser la actual (0 si el archivo no existe): 409 E_VERSION_CONFLICT;
    4. block_size: DFS_BLOCK_MIB MiB con w o con un archivo nuevo; con a y rw sobre un archivo
       que existe, el de su versión actual (decisión 2 de la 02c): 400.
    Los índices repetidos y los bloques mayores que block_size ya los rechaza el modelo (400).
    Devuelve el lock.
    """
    entry = await _file_entry(etcd, user, body.path)
    await _parent_entry(etcd, user, body.path)
    lock = await resolve_lock(etcd, body.lock_id, user)
    if lock.path != body.path:
        raise DFSError(ErrorCode.E_LOCK_LOST, f"el lock {body.lock_id} es de otro archivo")
    if body.base_version != _version(entry):
        raise DFSError(ErrorCode.E_VERSION_CONFLICT, f"la versión base {body.base_version} ya no es la actual")
    base = await _manifest(etcd, entry)
    expected = settings.block_mib * MIB if body.mode == "w" or base is None else base.json()["block_size"]
    if body.block_size != expected:
        raise DFSError(ErrorCode.E_BAD_REQUEST, f"block_size debe ser {expected}")
    return lock


# --- Otros destinos ---


async def realloc_upload(
    etcd: Etcd, user: str, upload_id: str, body: ReallocRequest, settings: Settings, pass_key: bytes
) -> ReallocResponse:
    """POST /uploads/{upload_id}/realloc: primario y pase nuevos, sin los nodos de exclude_nodes.

    No pide el lock: la subida está atada a su lease, y si vence, la subida ya no existe (404).
    Con exclude_nodes vacío sirve para renovar pases vencidos (decisión 3 de la 02c).
    """
    upload = await _upload(etcd, user, upload_id)
    hashes = {block["index"]: block["hash"] for block in upload["blocks"]}
    unknown = sorted(set(body.indices) - hashes.keys())
    if unknown:
        raise DFSError(ErrorCode.E_BAD_REQUEST, f"los índices {unknown} no son de esta subida")

    nodes = await live_nodes(etcd)
    start = new_start()
    exp = int(time.time()) + settings.pass_ttl_s
    missing = []
    for index in body.indices:
        chosen = choose_nodes(nodes, index, start, set(body.exclude_nodes), upload["block_size"], settings.w)
        missing.append(_assignment(user, index, hashes[index], chosen, exp, pass_key))
    return ReallocResponse(missing=missing)


# --- Commit ---


async def commit_upload(etcd: Etcd, user: str, upload_id: str, body: CommitRequest) -> CommitResponse:
    """POST /uploads/{upload_id}/commit. El orden de las comprobaciones está en la sección 13, punto 2."""
    lock = await resolve_lock(etcd, body.lock_id, user)
    upload = await _upload(etcd, user, upload_id)
    if upload["path"] != lock.path:
        raise DFSError(ErrorCode.E_LOCK_LOST, f"el lock {body.lock_id} es de otro archivo")
    path = upload["path"]
    entry = await etcd.get(ns_key(user, path))
    if body.base_version != _version(entry):
        raise DFSError(ErrorCode.E_VERSION_CONFLICT, f"la versión base {body.base_version} ya no es la actual")
    parent = await _parent_entry(etcd, user, path) if body.base_version == 0 else None
    base = await _manifest(etcd, entry)
    await _check_blocks(etcd, user, body, upload, base.json() if base else None)
    return await _publish(etcd, user, upload_id, upload, body, lock.key, entry, parent, base)


async def _check_blocks(etcd: Etcd, user: str, body: CommitRequest, upload: dict, base: dict | None) -> None:
    """Los bloques de la versión nueva, antes de publicarla:

    1. cada uno tiene su clave en /blocks/ (está confirmado); si no, 422 E_REPLICAS. Se comprueba
       aquí y no en la transacción: etcd admite hasta 128 operaciones por transacción, y la
       limpieza no borra un bloque tocado hace poco;
    2. forman el archivo: sin huecos, sin truncar con a y rw y sin cambiar lo que ya estaba con a (400);
    3. G1: todo bloque que no estaba en la versión base, venga o no en la subida, tiene copias
       vivas en 2 zonas (422). Los de la base solo necesitan su clave (sección 13, punto 1).
    """
    stored = {}
    for hash in dict.fromkeys(body.hashes):
        kv = await etcd.get(block_key(user, hash))
        if kv is None:
            raise DFSError(ErrorCode.E_REPLICAS, f"el bloque {hash} no está confirmado")
        stored[hash] = kv.json()
    sizes = [stored[hash]["size"] for hash in body.hashes]
    check_commit(body.hashes, sizes, body.size, upload["block_size"], upload["mode"], base)
    from_base = set(base["hashes"]) if base else set()
    alive = by_id(await live_nodes(etcd))
    for hash, block in stored.items():
        if hash not in from_base and not in_two_live_zones(block["stored_on"], alive):
            raise DFSError(ErrorCode.E_REPLICAS, f"las copias vivas del bloque {hash} no cubren 2 zonas")


async def _publish(
    etcd: Etcd,
    user: str,
    upload_id: str,
    upload: dict,
    body: CommitRequest,
    lock_key: str,
    entry: KeyValue | None,
    parent: KeyValue | None,
    base: KeyValue | None,
) -> CommitResponse:
    """La transacción del commit (docs/contratos.md, sección 8).

    Compara que la clave del lock exista y, si el archivo es nuevo, que su clave no exista y
    que el padre no haya cambiado (la raíz no se compara); si no, que /ns no haya cambiado.
    Si pasa, escribe /ns con la versión nueva, el manifiesto nuevo y el replaced_at del
    anterior, y borra la subida, todo en un paso.
    """
    path = upload["path"]
    key = ns_key(user, path)
    version = body.base_version + 1
    file_id = entry.json()["file_id"] if body.base_version else str(uuid.uuid4())
    now = now_ms()

    conditions = [compare(lock_key, "CREATE", "GREATER", 0)]  # el lock sigue tomado
    if body.base_version == 0:
        conditions.append(compare(key, "CREATE", "EQUAL", 0))
        if parent is not None:
            conditions.append(compare(parent.key, "MOD", "EQUAL", parent.mod_revision))
    else:
        conditions.append(compare(key, "MOD", "EQUAL", entry.mod_revision))
    manifest = {
        "user": user,
        "size": body.size,
        "block_size": upload["block_size"],
        "hashes": body.hashes,
        "file_sha256": body.file_sha256,
        "created_at": now,
        "replaced_at": None,
    }
    operations = [
        op_put(key, {"type": "file", "file_id": file_id, "version": version}),
        op_put(manifest_key(file_id, version), manifest),
        op_delete(upload_key(upload_id)),
    ]
    if base is not None:
        operations.append(op_put(base.key, {**base.json(), "replaced_at": now}))  # queda retenida (DA-14)

    if (await etcd.txn(conditions, operations, [])).succeeded:
        return CommitResponse(path=path, version=version)
    raise await _failed_commit(etcd, lock_key, parent, path)


async def _failed_commit(etcd: Etcd, lock_key: str, parent: KeyValue | None, path: str) -> DFSError:
    """Falló una comparación de la transacción del commit: se vuelve a leer para saber cuál.

    Sin la clave del lock: 409 E_LOCK_LOST. Con el padre cambiado o borrado: 404. Si no, la ruta
    cambió (otro commit, o alguien la creó): 409 E_VERSION_CONFLICT.
    """
    if await etcd.get(lock_key) is None:
        return DFSError(ErrorCode.E_LOCK_LOST, "el lock venció antes de publicar")
    if parent is not None:
        current = await etcd.get(parent.key)
        if current is None or current.mod_revision != parent.mod_revision:
            return DFSError(ErrorCode.E_NOT_FOUND, "el directorio padre cambió o ya no existe")
    return DFSError(ErrorCode.E_VERSION_CONFLICT, f"{path} cambió antes de publicar")


def expected_sizes(size: int, block_size: int) -> list[int]:
    """Tamaño de cada bloque de un archivo de size bytes: todos block_size, salvo el último, que mide lo que falta."""
    count = -(-size // block_size)  # ⌈size / block_size⌉
    return [min(block_size, size - index * block_size) for index in range(count)]


def check_commit(
    hashes: list[str], sizes: list[int], size: int, block_size: int, mode: str, base: dict | None
) -> None:
    """Comprueba que los bloques formen el archivo nuevo (sección 2). Si no, 400 E_BAD_REQUEST.

    hashes y sizes: los bloques de la versión nueva, en orden, con el tamaño guardado en
    /blocks/. base: el manifiesto de la versión base, o None si el archivo es nuevo.

    - ⌈size / block_size⌉ bloques que miden block_size, salvo el último: así se ve un hueco;
    - con a y rw, sin truncar: size no puede ser menor que el de la base (DA-14);
    - con a, los ⌊tamaño base / block_size⌋ primeros bloques son los de la base: anexar no
      cambia lo que ya estaba.
    """
    if sizes != expected_sizes(size, block_size):
        raise DFSError(ErrorCode.E_BAD_REQUEST, f"los bloques no forman un archivo de {size} bytes sin huecos")
    if base is None:
        return
    if mode in ("a", "rw") and size < base["size"]:
        raise DFSError(ErrorCode.E_BAD_REQUEST, f"{mode} no puede truncar el archivo ({base['size']} bytes)")
    kept = base["size"] // block_size
    if mode == "a" and hashes[:kept] != base["hashes"][:kept]:
        raise DFSError(ErrorCode.E_BAD_REQUEST, "anexar no puede cambiar los bloques que ya estaban")


# --- Auxiliares ---


def _version(entry: KeyValue | None) -> int | None:
    """Versión actual de la ruta: 0 si no existe, y None si es un directorio (nunca es la base)."""
    if entry is None:
        return 0
    node = entry.json()
    return node["version"] if node["type"] == "file" else None


async def _file_entry(etcd: Etcd, user: str, path: str) -> KeyValue | None:
    """La entrada de la ruta, o None si el archivo no existe. La raíz o un directorio: 400."""
    if path == "/":
        raise DFSError(ErrorCode.E_BAD_REQUEST, "la raíz / es un directorio")
    entry = await etcd.get(ns_key(user, path))
    if entry is not None and entry.json()["type"] == "dir":
        raise DFSError(ErrorCode.E_BAD_REQUEST, f"{path} es un directorio")
    return entry


async def _parent_entry(etcd: Etcd, user: str, path: str) -> KeyValue | None:
    """La entrada del directorio padre, o None si es la raíz. Si no existe o es un archivo: 404."""
    parent = str(PurePosixPath(path).parent)
    if parent == "/":
        return None
    entry = await etcd.get(ns_key(user, parent))
    if entry is None or entry.json()["type"] != "dir":
        raise DFSError(ErrorCode.E_NOT_FOUND, f"el directorio padre {parent} no existe o es un archivo")
    return entry


async def _manifest(etcd: Etcd, entry: KeyValue | None) -> KeyValue | None:
    """El manifiesto de la versión actual del archivo, o None si el archivo no existe."""
    if entry is None:
        return None
    node = entry.json()
    return await etcd.get(manifest_key(node["file_id"], node["version"]))


async def _upload(etcd: Etcd, user: str, upload_id: str) -> dict:
    """La subida upload_id. Si no existe (o su lock venció): 404. Si es de otro usuario: 403."""
    kv = await etcd.get(upload_key(upload_id))
    if kv is None:
        raise DFSError(ErrorCode.E_NOT_FOUND, f"la subida {upload_id} no existe o su lock venció")
    upload = kv.json()
    if upload["user"] != user:
        raise DFSError(ErrorCode.E_FORBIDDEN, f"la subida {upload_id} es de otro usuario")
    return upload


def _assignment(user: str, index: int, hash: str, chosen: list[Node], exp: int, pass_key: bytes) -> BlockAssignment:
    """El primario, con su dirección pública, y un pase de escritura con los secundarios, con la interna."""
    primary, *secondaries = chosen
    claims = BlockPass(
        user=user, hash=hash, op="w", secondaries=[node.internal_address for node in secondaries], exp=exp
    )
    return BlockAssignment(
        index=index,
        hash=hash,
        primary=NodeAddress(node_id=primary.node_id, address=primary.public_address),
        pass_=sign_pass(claims, pass_key),
    )
