"""Pruebas de la tarea 20: la limpieza de bloques (src/metaserver/gc.py).

- plan_gc, una prueba por regla, sin etcd y con la hora como parámetro.
- gc_round contra FakeEtcd: la carrera de la Figura 1 y las órdenes delete.
- La regla del instante T de punta a punta con un BlockStore, sin red.
- La limpieza dentro de la vuelta del líder: corre cada DFS_GC_INTERVAL_S y no espera la gracia.
"""

import asyncio
import hashlib
import os

import pytest

from datanode.storage import BlockStore
from dfs_common.config import Settings
from metaserver.gc import Manifest, StoredBlock, gc_round, plan_gc
from metaserver.leader import LeaderState, leader_round

from fakes import FakeEtcd, add_block, add_node

H1 = "6ce5a55b446b43e4e935c44695a9a09f161e8e87d1ff2359adebc5f9ee5ee5cf"
H2 = "b64bb9046f61749097db060781bcbbba317c08ff0ac378e9aed9a6d18217e449"
NOW = 1_793_640_000.0  # segundos
NOW_MS = int(NOW * 1000)
SETTINGS = Settings(_env_file=None, retention_s=600, gc_min_age_s=3600, gc_clock_margin_s=1, gc_interval_s=600)
OLD = NOW_MS - 3600 * 1000  # touched_at con la edad justa para ser candidato


def manifest(user: str, hashes: list[str], *, replaced_at: int | None = None, key: str = "/files/f1/v/1") -> Manifest:
    value = {
        "user": user,
        "size": 8388608 * len(hashes),
        "block_size": 8388608,
        "hashes": hashes,
        "file_sha256": None,
        "created_at": NOW_MS - 7200 * 1000,
        "replaced_at": replaced_at,
    }
    return Manifest(key, value, 5)


def stored(user: str, hash: str, *, touched_at: int = OLD, stored_on: list[str] = ("dn1", "dn2", "dn3")) -> StoredBlock:
    return StoredBlock(user, hash, {"size": 8388608, "stored_on": list(stored_on), "touched_at": touched_at}, 7)


def candidates(plan) -> list[tuple[str, str]]:
    return [(block.user, block.hash) for block in plan.candidates]


# =====================================================================
# plan_gc
# =====================================================================


def test_current_manifest_never_expires_and_protects_its_blocks():
    plan = plan_gc([manifest("ana", [H1])], [stored("ana", H1)], NOW, SETTINGS)
    assert (plan.expired, plan.candidates) == ([], [])


def test_retained_manifest_protects_its_blocks_until_it_expires():
    blocks = [stored("ana", H1)]
    almost = manifest("ana", [H1], replaced_at=NOW_MS - 600 * 1000 + 1)
    assert plan_gc([almost], blocks, NOW, SETTINGS).candidates == []
    expired = manifest("ana", [H1], replaced_at=NOW_MS - 600 * 1000)
    plan = plan_gc([expired], blocks, NOW, SETTINGS)
    assert plan.expired == [expired]
    assert candidates(plan) == [("ana", H1)]


def test_blocks_of_a_file_removed_with_rm_end_up_as_candidates():
    # Después de rm no hay entrada en /ns; queda el manifiesto retenido con su usuario (sección 8).
    removed = manifest("ana", [H1, H2], replaced_at=NOW_MS - 3600 * 1000)
    plan = plan_gc([removed], [stored("ana", H1), stored("ana", H2)], NOW, SETTINGS)
    assert candidates(plan) == [("ana", H1), ("ana", H2)]


def test_a_recent_block_is_not_a_candidate():
    # touched_at nuevo: una subida en curso que todavía no publicó su manifiesto (DA-02).
    plan = plan_gc([], [stored("ana", H1, touched_at=NOW_MS - 3600 * 1000 + 1)], NOW, SETTINGS)
    assert plan.candidates == []


def test_a_hash_used_by_another_file_of_the_same_user_is_not_a_candidate():
    other_file = manifest("ana", [H1], key="/files/f2/v/3")
    removed = manifest("ana", [H1], replaced_at=NOW_MS - 3600 * 1000)
    assert plan_gc([removed, other_file], [stored("ana", H1)], NOW, SETTINGS).candidates == []


def test_each_manifest_protects_the_blocks_of_its_own_user():
    # /blocks/{user}/{hash} es de un usuario: el mismo contenido de bob protege la clave de bob, no la de ana.
    bob_file = manifest("bob", [H1], key="/files/f9/v/1")
    plan = plan_gc([bob_file], [stored("ana", H1), stored("bob", H1)], NOW, SETTINGS)
    assert candidates(plan) == [("ana", H1)]


# =====================================================================
# gc_round contra FakeEtcd
# =====================================================================


def seed_manifest(etcd: FakeEtcd, m: Manifest) -> None:
    etcd.seed(m.key, m.value)


def ids():
    counter = iter(range(100))
    return lambda: f"orden-{next(counter)}"


