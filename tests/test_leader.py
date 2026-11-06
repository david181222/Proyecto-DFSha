"""Pruebas de la tarea 19: el líder (src/metaserver/leader.py).

- plan_repairs y plan_trim, una prueba por regla, sin etcd y con la hora como parámetro.
- La vuelta del líder (repair_round) contra FakeEtcd: qué escribe en /orders/ y en /blocks/.
- La elección con dos MetaServers sobre FakeEtcd, con un reloj falso: las esperas del
  sistema (DFS_LEADER_GRACE_S y compañía) no se esperan de verdad.
"""

import asyncio
import logging

import pytest

from dfs_common.config import Settings
from metaserver import leader
from metaserver.leader import Block, LeaderState, PendingOrder, Snapshot, plan_repairs, plan_trim, repair_round
from metaserver.nodes import Node

from fakes import DA13, FakeEtcd, add_block, add_node

H1 = "6ce5a55b446b43e4e935c44695a9a09f161e8e87d1ff2359adebc5f9ee5ee5cf"
H2 = "b64bb9046f61749097db060781bcbbba317c08ff0ac378e9aed9a6d18217e449"
MIB = 1024 * 1024
WON = 1_793_628_000.0  # cuándo ganó el líder, en segundos
AFTER_GRACE = WON + 300
SETTINGS = Settings(_env_file=None, leader_grace_s=300, rereplicate_after_s=60, gc_clock_margin_s=1)


def node(node_id: str, *, zone: str | None = None, free_bytes: int = 15 * 1024 * MIB) -> Node:
    ip, default_zone = DA13.get(node_id, ("10.0.0.99", "us-east-1c"))
    return Node(node_id, ip, 9443, 9444, zone or default_zone, free_bytes)


def nodes(*node_ids: str) -> dict[str, Node]:
    return {node_id: node(node_id) for node_id in node_ids}


def block(stored_on: list[str], *, hash: str = H1, size: int = 8 * MIB, rev: int = 10) -> Block:
    return Block("ana", hash, {"size": size, "stored_on": stored_on, "touched_at": 1793628000000}, rev)


def snap(alive: dict[str, Node], *blocks: Block, orders: list[PendingOrder] = ()) -> Snapshot:
    return Snapshot(nodes=alive, blocks=list(blocks), orders=list(orders))


def copy_order(origin: str, target: str, *, hash: str = H1) -> PendingOrder:
    address = f"{DA13[target][0]}:9444"
    order = {"order_id": "o-1", "type": "copy", "user": "ana", "hash": hash, "target": {"node_id": target, "address": address}}
    return PendingOrder(key=f"/orders/{origin}/o-1", node_id=origin, order=order)


def plan(snapshot: Snapshot, now: float = AFTER_GRACE, missing_since: dict | None = None):
    return plan_repairs(snapshot, now, SETTINGS, missing_since or {}, WON, new_id=lambda: "nuevo")


def copies(result) -> list[tuple[str, str]]:
    """(origen, destino) de cada orden copy del plan."""
    return [(origin, order["target"]["node_id"]) for origin, order in result.copies]


# =====================================================================
# plan_repairs: una prueba por regla
# =====================================================================


def test_rule_1_nothing_during_the_new_leader_grace():
    snapshot = snap(nodes("dn1", "dn2", "dn3", "dn4"), block(["dn1", "dn2"]))
    assert copies(plan(snapshot, now=WON + 299)) == []
    assert copies(plan(snapshot, now=WON + 300)) == [("dn1", "dn3")]


def test_rule_1_absent_nodes_are_counted_during_the_grace():
    snapshot = snap(nodes("dn1", "dn2", "dn4"), block(["dn1", "dn2", "dn3"]))
    assert plan(snapshot, now=WON + 10).missing_since == {"dn3": WON + 10}


def test_rule_2_missing_since_keeps_the_first_time_and_forgets_nodes_that_return():
    absent = snap(nodes("dn1", "dn2", "dn4"), block(["dn1", "dn2", "dn3"]))
    assert plan(absent, missing_since={"dn3": WON + 5}).missing_since == {"dn3": WON + 5}
    back = snap(nodes("dn1", "dn2", "dn3", "dn4"), block(["dn1", "dn2", "dn3"]))
    result = plan(back, missing_since={"dn3": WON + 5})
    assert result.missing_since == {}
    assert result.returned == {"dn3"}


