r"""Pruebas de metaserver.etcd contra un etcd real (tarea 06).

Se saltan si no está la variable DFS_TEST_ETCD. Para correrlas, levanta un etcd 3.5
sin TLS y apunta la variable a él (PowerShell, desde la raíz del repositorio):

    docker run -d --rm --name etcd-test -p 2379:2379 -e ETCD_LISTEN_CLIENT_URLS=http://0.0.0.0:2379 -e ETCD_ADVERTISE_CLIENT_URLS=http://127.0.0.1:2379 quay.io/coreos/etcd:v3.5.35
    $env:DFS_TEST_ETCD = "http://127.0.0.1:2379"; .venv\Scripts\python -m pytest -q tests/test_etcd_integration.py; Remove-Item Env:DFS_TEST_ETCD
    docker stop etcd-test

Cada prueba trabaja bajo un prefijo propio (/test-{uuid}) y lo borra al terminar; los
leases de las pruebas se revocan al final, y si una prueba se corta vencen solos.
"""

import contextlib
import os
import uuid

import anyio
import pytest

from metaserver.etcd import (
    Etcd,
    LeaseNotFound,
    compare,
    lease_hex,
    lock_key,
    op_delete,
    op_put,
    op_range,
    prefix_end,
)

ENDPOINT = os.environ.get("DFS_TEST_ETCD", "")
pytestmark = [
    pytest.mark.skipif(not ENDPOINT, reason="sin DFS_TEST_ETCD no hay un etcd real contra el cual probar"),
    pytest.mark.anyio,
]

LEASE_S = 30  # TTL de los leases de estas pruebas


@pytest.fixture
async def etcd():
    async with Etcd([ENDPOINT]) as client:
        yield client


@pytest.fixture
async def base(etcd):
    prefix = f"/test-{uuid.uuid4().hex}"
    yield prefix
    await etcd.txn([], [op_delete(prefix + "/", range_end=prefix_end(prefix + "/"))], [])


async def revoke_all(etcd: Etcd, *lease_ids: int) -> None:
    for lease_id in lease_ids:
        with contextlib.suppress(LeaseNotFound):
            await etcd.lease_revoke(lease_id)


async def test_put_get_and_get_prefix(etcd, base):
    docs = f"{base}/ns/ana/docs"
    await etcd.put(docs, {"type": "dir", "created_at": 1793628090000})
    await etcd.put(f"{docs}/a.txt", {"type": "file", "file_id": "f1", "version": 1})
    await etcd.put(f"{base}/ns/bruno/x", b"\x00\xff")

    got = await etcd.get(docs)
    assert got.json() == {"type": "dir", "created_at": 1793628090000}
    assert got.create_revision == got.mod_revision > 0
    assert (got.version, got.lease) == (1, 0)
    assert (await etcd.get(f"{base}/ns/bruno/x")).value == b"\x00\xff"
    assert await etcd.get(f"{base}/nada") is None

    assert [kv.key for kv in await etcd.get_prefix(f"{base}/ns/ana/")] == [docs, f"{docs}/a.txt"]
    keys = await etcd.get_prefix(f"{base}/ns/", keys_only=True, limit=2)
    assert [(kv.key, kv.value) for kv in keys] == [(docs, b""), (f"{docs}/a.txt", b"")]

    await etcd.put(docs, {"type": "dir", "created_at": 1})
    again = await etcd.get(docs)
    assert again.create_revision == got.create_revision
    assert again.mod_revision > got.mod_revision
    assert again.version == 2

    assert await etcd.delete(docs) == 1
    assert await etcd.delete(docs) == 0


