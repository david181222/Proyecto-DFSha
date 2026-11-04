"""Pruebas de la tarea 11: POST, PUT y DELETE /locks (docs/contratos.md, secciones 2, 8 y 11).

Las rutas se prueban con TestClient y FakeEtcd. Lo que necesita dos intentos a la vez (la
espera en la fila y su renovación) se prueba llamando directo a metaserver.locks, en un solo
bucle de eventos y con un intervalo de renovación de 0,05 s (DFS_LOCK_RENEW_S no baja de 1 s).
Lo mismo, contra un etcd real, está en tests/test_metaserver_integration.py.
"""

import asyncio

import pytest

from dfs_common.errors import DFSError, ErrorCode
from metaserver import locks
from metaserver.etcd import EtcdError, lease_from_hex, lease_hex
from metaserver.locks import LockInfo, parse_lock_key

from fakes import FakeEtcd, add_dir, add_file, bearer, public_client

LOCKS = "/api/v1/locks"
PATH = "/docs/informe.pdf"
NAME = "/locks/ana/docs/informe.pdf"
LOCK_ID = "694d8a1c2e3b4f05"  # el del ejemplo de docs/contratos.md
RENEW_S = 0.05


def assert_error(response, status: int, code: str) -> None:
    assert response.status_code == status, response.json()
    assert response.json()["error"] == code


def take(client, path: str = PATH, *, wait_s: int = 0, user: str = "ana"):
    return client.post(LOCKS, json={"path": path, "wait_s": wait_s}, headers=bearer(user))


def renew(client, lock_id: str, *, user: str = "ana"):
    return client.put(f"{LOCKS}/{lock_id}", headers=bearer(user))


def release(client, lock_id: str, *, user: str = "ana"):
    return client.delete(f"{LOCKS}/{lock_id}", headers=bearer(user))


def acquire(etcd: FakeEtcd, *, wait_s: float, renew_s: float = RENEW_S):
    """locks.acquire_lock de ana sobre PATH, con un lease de 30 s."""
    return locks.acquire_lock(etcd, "ana", PATH, wait_s=wait_s, lease_s=30, renew_s=renew_s)


# --- parse_lock_key (función pura) ---


@pytest.mark.parametrize(
    ("key", "expected"),
    [
        (f"{NAME}/{LOCK_ID}", LockInfo(f"{NAME}/{LOCK_ID}", "ana", PATH)),
        # Un nombre de archivo igual al lock_id: solo se quita el último componente.
        (f"/locks/ana/a/{LOCK_ID}/{LOCK_ID}", LockInfo(f"/locks/ana/a/{LOCK_ID}/{LOCK_ID}", "ana", f"/a/{LOCK_ID}")),
        ("/uploads/9b2f6d1e-4c3a-4e8b-9f1d-2a7c5e8b1d43", None),  # la subida, atada al mismo lease (tarea 12)
        ("/nodes/dn/dn1", None),  # el lease de latidos de un DataNode
        (f"{NAME}/7a00000000000001", None),  # la clave de otro lease
        (f"/locks/ana/{LOCK_ID}", None),  # sin ruta
        (f"/locks/{LOCK_ID}", None),  # sin usuario ni ruta
    ],
)
def test_parse_lock_key(key, expected):
    assert parse_lock_key(key, LOCK_ID) == expected


# --- Rutas ---


@pytest.mark.parametrize(
    ("method", "route"), [("POST", LOCKS), ("PUT", f"{LOCKS}/{LOCK_ID}"), ("DELETE", f"{LOCKS}/{LOCK_ID}")]
)
def test_every_lock_route_requires_a_jwt(method, route):
    with public_client() as client:
        assert_error(client.request(method, route, json={"path": PATH}), 401, "E_AUTH")


def test_take_renew_and_release():
    # La ruta no existe todavía: con w, el lock se toma antes de crear el archivo.
    etcd = FakeEtcd()
    with public_client(etcd, lock_lease_s=17) as client:
        taken = take(client, wait_s=30)
        assert taken.status_code == 200, taken.json()
        lock_id = taken.json()["lock_id"]
        lease = lease_from_hex(lock_id)
        assert taken.json() == {"lock_id": lock_id, "lease_s": 17}
        assert etcd.keys("/locks/") == [f"{NAME}/{lock_id}"]
        assert etcd.kvs[f"{NAME}/{lock_id}"].lease == lease
        assert etcd.ttls[lease] == 17  # el lease del intento dura DFS_LOCK_LEASE_S

        assert renew(client, lock_id).json() == {"lock_id": lock_id, "lease_s": 17}  # decisión 4
        assert etcd.keepalives == [lease]

        assert release(client, lock_id).json() == {"ok": True}
        assert etcd.keys("/locks/") == []
        assert etcd.leases == set()
        assert release(client, lock_id).json() == {"ok": True}  # idempotente
        assert_error(renew(client, lock_id), 409, "E_LOCK_LOST")


