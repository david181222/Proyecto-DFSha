"""Pruebas de choose_nodes (tarea 12, DA-05): a qué DataNodes va cada bloque. Es una función pura."""

import pytest

from dfs_common.errors import DFSError, ErrorCode
from metaserver.nodes import Node
from metaserver.placement import choose_nodes

GIB = 1024**3
BLOCK = 8 * 1024 * 1024


def node(node_id: str, zone: str, free_bytes: int = 14 * GIB) -> Node:
    return Node(node_id, f"10.0.0.{node_id[-1]}", 9443, 9444, zone, free_bytes)


# La topología de DA-13: solo la zona b tiene dos DataNodes.
DA13 = [node("dn1", "us-east-1a"), node("dn2", "us-east-1b"), node("dn3", "us-east-1c"), node("dn4", "us-east-1b")]


def ids(nodes: list[Node]) -> list[str]:
    return [n.node_id for n in nodes]


def choose(nodes=DA13, *, index=0, start=0, exclude=(), block_size=BLOCK, w=2) -> list[str]:
    return ids(choose_nodes(nodes, index, start, exclude, block_size, w))


def error(**kwargs) -> ErrorCode:
    with pytest.raises(DFSError) as caught:
        choose(**kwargs)
    return caught.value.code


@pytest.mark.parametrize(
    ("start", "index", "expected"),
    [
        (0, 0, ["dn1", "dn2", "dn3"]),
        (0, 1, ["dn2", "dn3", "dn4"]),
        (1, 1, ["dn3", "dn4", "dn1"]),  # el bloque 1 del ejemplo de POST /uploads
        (1, 2, ["dn4", "dn1", "dn2"]),  # el bloque 2 del mismo ejemplo
        (3, 2, ["dn2", "dn3", "dn4"]),  # (3 + 2) mod 4 = 1
    ],
)
def test_rotates_from_start_plus_index(start, index, expected):
    assert choose(start=start, index=index) == expected


def test_every_trio_of_the_da13_topology_covers_two_zones():
    for start in range(4):
        for index in range(4):
            chosen = choose_nodes(DA13, index, start, (), BLOCK, 2)
            assert len(chosen) == 3
            assert len({n.zone for n in chosen}) >= 2


def test_a_trio_in_a_single_zone_swaps_the_third_for_the_next_node_of_another_zone():
    nodes = [node("a1", "z1"), node("a2", "z1"), node("a3", "z1"), node("b1", "z2"), node("c1", "z3")]
    assert choose(nodes) == ["a1", "a2", "b1"]


def test_exclude_leaves_those_nodes_out():
    # El ejemplo de realloc: sin dn4 quedan 3 nodos, y (1 + 2) mod 3 = 0.
    assert choose(index=2, start=1, exclude={"dn4"}) == ["dn1", "dn2", "dn3"]


def test_with_two_nodes_in_different_zones_it_uses_both():
    # La zona b caída (DA-05): un primario y un secundario.
    assert choose([DA13[0], DA13[2]], start=1) == ["dn3", "dn1"]


@pytest.mark.parametrize(
    "nodes",
    [[], [DA13[0]], [DA13[1], DA13[3]], [node("a1", "z1"), node("a2", "z1"), node("a3", "z1")]],
    ids=["ninguno", "uno", "dos-en-la-misma-zona", "tres-en-la-misma-zona"],
)
def test_without_two_zones_it_is_503(nodes):
    assert error(nodes=nodes) == ErrorCode.E_UNAVAILABLE


def test_nodes_without_space_are_left_out_and_if_that_is_the_reason_it_is_507():
    full = [node(n.node_id, n.zone, free_bytes=BLOCK - 1) for n in DA13]
    assert error(nodes=full) == ErrorCode.E_NO_SPACE
    # Con espacio solo en dn2 y dn4 (zona b), no se logran 2 zonas por falta de espacio.
    some = [full[0], DA13[1], full[2], DA13[3]]
    assert error(nodes=some) == ErrorCode.E_NO_SPACE
    # Con espacio en dn1 y dn2, se usan esos 2.
    assert choose([DA13[0], DA13[1], full[2], full[3]]) == ["dn1", "dn2"]


def test_w_3_needs_three_nodes():
    """Con DFS_W=3, el caso de DA-05 con 2 nodos no aplica (sección 2)."""
    assert error(nodes=[DA13[0], DA13[2]], w=3) == ErrorCode.E_UNAVAILABLE
    assert error(nodes=[DA13[0], DA13[1], node("dn3", "us-east-1c", free_bytes=0)], w=3) == ErrorCode.E_NO_SPACE
    assert choose(w=3) == ["dn1", "dn2", "dn3"]
