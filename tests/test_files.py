"""Pruebas de la tarea 12: GET /api/v1/files/blocks (docs/contratos.md, secciones 2 y 13)."""

import time

import pytest

from dfs_common.security import verify_pass
from metaserver.files import order_replicas
from metaserver.nodes import Node

from fakes import DA13, PASS_KEY, FakeEtcd, add_block, add_dir, add_node, bearer, public_client

MIB = 1024 * 1024
BLOCK = 8 * MIB
H0 = "6ce5a55b446b43e4e935c44695a9a09f161e8e87d1ff2359adebc5f9ee5ee5cf"
H1 = "b64bb9046f61749097db060781bcbbba317c08ff0ac378e9aed9a6d18217e449"
H2 = "8de70b36bbfaaf4bd8fecc81370e32e388e0fa57cabc32a1fbd46777ac7ac937"
PATH = "/docs/informe.pdf"
FILE_ID = "3f0c9a7e-1b2d-4c5e-8f9a-0b1c2d3e4f50"
SHA = "247a8174ec0c61e4f6c2f0299b27648c9ef3dc8482f7083d01c8a389976a6a76"
NODES = {node_id: Node(node_id, ip, 9443, 9444, zone, 1) for node_id, (ip, zone) in DA13.items()}


def manifest(hashes, size, *, replaced_at=None, file_sha256=SHA) -> dict:
    return {
        "user": "ana",
        "size": size,
        "block_size": BLOCK,
        "hashes": hashes,
        "file_sha256": file_sha256,
        "created_at": 1793628090000,
        "replaced_at": replaced_at,
    }


def contract_example(*, dead=()) -> FakeEtcd:
    """El estado del ejemplo de GET /files/blocks: la versión 4 de 20 MiB en tres bloques."""
    etcd = FakeEtcd()
    for node_id in DA13:
        if node_id not in dead:
            add_node(etcd, node_id)
    add_dir(etcd, "ana", "/docs")
    etcd.seed(f"/ns/ana{PATH}", {"type": "file", "file_id": FILE_ID, "version": 4})
    etcd.seed(f"/files/{FILE_ID}/v/4", manifest([H0, H1, H2], 20 * MIB))
    add_block(etcd, "ana", H0, stored_on=["dn2", "dn3", "dn4"])
    add_block(etcd, "ana", H1, stored_on=["dn3", "dn4", "dn1"])
    add_block(etcd, "ana", H2, stored_on=["dn1", "dn2", "dn3"], size=4 * MIB)
    return etcd


def read(etcd, path=PATH, *, version=None, zone="us-east-1a", **settings):
    params = {"path": path} if version is None else {"path": path, "version": version}
    with public_client(etcd, **settings) as client:
        return client.get("/api/v1/files/blocks", params=params, headers={**bearer(), "X-Client-Zone": zone})


def assert_error(response, status: int, code: str) -> None:
    assert response.status_code == status, response.json()
    assert response.json()["error"] == code


def test_answers_like_the_contract_example():
    body = read(contract_example()).json()
    assert {key: body[key] for key in ("path", "version", "size", "block_size", "file_sha256")} == {
        "path": PATH,
        "version": 4,
        "size": 20 * MIB,
        "block_size": BLOCK,
        "file_sha256": SHA,
    }
    # Las mismas réplicas del ejemplo: primero la zona del cliente (us-east-1a), después en el orden de stored_on.
    assert [(b["index"], b["size"], b["stored_on"], [r["node_id"] for r in b["replicas"]]) for b in body["blocks"]] == [
        (0, BLOCK, ["dn2", "dn3", "dn4"], ["dn2", "dn3", "dn4"]),
        (1, BLOCK, ["dn3", "dn4", "dn1"], ["dn1", "dn3", "dn4"]),
        (2, 4 * MIB, ["dn1", "dn2", "dn3"], ["dn1", "dn2", "dn3"]),
    ]
    assert body["blocks"][1]["replicas"][0] == {"node_id": "dn1", "address": "172.31.0.10:9443"}  # puerto público


