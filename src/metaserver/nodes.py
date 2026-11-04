"""DataNodes registrados en etcd (DA-11, DA-12).

Un DataNode vivo tiene su clave /nodes/dn/{node_id} = {ip, public_port, internal_port,
zone, free_bytes}, atada al lease de sus latidos: cuando deja de latir, el lease vence y
la clave desaparece. Por eso "vivo" quiere decir "tiene clave en /nodes/dn/".
"""

from dataclasses import dataclass

from metaserver.etcd import Etcd
from metaserver.keys import DATANODES_PREFIX


@dataclass(frozen=True)
class Node:
    """Un DataNode vivo, como está en /nodes/dn/{node_id}."""

    node_id: str
    ip: str
    public_port: int
    internal_port: int
    zone: str
    free_bytes: int

    @property
    def public_address(self) -> str:
        """ip:puerto público (9443): el que se le entrega al cliente."""
        return f"{self.ip}:{self.public_port}"

    @property
    def internal_address(self) -> str:
        """ip:puerto interno (9444): el que usan los DataNodes entre sí."""
        return f"{self.ip}:{self.internal_port}"


async def live_nodes(etcd: Etcd) -> list[Node]:
    """Los DataNodes vivos, ordenados por node_id (el orden de las claves en etcd)."""
    return [
        Node(node_id=kv.key.removeprefix(DATANODES_PREFIX), **kv.json())
        for kv in await etcd.get_prefix(DATANODES_PREFIX)
    ]


def by_id(nodes: list[Node]) -> dict[str, Node]:
    return {node.node_id: node for node in nodes}
