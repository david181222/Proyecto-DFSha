"""MetaServers vivos: autorregistro y GET /cluster/metaservers (tarea 13, DA-11).

Cada MetaServer se registra al arrancar en /nodes/meta/{DFS_NODE_ID} = {ip, public_port,
internal_port}, atada a un lease de DFS_NODE_LEASE_S, y lo renueva cada DFS_HEARTBEAT_S con
una tarea de fondo del lifespan de la app pública (una sola vez por proceso). El cliente y los
DataNodes descubren a los MetaServers leyendo esa lista.

- Si el lease ya no existe (por ejemplo, el proceso estuvo pausado), se registra de nuevo.
- Si etcd no responde en una vuelta, queda en el log y se intenta en la siguiente.
- Si etcd no responde al arrancar, el arranque falla y Docker reinicia el contenedor (tarea 07).
"""

import asyncio
import logging

from dfs_common.config import Settings
from dfs_common.errors import DFSError
from dfs_common.models import MetaServerInfo, MetaServerListResponse
from metaserver.etcd import Etcd, EtcdError, LeaseNotFound
from metaserver.keys import METASERVERS_PREFIX, metaserver_key

logger = logging.getLogger("dfsha")


async def register_self(etcd: Etcd, settings: Settings) -> int:
    """Escribe /nodes/meta/{DFS_NODE_ID} atada a un lease nuevo de DFS_NODE_LEASE_S y devuelve el lease.

    DFS_NODE_ID y DFS_NODE_IP son obligatorias: sin ellas el MetaServer no arranca. python -m
    metaserver ya las exige antes de abrir los puertos (Settings.require); esta comprobación
    cubre el lifespan, donde un SystemExit se confundiría con una cancelación.
    """
    if not settings.node_id or settings.node_ip is None:
        raise ValueError("el MetaServer no arranca sin DFS_NODE_ID y DFS_NODE_IP: las necesita para registrarse")
    lease_id = await etcd.lease_grant(settings.node_lease_s)
    info = {"ip": str(settings.node_ip), "public_port": settings.meta_public_port, "internal_port": settings.meta_internal_port}
    await etcd.put(metaserver_key(settings.node_id), info, lease=lease_id)
    return lease_id


async def keep_registered(etcd: Etcd, settings: Settings, lease_id: int, every_s: float) -> None:
    """Renueva el lease del registro cada every_s (DFS_HEARTBEAT_S) hasta que la cancelen."""
    while True:
        await asyncio.sleep(every_s)
        try:
            try:
                await etcd.lease_keepalive(lease_id)
            except LeaseNotFound:
                lease_id = await register_self(etcd, settings)
                logger.warning("el registro de %s en etcd había vencido; se registró de nuevo", settings.node_id)
        except (DFSError, EtcdError) as exc:
            logger.warning("no se pudo renovar el registro de %s; se intenta en la siguiente vuelta: %s", settings.node_id, exc)


async def list_metaservers(etcd: Etcd) -> MetaServerListResponse:
    """Los MetaServers vivos de /nodes/meta/, ordenados por meta_id (el orden de las claves)."""
    kvs = await etcd.get_prefix(METASERVERS_PREFIX)
    return MetaServerListResponse(
        metaservers=[MetaServerInfo(meta_id=kv.key.removeprefix(METASERVERS_PREFIX), **kv.json()) for kv in kvs]
    )
