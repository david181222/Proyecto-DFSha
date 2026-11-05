"""DataNodes registrados en etcd (DA-11, DA-12).

Un DataNode vivo tiene su clave /nodes/dn/{node_id} = {ip, public_port, internal_port,
zone, free_bytes}, atada al lease de sus latidos: cuando deja de latir, el lease vence y
la clave desaparece. Por eso "vivo" quiere decir "tiene clave en /nodes/dn/".
"""

import ipaddress
from dataclasses import dataclass

from dfs_common.errors import DFSError, ErrorCode
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


def same_ip(first: str | None, second: str | None) -> bool:
    """Si las dos son la misma IP. Compara las direcciones, no el texto.

    Una IPv6 mapeada (::ffff:a.b.c.d, la que entrega un servidor que escucha en ::) cuenta como
    su IPv4. Si alguna no es una IP válida (o es None), no son la misma.
    """
    try:
        first_ip, second_ip = ipaddress.ip_address(first), ipaddress.ip_address(second)
    except ValueError:
        return False
    return _as_ipv4(first_ip) == _as_ipv4(second_ip)


def _as_ipv4(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    return getattr(ip, "ipv4_mapped", None) or ip


def check_source_ip(source_ip: str, expected_ip: str | None, node_id: str) -> None:
    """La API interna ata cada llamada a su nodo por la IP de origen (docs/contratos.md, sección 3).

    Todos los nodos tienen un certificado de la misma CA, así que el certificado no dice cuál
    es cuál. expected_ip None: el nodo no está vivo. Si no es la misma IP: 403 E_FORBIDDEN.
    La usan las tres rutas de la API interna: register, heartbeat y confirm.
    """
    if not same_ip(source_ip, expected_ip):
        raise DFSError(ErrorCode.E_FORBIDDEN, f"la llamada no viene de la IP de {node_id or 'ningún nodo'}")
