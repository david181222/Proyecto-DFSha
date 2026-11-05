"""Lectura de un archivo: GET /files/blocks (tarea 12; DA-05, DA-10, DA-11, DA-14).

Entrega el manifiesto de una versión con, por cada bloque, dónde está (stored_on), a qué
DataNodes vivos pedirlo (replicas) y un pase de lectura. El cliente pide cada bloque a la
primera réplica que responda (docs/contratos.md, sección 1).
"""

import time

from dfs_common.config import Settings
from dfs_common.errors import DFSError, ErrorCode
from dfs_common.models import BlockLocation, BlockPass, FileBlocksResponse, NodeAddress
from dfs_common.security import sign_pass
from metaserver.etcd import Etcd
from metaserver.fs import now_ms
from metaserver.keys import block_key, manifest_key, ns_key
from metaserver.nodes import Node, by_id, live_nodes
from metaserver.uploads import expected_sizes


async def file_blocks(
    etcd: Etcd,
    user: str,
    path: str,
    version: int | None,
    client_zone: str | None,
    settings: Settings,
    pass_key: bytes,
) -> FileBlocksResponse:
    """La versión actual, o version si es la actual o sigue retenida (DA-14); si no, 404.

    Después de rm, /ns ya no existe y responde 404 aunque el manifiesto siga retenido: el
    lector termina con los pases que tiene y no puede renovarlos. Es una limitación aceptada
    (docs/contratos.md, sección 2).
    """
    wanted, manifest = await _readable_manifest(etcd, user, path, version, settings.retention_s)
    alive = by_id(await live_nodes(etcd))
    exp = int(time.time()) + settings.pass_ttl_s
    sizes = expected_sizes(manifest["size"], manifest["block_size"])
    blocks = []
    for index, (hash, size) in enumerate(zip(manifest["hashes"], sizes)):
        stored = await etcd.get(block_key(user, hash))
        stored_on = stored.json()["stored_on"] if stored else []
        blocks.append(
            BlockLocation(
                index=index,
                hash=hash,
                size=size,
                stored_on=stored_on,
                replicas=order_replicas(stored_on, alive, client_zone),
                pass_=sign_pass(BlockPass(user=user, hash=hash, op="r", exp=exp), pass_key),
            )
        )
    return FileBlocksResponse(
        path=path,
        version=wanted,
        size=manifest["size"],
        block_size=manifest["block_size"],
        file_sha256=manifest["file_sha256"],
        blocks=blocks,
    )


async def _readable_manifest(
    etcd: Etcd, user: str, path: str, version: int | None, retention_s: int
) -> tuple[int, dict]:
    """La versión que se puede leer y su manifiesto: la actual, o version si es la actual o sigue
    retenida. La raíz o un directorio: 400. La ruta o la versión no existen, o ya no está retenida: 404."""
    if path == "/":
        raise DFSError(ErrorCode.E_BAD_REQUEST, "la raíz / es un directorio")
    entry = await etcd.get(ns_key(user, path))
    if entry is None:
        raise DFSError(ErrorCode.E_NOT_FOUND, f"{path} no existe")
    node = entry.json()
    if node["type"] == "dir":
        raise DFSError(ErrorCode.E_BAD_REQUEST, f"{path} es un directorio")
    wanted = node["version"] if version is None else version
    kv = await etcd.get(manifest_key(node["file_id"], wanted))
    if kv is None:
        raise DFSError(ErrorCode.E_NOT_FOUND, f"la versión {wanted} de {path} no existe")
    manifest = kv.json()
    if wanted != node["version"] and not _retained(manifest, retention_s):
        raise DFSError(ErrorCode.E_NOT_FOUND, f"la versión {wanted} de {path} ya no está retenida; reabre el archivo")
    return wanted, manifest


def order_replicas(stored_on: list[str], alive: dict[str, Node], client_zone: str | None) -> list[NodeAddress]:
    """Los DataNodes vivos de stored_on, con su dirección pública: primero los de la zona del
    cliente y, dentro de cada grupo, en el orden de stored_on (sort es estable)."""
    replicas = [alive[node_id] for node_id in stored_on if node_id in alive]
    replicas.sort(key=lambda node: node.zone != client_zone)
    return [NodeAddress(node_id=node.node_id, address=node.public_address) for node in replicas]


def _retained(manifest: dict, retention_s: int) -> bool:
    """Una versión reemplazada se puede leer durante retention_s (DFS_RETENTION_S) desde replaced_at."""
    replaced_at = manifest["replaced_at"]
    return replaced_at is not None and now_ms() - replaced_at < retention_s * 1000