def test_rule_3_absent_node_waits_rereplicate_after_and_then_gets_a_copy():
    snapshot = snap(nodes("dn1", "dn2", "dn4"), block(["dn1", "dn2", "dn3"]))
    seen = AFTER_GRACE
    assert copies(plan(snapshot, now=seen)) == []  # recién visto ausente
    assert copies(plan(snapshot, now=seen + 59, missing_since={"dn3": seen})) == []
    result = plan(snapshot, now=seen + 60, missing_since={"dn3": seen})
    assert copies(result) == [("dn1", "dn4")]
    origin, order = result.copies[0]
    assert order == {
        "order_id": "nuevo",
        "type": "copy",
        "user": "ana",
        "hash": H1,
        "target": {"node_id": "dn4", "address": "172.31.16.20:9444"},  # dirección interna del destino
    }


def test_rule_3_third_copy_of_a_block_confirmed_with_two():
    snapshot = snap(nodes("dn1", "dn2", "dn3", "dn4"), block(["dn1", "dn2"]))
    assert copies(plan(snapshot)) == [("dn1", "dn3")]


def test_rule_3_a_block_with_three_live_copies_needs_nothing():
    # dn3 está caído pero sigue en stored_on; dn4 ya tiene la copia que lo reemplaza (regla 8).
    snapshot = snap(nodes("dn1", "dn2", "dn4"), block(["dn1", "dn2", "dn3", "dn4"]))
    result = plan(snapshot, missing_since={"dn3": WON})
    assert (result.copies, result.trims) == ([], [])


def test_rule_4_no_duplicate_when_a_copy_order_is_pending():
    snapshot = snap(nodes("dn1", "dn2", "dn3", "dn4"), block(["dn1", "dn2"]), orders=[copy_order("dn1", "dn3")])
    result = plan(snapshot)
    assert (result.copies, result.stale_orders) == ([], [])


@pytest.mark.parametrize(
    ("alive", "stored_on", "order"),
    [
        (("dn1", "dn2", "dn4"), ["dn1", "dn2"], copy_order("dn1", "dn3")),  # el destino cayó
        (("dn2", "dn3", "dn4"), ["dn1", "dn2"], copy_order("dn1", "dn3")),  # el origen cayó
        (("dn1", "dn2", "dn3", "dn4"), ["dn2", "dn4"], copy_order("dn1", "dn3")),  # el origen ya no tiene el bloque
    ],
    ids=["destino-caido", "origen-caido", "origen-sin-bloque"],
)
def test_rule_4_a_stuck_copy_order_is_deleted_and_replaced_in_the_next_round(alive, stored_on, order):
    now = AFTER_GRACE + 0.5
    result = plan(snap(nodes(*alive), block(stored_on), orders=[order]), now=now)
    [stale] = result.stale_orders
    assert stale.pending == order
    assert result.copies == []  # la de reemplazo sale en la vuelta siguiente (sección 7.3)
    # El destino no está en stored_on: por si alcanzó a recibir la copia, una delete con la regla del instante T.
    assert stale.target == "dn3"
    assert stale.cleanup == {
        "order_id": "nuevo", "type": "delete", "user": "ana", "hash": H1, "written_before_ms": int(now * 1000) - 1000
    }
    assert len(plan(snap(nodes(*alive), block(stored_on)), now=now).copies) == 1


def test_rule_4_no_delete_for_a_target_that_is_in_stored_on():
    # El origen cayó, pero el destino ya tiene una copia registrada: no se le manda delete.
    result = plan(snap(nodes("dn2", "dn3", "dn4"), block(["dn2", "dn3"]), orders=[copy_order("dn1", "dn3")]))
    assert [stale.cleanup for stale in result.stale_orders] == [None]


def test_rule_4_a_copy_order_of_a_block_that_no_longer_exists_is_deleted():
    result = plan(snap(nodes("dn1", "dn2", "dn3"), orders=[copy_order("dn1", "dn3")]))
    [stale] = result.stale_orders
    assert (stale.pending.key, stale.block, stale.cleanup["type"]) == ("/orders/dn1/o-1", None, "delete")


def test_rule_5_origin_is_the_first_live_node_of_stored_on():
    snapshot = snap(nodes("dn2", "dn3", "dn4"), block(["dn1", "dn3", "dn2"]))
    assert copies(plan(snapshot, missing_since={"dn1": WON})) == [("dn3", "dn4")]