def test_each_block_brings_a_valid_read_pass():
    body = read(contract_example(), pass_ttl_s=77).json()
    for block in body["blocks"]:
        claims = verify_pass(block["pass"], PASS_KEY, hash=block["hash"], op="r")
        assert claims.user == "ana" and claims.secondaries is None
        assert abs(claims.exp - (time.time() + 77)) <= 2


def test_dead_datanodes_are_left_out_of_replicas_but_not_of_stored_on():
    body = read(contract_example(dead=("dn4", "dn1"))).json()
    assert [b["stored_on"] for b in body["blocks"]] == [["dn2", "dn3", "dn4"], ["dn3", "dn4", "dn1"], ["dn1", "dn2", "dn3"]]
    assert [[r["node_id"] for r in b["replicas"]] for b in body["blocks"]] == [["dn2", "dn3"], ["dn3"], ["dn2", "dn3"]]

    nobody = read(contract_example(dead=tuple(DA13))).json()
    assert all(block["replicas"] == [] for block in nobody["blocks"])  # el cliente no puede leerlo por ahora


def test_a_retained_version_can_be_read_and_an_expired_one_is_404():
    etcd = contract_example()
    now = time.time_ns() // 1_000_000
    etcd.seed(f"/files/{FILE_ID}/v/3", manifest([H0], BLOCK, replaced_at=now - 30_000))
    etcd.seed(f"/files/{FILE_ID}/v/2", manifest([H0], BLOCK, replaced_at=now - 90_000))
    retained = read(etcd, version=3, retention_s=60)
    assert retained.status_code == 200
    assert (retained.json()["version"], retained.json()["size"]) == (3, BLOCK)
    assert_error(read(etcd, version=2, retention_s=60), 404, "E_NOT_FOUND")  # pasó DFS_RETENTION_S
    assert_error(read(etcd, version=1), 404, "E_NOT_FOUND")  # ya no está
    assert_error(read(etcd, version=5), 404, "E_NOT_FOUND")  # todavía no existe
    assert read(etcd, version=4).json()["version"] == 4  # la actual, pedida por número


def test_after_rm_it_is_404_even_if_the_version_is_still_retained():
    etcd = contract_example()
    with public_client(etcd) as client:
        assert client.delete("/api/v1/fs/rm", params={"path": PATH}, headers=bearer()).json() == {"ok": True}
    assert etcd.value(f"/files/{FILE_ID}/v/4")["replaced_at"] is not None  # sigue retenida
    assert_error(read(etcd, version=4), 404, "E_NOT_FOUND")


def test_a_version_published_with_a_or_rw_has_no_file_sha256():
    etcd = contract_example()
    etcd.seed(f"/files/{FILE_ID}/v/4", manifest([H0, H1, H2], 20 * MIB, file_sha256=None))
    assert read(etcd).json()["file_sha256"] is None


def test_errors():
    etcd = contract_example()
    assert_error(read(etcd, "/docs"), 400, "E_BAD_REQUEST")
    assert_error(read(etcd, "/"), 400, "E_BAD_REQUEST")
    assert_error(read(etcd, "/docs/nada.pdf"), 404, "E_NOT_FOUND")
    with public_client(etcd) as client:
        assert_error(client.get("/api/v1/files/blocks", params={"path": PATH}), 401, "E_AUTH")


@pytest.mark.parametrize(
    ("zone", "expected"),
    [
        ("us-east-1b", ["dn4", "dn2", "dn3", "dn1"]),
        ("us-east-1a", ["dn1", "dn4", "dn3", "dn2"]),
        (None, ["dn4", "dn3", "dn1", "dn2"]),
    ],
)
def test_order_replicas_puts_the_client_zone_first_and_keeps_the_order_of_stored_on(zone, expected):
    stored_on = ["dn4", "dn3", "dn9", "dn1", "dn2"]  # dn9 no está vivo
    assert [replica.node_id for replica in order_replicas(stored_on, NODES, zone)] == expected
