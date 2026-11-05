"""Pruebas de la tarea 13: GET /api/v1/admin/status (docs/contratos.md, sección 2; DA-16)."""

from dfs_common.models import AdminStatusResponse
from metaserver.nodes import Node
from metaserver.status import build_admin_status

from fakes import DA13, FakeEtcd, add_block, add_node, bearer, public_client

H0 = "6ce5a55b446b43e4e935c44695a9a09f161e8e87d1ff2359adebc5f9ee5ee5cf"
H1 = "b64bb9046f61749097db060781bcbbba317c08ff0ac378e9aed9a6d18217e449"
H2 = "8de70b36bbfaaf4bd8fecc81370e32e388e0fa57cabc32a1fbd46777ac7ac937"
EXAMPLE = AdminStatusResponse.model_config["json_schema_extra"]["examples"][0]
STATUS = "/api/v1/admin/status"


def contract_state(*, leader: bytes | None = b"meta2") -> FakeEtcd:
    """El estado del ejemplo: poco después de caer dn4, con dos bloques en 2 copias y dos órdenes copy."""
    etcd = FakeEtcd()
    for meta in EXAMPLE["metaservers"]:
        etcd.seed(f"/nodes/meta/{meta['meta_id']}", {key: meta[key] for key in ("ip", "public_port", "internal_port")})
    if leader is not None:
        # La tarea 19 hace campaña con su meta_id como valor; la clave es {nombre}/{lease en hex}.
        etcd.seed("/election/leader/694d8a1c2e3b4f20", leader)
    for node in EXAMPLE["datanodes"]:
        if node["alive"]:
            add_node(etcd, node["node_id"], free_bytes=node["free_bytes"])
    add_block(etcd, "ana", H0, stored_on=["dn2", "dn3", "dn4"])
    add_block(etcd, "ana", H1, stored_on=["dn3", "dn4", "dn1"])
    add_block(etcd, "ana", H2, stored_on=["dn1", "dn2", "dn3"], size=4194304)  # tiene sus 3 copias
    for pending in EXAMPLE["pending_orders"]:
        etcd.seed(f"/orders/{pending['node_id']}/{pending['order']['order_id']}", pending["order"])
    return etcd


def status(etcd, *, user="admin", admin=True):
    with public_client(etcd) as client:
        return client.get(STATUS, headers=bearer(user, admin=admin))


def test_admin_status_is_the_contract_example():
    response = status(contract_state())
    assert response.status_code == 200, response.json()
    assert response.json() == EXAMPLE


def test_admin_status_requires_an_admin():
    assert status(contract_state(), user="ana", admin=False).json()["error"] == "E_FORBIDDEN"
    with public_client(contract_state()) as client:
        assert client.get(STATUS).json()["error"] == "E_AUTH"


def test_without_a_leader_it_is_null():
    assert status(contract_state(leader=None)).json()["leader"] is None


def test_build_admin_status_lists_dead_nodes_by_their_stored_on():
    """Función pura: un nodo caído solo se conoce porque aparece en algún stored_on."""
    nodes = [Node("dn3", DA13["dn3"][0], 9443, 9444, "us-east-1c", 5), Node("dn1", DA13["dn1"][0], 9443, 9444, "us-east-1a", 7)]
    blocks = [("ana", H0, ["dn3", "dn9", "dn1"]), ("bruno", H1, ["dn1", "dn8"])]
    result = build_admin_status(None, [], nodes, blocks, [])
    assert [(n.node_id, n.alive, n.address) for n in result.datanodes] == [
        ("dn1", True, "172.31.0.10:9443"),
        ("dn3", True, "172.31.32.10:9443"),
        ("dn8", False, None),
        ("dn9", False, None),
    ]
    # Menos de 3 copias vivas: los dos.
    assert [(b.user, b.hash, b.alive_on) for b in result.under_replicated] == [
        ("ana", H0, ["dn3", "dn1"]),
        ("bruno", H1, ["dn1"]),
    ]