@pytest.mark.parametrize("wait_s", [0, 1], ids=["sin-espera", "espera-agotada"])
def test_a_second_attempt_is_423_and_leaves_the_line(wait_s):
    etcd = FakeEtcd()
    with public_client(etcd) as client:
        first = take(client).json()["lock_id"]
        assert_error(take(client, wait_s=wait_s), 423, "E_LOCKED")

    assert etcd.keys("/locks/") == [f"{NAME}/{first}"]
    assert etcd.leases == {lease_from_hex(first)}  # el lease del segundo intento se revocó


def test_only_files_can_be_locked():
    etcd = FakeEtcd()
    add_dir(etcd, "ana", "/docs")
    add_file(etcd, "ana", PATH)
    with public_client(etcd) as client:
        assert_error(take(client, "/"), 400, "E_BAD_REQUEST")
        assert_error(take(client, "/docs"), 400, "E_BAD_REQUEST")
        assert_error(take(client, "docs"), 400, "E_BAD_REQUEST")  # ruta inválida
        assert_error(take(client, wait_s=61), 400, "E_BAD_REQUEST")  # fuera de 0 a 60
        assert etcd.leases == set()  # ninguno de estos pidió lease

        assert take(client, PATH).status_code == 200  # un archivo que existe
        assert take(client, "/docs/nuevo.txt").status_code == 200  # uno que todavía no existe


def test_a_lock_of_another_user_is_403_and_stays_as_it_was():
    etcd = FakeEtcd()
    with public_client(etcd) as client:
        lock_id = take(client, user="bruno").json()["lock_id"]
        assert_error(renew(client, lock_id, user="ana"), 403, "E_FORBIDDEN")
        assert_error(release(client, lock_id, user="ana"), 403, "E_FORBIDDEN")

    assert etcd.keys("/locks/") == [f"/locks/bruno{PATH}/{lock_id}"]
    assert etcd.keepalives == []  # no se renovó


@pytest.mark.parametrize("lock_id", [LOCK_ID, "no-es-hex", LOCK_ID.upper(), "0", "f" * 16])
def test_a_lock_id_that_names_no_lock_is_409_to_renew_and_200_to_release(lock_id):
    with public_client() as client:
        assert_error(renew(client, lock_id), 409, "E_LOCK_LOST")
        assert release(client, lock_id).json() == {"ok": True}


@pytest.mark.parametrize("keys", [["/nodes/dn/dn1"], []], ids=["datanode", "sin-claves"])
def test_a_lease_that_is_not_a_lock_is_never_revoked_or_renewed(keys):
    """Decisión 1: un lease sin clave de lock es un lock que no existe. Es una protección:
    un usuario no puede revocar ni renovar leases que no son sus locks, como el de los
    latidos de un DataNode."""
    etcd = FakeEtcd()
    lease = asyncio.run(etcd.lease_grant(15))
    for key in keys:
        etcd.seed(key, {"ip": "172.31.0.10", "zone": "us-east-1a"}, lease=lease)

    with public_client(etcd) as client:
        assert release(client, lease_hex(lease)).json() == {"ok": True}
        assert_error(renew(client, lease_hex(lease)), 409, "E_LOCK_LOST")

    assert etcd.leases == {lease}  # sigue vivo
    assert etcd.keys("/nodes/") == keys  # con sus claves
    assert etcd.keepalives == []  # y no se renovó


class LeaseEndsBeforeKeepalive(FakeEtcd):
    """El lease vence entre resolve_lock y su renovación."""

    async def lease_keepalive(self, lease_id):
        await self.lease_revoke(lease_id)
        return await super().lease_keepalive(lease_id)


def test_a_lock_that_expires_just_before_its_renewal_is_409():
    etcd = LeaseEndsBeforeKeepalive()
    with public_client(etcd) as client:
        lock_id = take(client).json()["lock_id"]
        assert_error(renew(client, lock_id), 409, "E_LOCK_LOST")


class LeaseEndsBeforeRevoke(FakeEtcd):
    """El lease vence entre resolve_lock y su revocación: revocarlo da LeaseNotFound."""

    async def lease_revoke(self, lease_id):
        await super().lease_revoke(lease_id)
        await super().lease_revoke(lease_id)


def test_a_lock_that_expires_just_before_its_release_is_200():
    etcd = LeaseEndsBeforeRevoke()
    with public_client(etcd) as client:
        lock_id = take(client).json()["lock_id"]
        assert release(client, lock_id).json() == {"ok": True}


class RevokeFails(FakeEtcd):
    """Revocar un lease falla, como si etcd dejara de responder justo en ese momento."""

    def __init__(self, failure: Exception) -> None:
        super().__init__()
        self.failure = failure

    async def lease_revoke(self, lease_id):
        raise self.failure


@pytest.mark.parametrize(
    "failure",
    [DFSError(ErrorCode.E_UNAVAILABLE, "ningún nodo de etcd respondió"), EtcdError("etcdserver: request timed out")],
    ids=["503", "error-de-etcd"],
)
def test_a_423_is_still_423_if_revoking_the_attempt_fails(failure, caplog):
    """Decisión 3: el lease del intento vence solo en DFS_LOCK_LEASE_S."""
    etcd = RevokeFails(failure)
    with public_client(etcd) as client:
        take(client)
        assert_error(take(client), 423, "E_LOCKED")
    assert "vencerá solo" in caplog.text


