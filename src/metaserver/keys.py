"""Claves de etcd del MetaServer (docs/contratos.md, sección 8), escritas en un solo lugar."""


def user_key(user: str) -> str:
    """Usuario: {password_hash, is_admin}."""
    return f"/users/{user}"


def ns_key(user: str, path: str) -> str:
    """Entrada de un archivo o directorio del espacio del usuario. La raíz / no tiene clave."""
    if path == "/":
        raise ValueError("la raíz / no se guarda en etcd")
    return f"/ns/{user}{path}"


def children_prefix(user: str, path: str) -> str:
    """Prefijo de todo lo que hay dentro del directorio path, también de la raíz: /ns/ana/docs/ o /ns/ana/."""
    return f"/ns/{user}{path.rstrip('/')}/"


def manifest_key(file_id: str, version: int) -> str:
    """Manifiesto de la versión version de un archivo."""
    return f"/files/{file_id}/v/{version}"


def lock_name(user: str, path: str) -> str:
    """Nombre del lock de etcd de un archivo; cada intento queda en {lock_name}/{lock_id} (DA-08)."""
    return f"/locks/{user}{path}"


def block_key(user: str, hash: str) -> str:
    """Bloque de un usuario: {size, stored_on, touched_at} (DA-02)."""
    return f"/blocks/{user}/{hash}"


def upload_key(upload_id: str) -> str:
    """Subida en curso: {user, path, mode, base_version, block_size, lock_key, blocks, created_at}."""
    return f"/uploads/{upload_id}"


BLOCKS_PREFIX = "/blocks/"

# DataNodes vivos: /nodes/dn/{node_id} = {ip, public_port, internal_port, zone, free_bytes}, atada a su lease.
DATANODES_PREFIX = "/nodes/dn/"

# MetaServers vivos: /nodes/meta/{meta_id} = {ip, public_port, internal_port}, atada a su lease.
METASERVERS_PREFIX = "/nodes/meta/"

# Prefijo de las órdenes pendientes: /orders/{node_id}/{order_id} (sección 7.3).
ORDERS_PREFIX = "/orders/"

# Nombre de la elección de líder (DA-12). Cada candidato hace campaña con su meta_id como valor (tarea 19).
ELECTION_NAME = "/election/leader"


def datanode_key(node_id: str) -> str:
    return f"{DATANODES_PREFIX}{node_id}"


def metaserver_key(meta_id: str) -> str:
    return f"{METASERVERS_PREFIX}{meta_id}"


def disk_key(node_id: str) -> str:
    """El disco de datos de un DataNode: {disk_id}. Sin lease: sobrevive a la caída del nodo."""
    return f"/nodes/disk/{node_id}"


def orders_prefix(node_id: str) -> str:
    """Órdenes pendientes de un DataNode."""
    return f"{ORDERS_PREFIX}{node_id}/"


def order_key(node_id: str, order_id: str) -> str:
    return f"{orders_prefix(node_id)}{order_id}"
