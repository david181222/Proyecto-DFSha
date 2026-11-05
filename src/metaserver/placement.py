"""A qué DataNodes va cada bloque (DA-05). Función pura: recibe la lista de nodos y no lee etcd.

El primario y los secundarios se eligen por turnos, desde una posición que depende de un
inicio aleatorio por reserva (start) y del índice del bloque: así los bloques de un archivo
se reparten entre todos los DataNodes. Las 3 copias deben cubrir al menos 2 zonas (G1).
"""

from collections.abc import Collection

from dfs_common.errors import DFSError, ErrorCode
from metaserver.nodes import Node

COPIES = 3  # primario y 2 secundarios (DA-04)


def choose_nodes(
    nodes: list[Node], index: int, start: int, exclude: Collection[str], block_size: int, w: int = 2
) -> list[Node]:
    """Primario y secundarios del bloque index: 3 nodos, o 2 si solo quedan 2 en zonas distintas.

    nodes son los DataNodes vivos, ordenados por node_id. Primero se quitan los de exclude
    y los que tienen menos de block_size bytes libres. Del resto se toman 3 en círculo desde
    la posición (start + index) mod N; si no cubren 2 zonas, el tercero se cambia por el
    siguiente nodo de otra zona. El primero de la lista es el primario.

    Hacen falta al menos w nodos (DFS_W; las rutas lo pasan siempre). Si no se logran:
    507 E_NO_SPACE si el motivo es el espacio libre, y 503 E_UNAVAILABLE en otro caso.
    """
    candidates = [node for node in nodes if node.node_id not in exclude]
    with_space = [node for node in candidates if node.free_bytes >= block_size]
    chosen = _pick(with_space, index, start)
    if len(chosen) >= w:
        return chosen
    if len(_pick(candidates, index, start)) >= w:
        raise DFSError(ErrorCode.E_NO_SPACE, "los DataNodes vivos no tienen espacio para el bloque")
    raise DFSError(ErrorCode.E_UNAVAILABLE, f"no hay {w} DataNodes vivos que cubran 2 zonas")


def _pick(nodes: list[Node], index: int, start: int) -> list[Node]:
    """Hasta 3 nodos que cubren al menos 2 zonas, por turnos; [] si no se logran 2 zonas."""
    if not nodes:
        return []
    first = (start + index) % len(nodes)
    ring = nodes[first:] + nodes[:first]
    chosen = ring[:COPIES]
    if len({node.zone for node in chosen}) >= 2:
        return chosen
    # Todos en una misma zona: el tercero se cambia por el siguiente nodo de otra zona.
    other = next((node for node in ring[COPIES:] if node.zone != chosen[0].zone), None)
    if other is None:
        return []
    return [*chosen[: COPIES - 1], other]