def test_rule_5_prefers_a_zone_the_live_copies_do_not_cover():
    # dn2 va antes en node_id, pero está en la zona a, que ya cubre dn1; dn9 está en la c.
    alive = {"dn1": node("dn1"), "dn2": node("dn2", zone="us-east-1a"), "dn3": node("dn3"), "dn9": node("dn9", zone="us-east-1c")}
    alive["dn3"] = node("dn3", zone="us-east-1b")
    assert copies(plan(snap(alive, block(["dn1", "dn3"])))) == [("dn1", "dn9")]


def test_rule_5_ties_go_to_the_smallest_node_id():
    # dn2 y dn4 están los dos en la zona b, que no cubren dn1 (a) ni dn3 (c).
    assert copies(plan(snap(nodes("dn1", "dn2", "dn3", "dn4"), block(["dn1", "dn3"])))) == [("dn1", "dn2")]


def test_rule_5_target_needs_free_space_for_the_block():
    alive = nodes("dn1", "dn2", "dn4")
    alive["dn3"] = node("dn3", free_bytes=8 * MIB - 1)
    assert copies(plan(snap(alive, block(["dn1", "dn2"])))) == [("dn1", "dn4")]


def test_rule_5_without_a_possible_target_nothing_is_ordered():
    # Zona b caída: quedan dn1 y dn3, y los dos ya tienen el bloque (DA-05).
    snapshot = snap(nodes("dn1", "dn3"), block(["dn1", "dn3"]))
    result = plan(snapshot)
    assert (result.copies, result.trims) == ([], [])


def test_rule_5_one_order_per_block_and_per_round():
    snapshot = snap(nodes("dn1", "dn2", "dn3", "dn4"), block(["dn1"]), block(["dn2", "dn3"], hash=H2))
    assert copies(plan(snapshot)) == [("dn1", "dn2"), ("dn2", "dn1")]


def test_rule_7_extra_copy_removes_the_node_that_returned():
    snapshot = snap(nodes("dn1", "dn2", "dn3", "dn4"), block(["dn1", "dn2", "dn3", "dn4"]))
    now = AFTER_GRACE + 0.5
    result = plan(snapshot, now=now, missing_since={"dn3": WON})
    assert [(trim.node_id, trim.order) for trim in result.trims] == [
        ("dn3", {"order_id": "nuevo", "type": "delete", "user": "ana", "hash": H1, "written_before_ms": int(now * 1000) - 1000})
    ]
    assert result.copies == []


def test_rule_7_without_knowing_who_returned_removes_the_last_that_leaves_two_zones():
    snapshot = snap(nodes("dn1", "dn2", "dn3", "dn4"), block(["dn1", "dn2", "dn3", "dn4"]))
    assert [trim.node_id for trim in plan(snapshot).trims] == ["dn4"]
    # Si el último dejaría una sola zona, se pasa al anterior.
    alive = {f"dn{i}": node(f"dn{i}", zone="us-east-1a") for i in (1, 2, 3)} | {"dn9": node("dn9", zone="us-east-1c")}
    assert plan_trim(block(["dn1", "dn2", "dn3", "dn9"]), alive, set(), AFTER_GRACE, SETTINGS).node_id == "dn3"


def test_rule_8_absent_nodes_stay_in_stored_on():
    snapshot = snap(nodes("dn1", "dn2", "dn4"), block(["dn1", "dn2", "dn3"]))
    result = plan(snapshot, now=AFTER_GRACE + 60, missing_since={"dn3": AFTER_GRACE})
    assert result.trims == []  # la copia se agrega; nadie quita dn3


# =====================================================================
# La vuelta del líder contra FakeEtcd
# =====================================================================

def etcd_with(*node_ids: str) -> FakeEtcd:
    etcd = FakeEtcd()
    for node_id in node_ids:
        add_node(etcd, node_id)
    return etcd


@pytest.mark.anyio
async def test_round_writes_copy_orders_under_the_origin():
    etcd = etcd_with("dn1", "dn2", "dn3", "dn4")
    add_block(etcd, "ana", H1, stored_on=["dn1", "dn2"])
    await repair_round(etcd, SETTINGS, LeaderState(won_at=WON), AFTER_GRACE)
    [key] = etcd.keys("/orders/")
    assert key.startswith("/orders/dn1/")
    order = etcd.value(key)
    assert key == f"/orders/dn1/{order['order_id']}"
    assert order["target"] == {"node_id": "dn3", "address": "172.31.32.10:9444"}


