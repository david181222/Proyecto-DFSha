"""Pruebas de la tarea 12: claves de bloque y POST /api/v1/internal/blocks/confirm (secciones 3 y 13).

confirm, update_stored_on y touch_block escriben la clave del bloque con una transacción que
compara la mod_revision leída; si otra escritura se cruza, lo intentan de nuevo (hasta 3 veces).
"""

import pytest

from dfs_common.errors import DFSError, ErrorCode
from metaserver.blocks import in_two_live_zones, live_zones, touch_block, update_stored_on
from metaserver.nodes import Node

from fakes import DA13, FakeEtcd, add_block, add_node, internal_client

H1 = "b64bb9046f61749097db060781bcbbba317c08ff0ac378e9aed9a6d18217e449"
KEY = f"/blocks/ana/{H1}"
CONFIRM = "/api/v1/internal/blocks/confirm"
NODES = {node_id: Node(node_id, ip, 9443, 9444, zone, 1) for node_id, (ip, zone) in DA13.items()}


def cluster(*node_ids: str) -> FakeEtcd:
    etcd = FakeEtcd()
    for node_id in node_ids or DA13:
        add_node(etcd, node_id)
    return etcd


class CrossingWrite(FakeEtcd):
    """Otro MetaServer escribe la clave del bloque justo antes de cada una de las primeras `times` transacciones."""

    def __init__(self, value: dict, times: int = 1) -> None:
        super().__init__()
        self.crossing, self.times = value, times

    async def txn(self, compare, success, failure):
        if self.times:
            self.times -= 1
            self.seed(KEY, self.crossing)
        return await super().txn(compare, success, failure)


def confirm(etcd, stored_on, *, ip=DA13["dn3"][0], size=8388608):
    with internal_client(etcd, ip=ip) as client:
        return client.post(CONFIRM, json={"user": "ana", "hash": H1, "size": size, "stored_on": stored_on})


# --- Zonas vivas (funciones puras) ---


def test_live_zones_count_only_nodes_that_are_alive():
    assert live_zones(["dn2", "dn4"], NODES) == {"us-east-1b"}
    assert live_zones(["dn3", "dn9", "dn1"], NODES) == {"us-east-1c", "us-east-1a"}  # dn9 no está vivo
    assert not in_two_live_zones(["dn2", "dn4"], NODES)
    assert in_two_live_zones(["dn3", "dn4"], NODES)
    assert not in_two_live_zones(["dn1", "dn9"], NODES)


# --- confirm ---


def test_confirm_creates_the_block_key():
    etcd = cluster()
    response = confirm(etcd, ["dn3", "dn4", "dn1"])
    assert response.json() == {"ok": True}
    value = etcd.value(KEY)
    assert value["size"] == 8388608 and value["stored_on"] == ["dn3", "dn4", "dn1"]
    assert value["touched_at"] > 1793628000000


def test_confirm_joins_stored_on_with_the_existing_key():
    etcd = cluster()
    add_block(etcd, "ana", H1, stored_on=["dn1", "dn2"])
    assert confirm(etcd, ["dn3", "dn1"]).status_code == 200
    assert etcd.value(KEY)["stored_on"] == ["dn1", "dn2", "dn3"]


@pytest.mark.parametrize(
    "stored_on", [["dn2", "dn4"], ["dn3", "dn9"]], ids=["una-zona", "un-nodo-caido"]
)
def test_confirm_without_two_live_zones_is_422_and_writes_nothing(stored_on):
    etcd = cluster()
    response = confirm(etcd, stored_on, ip=DA13[stored_on[0]][0])
    assert response.status_code == 422
    assert response.json()["error"] == "E_REPLICAS" and response.json()["retryable"] is True
    assert etcd.value(KEY) is None


