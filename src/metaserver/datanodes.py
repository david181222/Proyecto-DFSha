"""Registro y latidos de los DataNodes: POST /internal/register y /internal/heartbeat (tarea 13).

Un DataNode se registra al arrancar y después late cada DFS_HEARTBEAT_S (DA-11, DA-12). Su
clave /nodes/dn/{node_id} está atada a un lease de DFS_NODE_LEASE_S: si deja de latir, el
lease vence, la clave desaparece y el nodo cuenta como caído. En la respuesta de cada latido
recibe sus órdenes pendientes, y en el siguiente avisa cuáles cumplió (sección 7.3).

Decisiones de la tarea 13 (no cambian el contrato):

1. El latido comprueba, antes de renovar el lease, que /nodes/dn/{node_id} esté atada a ese
   lease (si no, 404 y el nodo se registra de nuevo) y que la llamada venga de la IP
   registrada (si no, 403). Así un nodo no puede renovar un lease ajeno, como el de otro nodo
   o el de un lock: la misma protección de la tarea 11.
2. Un disco nuevo (disk_id distinto del guardado) primero saca al nodo de todos los stored_on
   y después guarda el disk_id nuevo: si se corta a la mitad, el siguiente registro repite la
   limpieza (sección 3). Para eso recorre todo /blocks/, lo que es aceptable a la escala del
   proyecto.
"""

from dfs_common.config import Settings
from dfs_common.errors import DFSError, ErrorCode
from dfs_common.models import HeartbeatRequest, HeartbeatResponse, RegisterRequest, RegisterResponse
from metaserver.blocks import update_stored_on
from metaserver.etcd import Etcd, LeaseNotFound, lease_from_hex, lease_hex
from metaserver.keys import BLOCKS_PREFIX, datanode_key, disk_key, order_key, orders_prefix
from metaserver.nodes import check_source_ip


async def register_datanode(etcd: Etcd, body: RegisterRequest, source_ip: str, settings: Settings) -> RegisterResponse:
    """Guarda /nodes/dn/{node_id} atada a un lease nuevo de DFS_NODE_LEASE_S y responde cada cuánto latir.

    La IP de origen debe ser la del cuerpo y, si el nodo ya está vivo, la que tiene registrada:
    así un error de configuración (dos DataNodes con el mismo DFS_NODE_ID) no pisa el registro
    del otro. Si había una clave de un registro anterior, se sobrescribe; el lease viejo vence solo.
    """
    check_source_ip(source_ip, body.ip, body.node_id)
    current = await etcd.get(datanode_key(body.node_id))
    if current is not None:
        check_source_ip(source_ip, current.json()["ip"], body.node_id)
    await _check_disk(etcd, body.node_id, body.disk_id)

    lease_id = await etcd.lease_grant(settings.node_lease_s)
    node = body.model_dump(include={"ip", "public_port", "internal_port", "zone", "free_bytes"})
    await etcd.put(datanode_key(body.node_id), node, lease=lease_id)
    return RegisterResponse(lease_id=lease_hex(lease_id), heartbeat_s=settings.heartbeat_s)


async def heartbeat(etcd: Etcd, body: HeartbeatRequest, source_ip: str) -> HeartbeatResponse:
    """Renueva el lease, actualiza free_bytes, aplica lo que el nodo informa y responde sus órdenes.

    1. /nodes/dn/{node_id} debe estar atada a lease_id (si no, 404) y la llamada debe venir de su
       IP (si no, 403); después se renueva el lease (si ya venció, 404).
    2. Reescribe la clave con el free_bytes nuevo, en el mismo lease.
    3. done_orders: una copy cumplida agrega su destino a stored_on (si el bloque ya no existe, nada);
       después se borra la orden. Una orden que ya no existe se ignora.
    4. bad_blocks: quita este nodo de stored_on.
    5. Responde las órdenes que siguen pendientes en /orders/{node_id}/.
    """
    node_id = body.node_id
    lease_id = _lease_of(body.lease_id)
    current = await etcd.get(datanode_key(node_id))
    if current is None or current.lease != lease_id:
        raise _register_again(node_id)
    node = current.json()
    check_source_ip(source_ip, node["ip"], node_id)
    try:
        await etcd.lease_keepalive(lease_id)
        await etcd.put(datanode_key(node_id), {**node, "free_bytes": body.free_bytes}, lease=lease_id)
    except LeaseNotFound:
        raise _register_again(node_id) from None

    for order_id in body.done_orders:
        stored = await etcd.get(order_key(node_id, order_id))
        if stored is None:
            continue
        order = stored.json()
        if order["type"] == "copy":
            await update_stored_on(etcd, order["user"], order["hash"], add=[order["target"]["node_id"]])
        await etcd.delete(stored.key)
    for bad in body.bad_blocks:
        await update_stored_on(etcd, bad.user, bad.hash, remove=[node_id])

    pending = await etcd.get_prefix(orders_prefix(node_id))
    return HeartbeatResponse(orders=[kv.json() for kv in pending])


def _lease_of(lease_id: str) -> int:
    try:
        return lease_from_hex(lease_id)
    except LeaseNotFound:
        return 0  # no nombra ningún lease: ninguna clave está atada a 0


def _register_again(node_id: str) -> DFSError:
    return DFSError(ErrorCode.E_NOT_FOUND, f"el lease de {node_id} ya no existe; regístrate de nuevo")


async def _check_disk(etcd: Etcd, node_id: str, disk_id: str) -> None:
    """Compara disk_id con /nodes/disk/{node_id}. Si cambió, el disco es nuevo y sus copias ya no existen."""
    stored = await etcd.get(disk_key(node_id))
    if stored is not None and stored.json()["disk_id"] == disk_id:
        return
    if stored is not None:
        await _forget_copies(etcd, node_id)
    await etcd.put(disk_key(node_id), {"disk_id": disk_id})


async def _forget_copies(etcd: Etcd, node_id: str) -> None:
    """Quita node_id de todos los stored_on; la re-réplica repone esas copias (G5)."""
    for kv in await etcd.get_prefix(BLOCKS_PREFIX):
        if node_id in kv.json()["stored_on"]:
            user, hash = kv.key.removeprefix(BLOCKS_PREFIX).split("/")
            await update_stored_on(etcd, user, hash, remove=[node_id])