@pytest.mark.anyio
async def test_round_remembers_missing_nodes_between_rounds():
    etcd = etcd_with("dn1", "dn2", "dn4")
    add_block(etcd, "ana", H1, stored_on=["dn1", "dn2", "dn3"])
    state = LeaderState(won_at=WON)
    await repair_round(etcd, SETTINGS, state, AFTER_GRACE)
    assert etcd.keys("/orders/") == []
    await repair_round(etcd, SETTINGS, state, AFTER_GRACE + 60)
    assert len(etcd.keys("/orders/dn1/")) == 1


@pytest.mark.anyio
async def test_round_trims_the_extra_copy_in_one_transaction():
    etcd = etcd_with("dn1", "dn2", "dn3", "dn4")
    add_block(etcd, "ana", H1, stored_on=["dn1", "dn2", "dn3", "dn4"])
    state = LeaderState(won_at=WON, missing_since={"dn3": WON})
    now = AFTER_GRACE + 0.25
    await repair_round(etcd, SETTINGS, state, now)
    assert etcd.value(f"/blocks/ana/{H1}")["stored_on"] == ["dn1", "dn2", "dn4"]
    [key] = etcd.keys("/orders/")
    assert key.startswith("/orders/dn3/")
    assert etcd.value(key)["written_before_ms"] == int(now * 1000) - 1000


@pytest.mark.anyio
async def test_round_deletes_stuck_orders_and_the_next_round_replaces_them():
    etcd = etcd_with("dn1", "dn2", "dn4")
    add_block(etcd, "ana", H1, stored_on=["dn1", "dn2"])
    etcd.seed("/orders/dn1/viejo", copy_order("dn1", "dn3").order)  # dn3 no está vivo
    state = LeaderState(won_at=WON)

    await repair_round(etcd, SETTINGS, state, AFTER_GRACE)
    [cleanup] = etcd.keys("/orders/")  # la atascada ya no está; dn3 recibirá una delete al volver
    assert cleanup.startswith("/orders/dn3/")
    assert etcd.value(cleanup)["type"] == "delete"
    assert etcd.value(cleanup)["written_before_ms"] == int(AFTER_GRACE * 1000) - 1000

    await repair_round(etcd, SETTINGS, state, AFTER_GRACE + 10)
    [copy] = etcd.keys("/orders/dn1/")
    assert etcd.value(copy)["target"]["node_id"] == "dn4"


@pytest.mark.anyio
async def test_stuck_order_that_a_heartbeat_just_completed_is_left_alone(monkeypatch):
    # Origen caído justo después de copiar a dn3: su último latido cumple la orden entre la
    # lectura del líder y su transacción (agrega dn3 a stored_on y borra la orden).
    etcd = etcd_with("dn2", "dn3", "dn4")
    add_block(etcd, "ana", H1, stored_on=["dn1", "dn2"])
    etcd.seed("/orders/dn1/viejo", copy_order("dn1", "dn3").order)
    real_txn = etcd.txn

    async def heartbeat_first(compare, success, failure):
        etcd.seed(f"/blocks/ana/{H1}", {**etcd.value(f"/blocks/ana/{H1}"), "stored_on": ["dn1", "dn2", "dn3"]})
        await etcd.delete("/orders/dn1/viejo")
        return await real_txn(compare, success, failure)

    monkeypatch.setattr(etcd, "txn", heartbeat_first)
    await repair_round(etcd, SETTINGS, LeaderState(won_at=WON), AFTER_GRACE)

    assert etcd.keys("/orders/") == []  # no salió una delete que borraría la copia registrada en dn3