@pytest.mark.parametrize(
    ("stored_on", "ip"),
    [(["dn3", "dn1"], DA13["dn1"][0]), (["dn9", "dn1"], "10.0.0.9"), ([], DA13["dn1"][0])],
    ids=["ip-de-otro-nodo", "primario-caido", "sin-primario"],
)
def test_confirm_from_an_ip_that_is_not_the_primary_is_403(stored_on, ip):
    etcd = cluster()
    response = confirm(etcd, stored_on, ip=ip)
    assert response.status_code == 403
    assert response.json()["error"] == "E_FORBIDDEN"
    assert etcd.value(KEY) is None


def test_confirm_tries_again_when_another_write_crosses():
    # Otro MetaServer agrega dn2 entre la lectura y la transacción: no se pierde.
    etcd = CrossingWrite({"size": 8388608, "stored_on": ["dn1", "dn2"], "touched_at": 1})
    for node_id in DA13:
        add_node(etcd, node_id)
    add_block(etcd, "ana", H1, stored_on=["dn1"])
    assert confirm(etcd, ["dn3", "dn1"]).status_code == 200
    assert etcd.value(KEY)["stored_on"] == ["dn1", "dn2", "dn3"]


def test_confirm_is_503_if_other_writes_keep_crossing():
    etcd = CrossingWrite({"size": 8388608, "stored_on": ["dn1"], "touched_at": 1}, times=3)
    for node_id in DA13:
        add_node(etcd, node_id)
    response = confirm(etcd, ["dn3", "dn1"])
    assert response.status_code == 503
    assert response.json()["error"] == "E_UNAVAILABLE"


# --- update_stored_on y touch_block (los usan los latidos, la reserva y el líder) ---


@pytest.mark.anyio
async def test_update_stored_on_adds_and_removes_without_repeating():
    etcd = FakeEtcd()
    add_block(etcd, "ana", H1, stored_on=["dn1", "dn2"], touched_at=7)
    assert await update_stored_on(etcd, "ana", H1, add=["dn2", "dn3"]) is True
    assert etcd.value(KEY)["stored_on"] == ["dn1", "dn2", "dn3"]
    assert await update_stored_on(etcd, "ana", H1, remove=["dn1"]) is True
    assert etcd.value(KEY) == {"size": 8388608, "stored_on": ["dn2", "dn3"], "touched_at": 7}


@pytest.mark.anyio
async def test_update_stored_on_and_touch_block_do_nothing_without_the_key():
    etcd = FakeEtcd()
    assert await update_stored_on(etcd, "ana", H1, add=["dn1"]) is False
    assert await touch_block(etcd, "ana", H1) is False
    assert etcd.keys("/blocks/") == []


@pytest.mark.anyio
async def test_update_stored_on_keeps_a_crossing_write():
    etcd = CrossingWrite({"size": 8388608, "stored_on": ["dn1", "dn4"], "touched_at": 1})
    add_block(etcd, "ana", H1, stored_on=["dn1"])
    await update_stored_on(etcd, "ana", H1, add=["dn3"])
    assert etcd.value(KEY)["stored_on"] == ["dn1", "dn4", "dn3"]


@pytest.mark.anyio
async def test_update_stored_on_gives_up_after_three_crossings():
    etcd = CrossingWrite({"size": 8388608, "stored_on": ["dn1"], "touched_at": 1}, times=3)
    add_block(etcd, "ana", H1, stored_on=["dn1"])
    with pytest.raises(DFSError) as caught:
        await update_stored_on(etcd, "ana", H1, add=["dn3"])
    assert caught.value.code == ErrorCode.E_UNAVAILABLE


@pytest.mark.anyio
async def test_touch_block_moves_touched_at_and_keeps_the_rest():
    etcd = FakeEtcd()
    add_block(etcd, "ana", H1, stored_on=["dn1", "dn3"], touched_at=7)
    assert await touch_block(etcd, "ana", H1) is True
    value = etcd.value(KEY)
    assert value["touched_at"] > 7 and value["stored_on"] == ["dn1", "dn3"]