async def test_txn_create_rule_and_range_compare(etcd, base):
    """Las dos reglas de "Crear y borrar sin carreras" (docs/contratos.md, sección 8)."""
    parent = f"{base}/ns/ana/docs"
    await etcd.put(parent, {"type": "dir", "created_at": 1})
    parent_rev = (await etcd.get(parent)).mod_revision

    # Crear: la clave no existe y el padre no cambió. La segunda vez la clave ya existe.
    child = f"{parent}/fotos"
    create = [compare(child, "CREATE", "EQUAL", 0), compare(parent, "MOD", "EQUAL", parent_rev)]
    created = await etcd.txn(create, [op_put(child, {"type": "dir", "created_at": 2})], [op_range(child)])
    assert created.succeeded
    assert created.responses == [None]
    again = await etcd.txn(create, [op_put(child, {"type": "dir", "created_at": 3})], [op_range(child)])
    assert not again.succeeded
    assert [kv.json() for kv in again.responses[0]] == [{"type": "dir", "created_at": 2}]

    # rmdir: comparación de rango con create_revision = 0, verdadera solo con el rango vacío.
    # Un hermano con nombre parecido (fotos-viejas) no está en el rango de fotos/.
    await etcd.put(f"{parent}/fotos-viejas", {"type": "dir", "created_at": 4})
    children = f"{child}/"
    empty = compare(children, "CREATE", "EQUAL", 0, range_end=prefix_end(children))
    unchanged = compare(child, "MOD", "EQUAL", (await etcd.get(child)).mod_revision)
    assert (await etcd.txn([empty, unchanged], [], [])).succeeded

    await etcd.put(f"{children}a.jpg", {"type": "file", "file_id": "f1", "version": 1})
    in_range = op_range(children, range_end=prefix_end(children))
    refused = await etcd.txn([empty, unchanged], [op_delete(child)], [in_range])
    assert not refused.succeeded
    assert [kv.key for kv in refused.responses[0]] == [f"{children}a.jpg"]
    assert await etcd.get(child) is not None


async def test_lease_and_keepalive(etcd, base):
    lease_id = await etcd.lease_grant(LEASE_S)
    assert 0 < await etcd.lease_keepalive(lease_id) <= LEASE_S

    key = f"{base}/nodes/meta/meta1"
    await etcd.put(key, {"ip": "172.31.0.10", "public_port": 8443, "internal_port": 8444}, lease=lease_id)
    assert (await etcd.get(key)).lease == lease_id

    await etcd.lease_revoke(lease_id)
    assert await etcd.get(key) is None  # revocar el lease borra sus claves
    with pytest.raises(LeaseNotFound):
        await etcd.lease_keepalive(lease_id)
    with pytest.raises(LeaseNotFound):
        await etcd.lease_revoke(lease_id)
    with pytest.raises(LeaseNotFound):
        await etcd.put(key, b"", lease=lease_id)


async def test_lock_with_and_without_wait(etcd, base):
    name = f"{base}/locks/ana/docs/informe.pdf"
    first, second, third, fourth = [await etcd.lease_grant(LEASE_S) for _ in range(4)]
    try:
        key = await etcd.lock(name, first, wait_s=5)
        assert key == lock_key(name, first)  # el servicio de lock usa {name}/{lease en hex}

        # Sin espera y ocupado: None, y no deja su clave en la fila.
        assert await etcd.lock(name, second, wait_s=0) is None
        assert [kv.key for kv in await etcd.get_prefix(f"{name}/", keys_only=True)] == [key]

        # Con espera y ocupado: se agota la espera y None. Quien llama revoca el lease (tarea 11).
        assert await etcd.lock(name, second, wait_s=1) is None
        await etcd.lease_revoke(second)

        # Con espera: queda en la fila y entra cuando se suelta el lock.
        got = {}

        async def wait_for_lock():
            got["key"] = await etcd.lock(name, third, wait_s=10)

        with anyio.fail_after(10):
            async with anyio.create_task_group() as tasks:
                tasks.start_soon(wait_for_lock)
                await anyio.sleep(0.5)
                assert got == {}  # sigue esperando
                await etcd.unlock(key)
        assert got["key"] == lock_key(name, third)

        # Sin espera y libre: lo obtiene.
        await etcd.lease_revoke(third)
        assert await etcd.lock(name, fourth, wait_s=0) == lock_key(name, fourth)
    finally:
        await revoke_all(etcd, first, second, third, fourth)


async def test_election_campaign_leader_and_resign(etcd, base):
    name = f"{base}/election/leader"
    first, second = [await etcd.lease_grant(LEASE_S) for _ in range(2)]
    try:
        assert await etcd.leader(name) is None

        won = await etcd.campaign(name, first, {"meta_id": "meta1"})
        assert won.key == f"{name}/{lease_hex(first)}"
        leader = await etcd.leader(name)
        assert leader.key == won.key
        assert leader.json() == {"meta_id": "meta1"}

        # El segundo candidato espera hasta que el primero renuncia.
        result = {}

        async def campaign_second():
            result["won"] = await etcd.campaign(name, second, {"meta_id": "meta2"})

        with anyio.fail_after(10):
            async with anyio.create_task_group() as tasks:
                tasks.start_soon(campaign_second)
                await anyio.sleep(0.5)
                assert result == {}  # sigue esperando
                await etcd.resign(won)
        assert (await etcd.leader(name)).json() == {"meta_id": "meta2"}

        await etcd.resign(result["won"])
        assert await etcd.leader(name) is None
    finally:
        await revoke_all(etcd, first, second)
