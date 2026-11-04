"""Pruebas de FakeEtcd (tests/fakes.py) en lo que agregó la tarea 11: keepalive, lease_keys y lock con espera.

FakeEtcd debe portarse como Etcd en lo que usan las rutas. Lo mismo se comprueba contra un
etcd real en tests/test_etcd_integration.py y tests/test_metaserver_integration.py.
"""

import asyncio

import pytest

from metaserver.etcd import LeaseNotFound, lock_key

from fakes import FakeEtcd

pytestmark = pytest.mark.anyio

NAME = "/locks/ana/docs/informe.pdf"


async def test_lease_keepalive_counts_every_call_and_fails_without_the_lease():
    etcd = FakeEtcd()
    lease = await etcd.lease_grant(30)
    assert await etcd.lease_keepalive(lease) == 30

    await etcd.lease_revoke(lease)
    with pytest.raises(LeaseNotFound):
        await etcd.lease_keepalive(lease)
    assert etcd.keepalives == [lease, lease]


async def test_lease_keys_lists_the_keys_of_the_lease_or_none():
    etcd = FakeEtcd()
    lease = await etcd.lease_grant(30)
    assert await etcd.lease_keys(lease) == []

    await etcd.put("/uploads/u1", {"user": "ana"}, lease=lease)
    await etcd.put(lock_key(NAME, lease), b"", lease=lease)
    await etcd.put("/ns/ana/docs", {"type": "dir", "created_at": 1})  # sin lease: no aparece
    assert await etcd.lease_keys(lease) == [lock_key(NAME, lease), "/uploads/u1"]

    await etcd.lease_revoke(lease)
    assert await etcd.lease_keys(lease) is None


async def test_lock_with_wait_enters_when_the_holder_releases():
    etcd = FakeEtcd()
    holder, waiter = await etcd.lease_grant(30), await etcd.lease_grant(30)
    assert await etcd.lock(NAME, holder, wait_s=0) == lock_key(NAME, holder)

    waiting = asyncio.create_task(etcd.lock(NAME, waiter, wait_s=5))
    await asyncio.sleep(0.1)
    assert not waiting.done()  # sigue en la fila
    await etcd.lease_revoke(holder)

    assert await asyncio.wait_for(waiting, 1) == lock_key(NAME, waiter)


async def test_lock_with_wait_that_runs_out_leaves_the_line():
    etcd = FakeEtcd()
    holder, waiter = await etcd.lease_grant(30), await etcd.lease_grant(30)
    await etcd.lock(NAME, holder, wait_s=0)

    assert await etcd.lock(NAME, waiter, wait_s=0.1) is None
    assert etcd.keys(f"{NAME}/") == [lock_key(NAME, holder)]


async def test_lock_whose_lease_ends_while_waiting_raises_lease_not_found():
    etcd = FakeEtcd()
    holder, waiter = await etcd.lease_grant(30), await etcd.lease_grant(30)
    await etcd.lock(NAME, holder, wait_s=0)

    waiting = asyncio.create_task(etcd.lock(NAME, waiter, wait_s=5))
    await asyncio.sleep(0.1)
    await etcd.lease_revoke(waiter)  # como si hubiera vencido

    with pytest.raises(LeaseNotFound):
        await asyncio.wait_for(waiting, 1)


async def test_leader_is_the_oldest_candidate_or_none():
    etcd = FakeEtcd()
    assert await etcd.leader("/election/leader") is None
    etcd.seed("/election/leader/7a00000000000002", b"meta2")
    etcd.seed("/election/leader/694d8a1c2e3b4f01", b"meta1")  # va antes en orden de clave, pero llegó después
    assert (await etcd.leader("/election/leader")).value == b"meta2"
