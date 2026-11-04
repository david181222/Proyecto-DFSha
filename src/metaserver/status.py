"""Estado del sistema para dfs status: GET /admin/status (tarea 13, DA-16).

admin_status lee etcd y build_admin_status arma la respuesta; esta última es una función pura.
Recorrer todo /blocks/ en cada consulta es aceptable a la escala del proyecto.

El líder es el valor de la elección /election/leader: la tarea 19 hace campaña con su meta_id
en UTF-8 como valor. Si no hay líder, null.
"""

from dfs_common.models import AdminStatusResponse, DataNodeStatus, PendingOrder, UnderReplicatedBlock
from metaserver.etcd import Etcd
from metaserver.keys import BLOCKS_PREFIX, ELECTION_NAME, ORDERS_PREFIX
from metaserver.metaservers import list_metaservers
from metaserver.nodes import Node, by_id, live_nodes

COPIES = 3  # copias que debe tener cada bloque (DA-04)


async def admin_status(etcd: Etcd) -> AdminStatusResponse:
    leader = await etcd.leader(ELECTION_NAME)
    metaservers = (await list_metaservers(etcd)).metaservers
    blocks = []
    for kv in await etcd.get_prefix(BLOCKS_PREFIX):
        user, hash = kv.key.removeprefix(BLOCKS_PREFIX).split("/")
        blocks.append((user, hash, kv.json()["stored_on"]))
    orders = []
    for kv in await etcd.get_prefix(ORDERS_PREFIX):
        node_id = kv.key.removeprefix(ORDERS_PREFIX).split("/")[0]
        orders.append((node_id, kv.json()))
    return build_admin_status(
        leader.value.decode() if leader else None,
        [meta.model_dump() for meta in metaservers],
        await live_nodes(etcd),
        blocks,
        orders,
    )


def build_admin_status(
    leader: str | None,
    metaservers: list[dict],
    nodes: list[Node],
    blocks: list[tuple[str, str, list[str]]],
    orders: list[tuple[str, dict]],
) -> AdminStatusResponse:
    """Arma /admin/status con lo leído de etcd.

    nodes: los DataNodes vivos. blocks: (user, hash, stored_on) de cada bloque. orders: (node_id, orden).
    - datanodes: los vivos y, como caídos, los node_id que aparecen en algún stored_on sin clave
      viva (sus campos en null). Todos ordenados por node_id.
    - under_replicated: los bloques con menos de 3 copias vivas, con las vivas en el orden de stored_on.
    """
    alive = by_id(nodes)
    known = set(alive) | {node_id for _, _, stored_on in blocks for node_id in stored_on}
    datanodes = []
    for node_id in sorted(known):
        node = alive.get(node_id)
        if node is None:
            datanodes.append(DataNodeStatus(node_id=node_id, address=None, zone=None, free_bytes=None, alive=False))
        else:
            datanodes.append(
                DataNodeStatus(
                    node_id=node_id, address=node.public_address, zone=node.zone, free_bytes=node.free_bytes, alive=True
                )
            )
    under_replicated = []
    for user, hash, stored_on in blocks:
        alive_on = [node_id for node_id in stored_on if node_id in alive]
        if len(alive_on) < COPIES:
            under_replicated.append(UnderReplicatedBlock(user=user, hash=hash, alive_on=alive_on))
    return AdminStatusResponse(
        leader=leader,
        metaservers=metaservers,
        datanodes=datanodes,
        under_replicated=under_replicated,
        pending_orders=[PendingOrder(node_id=node_id, order=order) for node_id, order in orders],
    )