@pytest.mark.anyio
@pytest.mark.parametrize(("interferences", "txns", "trimmed"), [(1, 2, True), (5, 3, False)])
async def test_trim_retries_when_the_block_changed_at_most_three_times(monkeypatch, interferences, txns, trimmed):
    etcd = etcd_with("dn1", "dn2", "dn3", "dn4")
    add_block(etcd, "ana", H1, stored_on=["dn1", "dn2", "dn3", "dn4"])
    real_txn, calls = etcd.txn, []

    async def touched_in_between(compare, success, failure):
        calls.append(1)
        if len(calls) <= interferences:
            # Otra escritura (por ejemplo, una reserva que toca touched_at) se cruza antes de la transacción.
            value = etcd.value(f"/blocks/ana/{H1}")
            etcd.seed(f"/blocks/ana/{H1}", {**value, "touched_at": value["touched_at"] + 1})
        return await real_txn(compare, success, failure)

    monkeypatch.setattr(etcd, "txn", touched_in_between)
    await repair_round(etcd, SETTINGS, LeaderState(won_at=WON, missing_since={"dn3": WON}), AFTER_GRACE)

    assert len(calls) == txns
    stored_on = etcd.value(f"/blocks/ana/{H1}")["stored_on"]
    assert (stored_on == ["dn1", "dn2", "dn4"]) is trimmed
    assert (len(etcd.keys("/orders/dn3/")) == 1) is trimmed


# =====================================================================
# Elección con dos MetaServers
# =====================================================================


class FakeClock:
    def __init__(self, now: float) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


async def no_wait(_seconds: float) -> None:
    """Reemplaza las esperas del sistema: cede el turno sin esperar los segundos pedidos."""
    await asyncio.sleep(0.001)


async def eventually(condition, timeout_s: float = 3.0) -> None:
    for _ in range(int(timeout_s / 0.005)):
        if await condition():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("la condición no se cumplió a tiempo")


def candidate(etcd: FakeEtcd, meta_id: str, clock: FakeClock) -> asyncio.Task:
    settings = Settings(_env_file=None, node_id=meta_id, leader_grace_s=300, rereplicate_after_s=60)
    return asyncio.create_task(leader.run_leader(etcd, settings, clock=clock, sleep=no_wait))


async def leader_is(etcd: FakeEtcd, meta_id: str) -> bool:
    current = await etcd.leader("/election/leader")
    return current is not None and current.value == meta_id.encode()


@pytest.mark.anyio
async def test_only_one_leader_and_the_next_one_starts_its_own_grace(caplog):
    caplog.set_level(logging.INFO, logger="dfsha")
    etcd = etcd_with("dn1", "dn2", "dn3", "dn4")
    add_block(etcd, "ana", H1, stored_on=["dn1", "dn2"])  # necesita su tercera copia
    clock = FakeClock(WON)
    first = candidate(etcd, "meta1", clock)
    await eventually(lambda: leader_is(etcd, "meta1"))
    second = candidate(etcd, "meta2", clock)
    try:
        await eventually(lambda: _candidates(etcd, 2))
        assert await leader_is(etcd, "meta1")  # meta2 espera en la fila

        # meta1 pierde su lease cerca del final de su espera; meta2 gana en ese instante.
        clock.now = WON + 299
        meta1_lease = (await etcd.leader("/election/leader")).lease
        await etcd.lease_revoke(meta1_lease)
        await eventually(lambda: leader_is(etcd, "meta2"))
        # etcd ya lo muestra como líder; se espera a que meta2 se entere y anote won_at.
        await eventually(lambda: _logged(caplog, "meta2 ganó la elección"))

        # La espera de meta1 ya habría terminado, pero meta2 cuenta la suya desde que ganó.
        clock.now = WON + 350
        await asyncio.sleep(0.05)
        assert etcd.keys("/orders/") == []

        clock.now = WON + 299 + 300
        await eventually(lambda: _has_orders(etcd))
        await eventually(lambda: _candidates(etcd, 2))  # meta1 volvió a la fila con otro lease
        assert await leader_is(etcd, "meta2")
    finally:
        await _stop(first, second)


@pytest.mark.anyio
async def test_shutting_down_resigns_and_the_next_candidate_wins():
    etcd = FakeEtcd()
    clock = FakeClock(WON)
    first = candidate(etcd, "meta1", clock)
    await eventually(lambda: leader_is(etcd, "meta1"))
    second = candidate(etcd, "meta2", clock)
    try:
        await eventually(lambda: _candidates(etcd, 2))
        await _stop(first)
        await eventually(lambda: leader_is(etcd, "meta2"))
    finally:
        await _stop(second)


async def _logged(caplog, text: str) -> bool:
    return any(text in record.getMessage() for record in caplog.records)


async def _candidates(etcd: FakeEtcd, count: int) -> bool:
    return len(etcd.keys("/election/leader/")) == count


async def _has_orders(etcd: FakeEtcd) -> bool:
    return bool(etcd.keys("/orders/"))


async def _stop(*tasks: asyncio.Task) -> None:
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