@pytest.mark.anyio
async def test_round_deletes_expired_manifests_then_the_block_and_orders_one_delete_per_node():
    etcd = FakeEtcd()
    add_node(etcd, "dn1")
    add_node(etcd, "dn2")  # dn3 está caído: recibe su orden al volver
    seed_manifest(etcd, manifest("ana", [H1], replaced_at=NOW_MS - 3600 * 1000))
    add_block(etcd, "ana", H1, stored_on=["dn1", "dn2", "dn3"], touched_at=OLD)
    t = NOW + 0.25  # el instante T se toma justo antes de escribir

    await gc_round(etcd, SETTINGS, NOW, clock=lambda: t, new_id=ids())

    assert etcd.keys("/files/") == []
    assert etcd.keys("/blocks/") == []
    assert etcd.keys("/orders/") == ["/orders/dn1/orden-0", "/orders/dn2/orden-1", "/orders/dn3/orden-2"]
    for key in etcd.keys("/orders/"):
        assert etcd.value(key) == {
            "order_id": key.rsplit("/", 1)[1],
            "type": "delete",
            "user": "ana",
            "hash": H1,
            "written_before_ms": int(t * 1000) - 1000,
        }


@pytest.mark.anyio
async def test_figure_1_a_reservation_that_touches_the_block_wins_and_the_block_survives(monkeypatch):
    etcd = FakeEtcd()
    add_block(etcd, "ana", H1, stored_on=["dn1", "dn2", "dn3"], touched_at=OLD)
    real_txn = etcd.txn

    async def reservation_in_between(compare, success, failure):
        # Entre la lectura de la limpieza y su transacción, una reserva reutiliza el bloque (touch_block).
        etcd.seed(f"/blocks/ana/{H1}", {**etcd.value(f"/blocks/ana/{H1}"), "touched_at": NOW_MS})
        return await real_txn(compare, success, failure)

    monkeypatch.setattr(etcd, "txn", reservation_in_between)
    await gc_round(etcd, SETTINGS, NOW)

    assert etcd.value(f"/blocks/ana/{H1}")["touched_at"] == NOW_MS  # la clave sigue
    assert etcd.keys("/orders/") == []  # y no salió ninguna orden delete


@pytest.mark.anyio
async def test_if_an_expired_manifest_changed_no_block_is_deleted_this_round(monkeypatch):
    etcd = FakeEtcd()
    seed_manifest(etcd, manifest("ana", [H1], replaced_at=NOW_MS - 3600 * 1000))
    add_block(etcd, "ana", H1, stored_on=["dn1"], touched_at=OLD)
    real_txn = etcd.txn

    async def manifest_changes(compare, success, failure):
        etcd.seed("/files/f1/v/1", etcd.value("/files/f1/v/1"))  # otra escritura cambia su mod_revision
        return await real_txn(compare, success, failure)

    monkeypatch.setattr(etcd, "txn", manifest_changes)
    await gc_round(etcd, SETTINGS, NOW)

    assert etcd.keys("/files/") == ["/files/f1/v/1"]
    assert etcd.keys("/blocks/") == [f"/blocks/ana/{H1}"]


# =====================================================================
# La regla del instante T de punta a punta, con un BlockStore
# =====================================================================


async def chunks_of(data: bytes):
    yield data


@pytest.mark.anyio
async def test_instant_t_a_copy_written_after_t_survives_and_one_written_before_is_deleted(tmp_path):
    data = b"bloque de ana " * 100
    hash = hashlib.sha256(data).hexdigest()
    old_copy = BlockStore(tmp_path / "dn1", bytes(32), 8388608)  # copia vieja, escrita antes de T
    new_copy = BlockStore(tmp_path / "dn2", bytes(32), 8388608)  # el cliente la volvió a subir después de T
    await old_copy.write("ana", hash, chunks_of(data))
    await new_copy.write("ana", hash, chunks_of(data))
    t = NOW
    before_ns, after_ns = int((t - 5) * 1e9), int((t + 5) * 1e9)
    os.utime(tmp_path / "dn1" / "ana" / hash, ns=(before_ns, before_ns))
    os.utime(tmp_path / "dn2" / "ana" / hash, ns=(after_ns, after_ns))

    etcd = FakeEtcd()
    add_block(etcd, "ana", hash, stored_on=["dn1", "dn2"], touched_at=OLD)
    await gc_round(etcd, SETTINGS, NOW, clock=lambda: t)

    stores = {"dn1": old_copy, "dn2": new_copy}
    for key in etcd.keys("/orders/"):
        order = etcd.value(key)
        node_id = key.split("/")[2]
        await asyncio.to_thread(stores[node_id].delete_if_older, order["user"], order["hash"], order["written_before_ms"])
    assert not (tmp_path / "dn1" / "ana" / hash).exists()
    assert new_copy.read("ana", hash) == data


# =====================================================================
# Dentro de la vuelta del líder
# =====================================================================


@pytest.mark.anyio
async def test_leader_runs_the_cleanup_during_its_grace_and_then_every_interval():
    etcd = FakeEtcd()
    add_block(etcd, "ana", H1, stored_on=["dn1"], touched_at=OLD)
    state = LeaderState(won_at=NOW)  # recién ganó: la re-réplica espera, la limpieza no

    await leader_round(etcd, SETTINGS, state, NOW)
    assert etcd.keys("/blocks/") == []
    assert state.last_gc == NOW

    add_block(etcd, "ana", H2, stored_on=["dn1"], touched_at=OLD)
    await leader_round(etcd, SETTINGS, state, NOW + 599)
    assert etcd.keys("/blocks/") == [f"/blocks/ana/{H2}"]  # todavía no le toca
    await leader_round(etcd, SETTINGS, state, NOW + 600)
    assert etcd.keys("/blocks/") == []