def test_rm_and_post_locks_share_the_line():
    etcd = FakeEtcd()
    add_file(etcd, "ana", PATH)
    with public_client(etcd) as client:
        lock_id = take(client).json()["lock_id"]
        rm = client.delete("/api/v1/fs/rm", params={"path": PATH}, headers=bearer())
        assert_error(rm, 423, "E_LOCKED")

        release(client, lock_id)
        rm = client.delete("/api/v1/fs/rm", params={"path": PATH}, headers=bearer())
        assert rm.json() == {"ok": True}


# --- resolve_lock y la espera en la fila (llamando directo a metaserver.locks) ---


@pytest.mark.anyio
async def test_resolve_lock_finds_the_lock_among_the_keys_of_its_lease():
    etcd = FakeEtcd()
    lock_id = await acquire(etcd, wait_s=0)
    await etcd.put("/uploads/u1", {"user": "ana"}, lease=lease_from_hex(lock_id))  # la subida (tarea 12)

    assert await locks.resolve_lock(etcd, lock_id, "ana") == LockInfo(f"{NAME}/{lock_id}", "ana", PATH)
    with pytest.raises(DFSError) as caught:
        await locks.resolve_lock(etcd, lock_id, "bruno")
    assert caught.value.code == ErrorCode.E_FORBIDDEN


@pytest.mark.anyio
async def test_a_waiting_attempt_enters_when_the_holder_releases():
    etcd = FakeEtcd()
    holder = await acquire(etcd, wait_s=0)
    waiter = asyncio.create_task(acquire(etcd, wait_s=5))
    await asyncio.sleep(0.1)
    assert not waiter.done()  # sigue en la fila

    await locks.release_lock(etcd, holder, "ana")
    lock_id = await asyncio.wait_for(waiter, 1)
    assert etcd.keys("/locks/") == [f"{NAME}/{lock_id}"]


@pytest.mark.anyio
async def test_the_lease_is_renewed_while_waiting_and_not_after():
    etcd = FakeEtcd()
    holder = await acquire(etcd, wait_s=0)
    waiter = asyncio.create_task(acquire(etcd, wait_s=5))
    await asyncio.sleep(0.3)
    await locks.release_lock(etcd, holder, "ana")
    lease = lease_from_hex(await asyncio.wait_for(waiter, 1))

    renewals = len(etcd.keepalives)
    assert renewals >= 3  # 0,3 s en la fila, con una renovación cada 0,05 s
    assert set(etcd.keepalives) == {lease}  # solo la del intento que esperaba
    await asyncio.sleep(0.2)
    assert len(etcd.keepalives) == renewals  # la renovación se canceló al terminar la espera


@pytest.mark.anyio
async def test_a_wait_that_runs_out_is_423_and_leaves_the_line():
    etcd = FakeEtcd()
    holder = await acquire(etcd, wait_s=0)
    with pytest.raises(DFSError) as caught:
        await acquire(etcd, wait_s=0.2)

    assert caught.value.code == ErrorCode.E_LOCKED
    assert etcd.keys("/locks/") == [f"{NAME}/{holder}"]
    assert etcd.leases == {lease_from_hex(holder)}


@pytest.mark.anyio
async def test_a_lease_that_ends_while_waiting_is_423():
    """Decisión 2: es una espera que se agotó. renew_s mayor que la espera: nadie renueva el lease."""
    etcd = FakeEtcd()
    holder = await acquire(etcd, wait_s=0)
    waiter = asyncio.create_task(acquire(etcd, wait_s=5, renew_s=60))
    await asyncio.sleep(0.1)
    [lease] = etcd.leases - {lease_from_hex(holder)}
    await etcd.lease_revoke(lease)  # vence en la fila

    with pytest.raises(DFSError) as caught:
        await asyncio.wait_for(waiter, 1)
    assert caught.value.code == ErrorCode.E_LOCKED
    assert etcd.keys("/locks/") == [f"{NAME}/{holder}"]


class KeepaliveFailsOnce(FakeEtcd):
    """La primera renovación no llega a etcd."""

    async def lease_keepalive(self, lease_id):
        if not self.keepalives:
            self.keepalives.append(lease_id)
            raise DFSError(ErrorCode.E_UNAVAILABLE, "ningún nodo de etcd respondió")
        return await super().lease_keepalive(lease_id)


@pytest.mark.anyio
async def test_a_renewal_that_fails_is_logged_and_tried_again_on_the_next_round(caplog):
    """Decisión 2: si etcd no responde en una vuelta, queda en el log y se intenta en la siguiente."""
    etcd = KeepaliveFailsOnce()
    holder = await acquire(etcd, wait_s=0)
    waiter = asyncio.create_task(acquire(etcd, wait_s=5))
    await asyncio.sleep(0.3)
    await locks.release_lock(etcd, holder, "ana")
    lease = lease_from_hex(await asyncio.wait_for(waiter, 1))

    assert etcd.keepalives.count(lease) >= 3  # siguió renovando después del fallo
    assert "se reintenta" in caplog.text
