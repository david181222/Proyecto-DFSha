"""Rutas de las tareas 07, 08 y 11 contra un etcd real, con el lifespan de verdad.

Las pruebas de tests/test_auth.py, tests/test_fs.py y tests/test_locks.py usan FakeEtcd.
Estas confirman que con etcd pasa lo mismo: transacciones de creación y de rango,
revisiones, leases, el intento de lock sin espera, la fila del servicio de lock y la
renovación del lease mientras se espera en ella. Se saltan sin la variable DFS_TEST_ETCD;
cómo levantar el etcd está en el docstring de tests/test_etcd_integration.py.

Cada prueba usa un usuario y un file_id propios y borra sus claves al terminar.
"""

import asyncio
import contextlib
import os
import uuid

import anyio
import pytest
from fastapi.testclient import TestClient

from dfs_common.errors import DFSError, ErrorCode
from dfs_common.security import verify_jwt
from metaserver import auth, deps, locks
from metaserver.etcd import Etcd, LeaseNotFound, lease_from_hex, lease_hex, op_delete, prefix_end
from metaserver.public_api import app as metaserver_public

from fakes import JWT_SECRET, bearer

ENDPOINT = os.environ.get("DFS_TEST_ETCD", "")
pytestmark = pytest.mark.skipif(not ENDPOINT, reason="sin DFS_TEST_ETCD no hay un etcd real contra el cual probar")

LEASE_S = 30  # TTL del lease del "otro escritor" de la prueba de rm y del dueño del lock en las de locks
SHORT_LEASE_S = 2  # el TTL mínimo de etcd con su configuración por defecto


def with_etcd(work):
    """Corre work(etcd) con un cliente propio, aparte del de la app (para preparar y revisar)."""

    async def main():
        async with Etcd([ENDPOINT]) as etcd:
            return await work(etcd)

    return anyio.run(main)


async def delete_prefixes(etcd: Etcd, *prefixes: str) -> None:
    for prefix in prefixes:
        await etcd.txn([], [op_delete(prefix, range_end=prefix_end(prefix))], [])


@pytest.fixture
def user():
    name = f"t{uuid.uuid4().hex[:12]}"
    yield name
    with_etcd(lambda etcd: delete_prefixes(etcd, f"/ns/{name}/", f"/locks/{name}/", f"/users/{name}"))


@pytest.fixture
def client(monkeypatch, tmp_path):
    """La API pública arrancada con su lifespan, conectada al etcd real (sin TLS) y sin admin inicial."""
    for key in list(os.environ):
        if key.upper().startswith("DFS_"):
            monkeypatch.delenv(key)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "jwt_secret").write_bytes(JWT_SECRET)
    monkeypatch.setenv("DFS_JWT_SECRET_FILE", str(tmp_path / "jwt_secret"))
    monkeypatch.setattr(deps, "connect_etcd", lambda settings: Etcd([ENDPOINT]))
    with TestClient(metaserver_public) as client:
        yield client


def test_create_user_and_log_in(client, user):
    admin = bearer("admin", admin=True)
    created = client.post("/api/v1/admin/users", json={"user": user, "password": "clave-larga"}, headers=admin)
    repeated = client.post("/api/v1/admin/users", json={"user": user, "password": "otra"}, headers=admin)
    login = client.post("/api/v1/auth/login", json={"user": user, "password": "clave-larga"})
    wrong = client.post("/api/v1/auth/login", json={"user": user, "password": "otra"})

    assert (created.status_code, repeated.status_code, login.status_code, wrong.status_code) == (200, 409, 200, 401)
    assert verify_jwt(login.json()["token"], JWT_SECRET).sub == user


def test_three_creations_at_once_have_a_single_winner(user):
    """Lo que protege al primer administrador cuando arrancan los tres MetaServers a la vez."""

    async def three_at_once(etcd):
        results = []

        async def create():
            results.append(await auth.create_user(etcd, user, "clave", is_admin=True))

        async with anyio.create_task_group() as tasks:
            for _ in range(3):
                tasks.start_soon(create)
        return results, await etcd.get(f"/users/{user}")

    results, stored = with_etcd(three_at_once)
    assert sorted(results) == [False, False, True]
    assert stored.version == 1


def test_directories(client, user):
    def fs(method, name, path):
        request = {"json": {"path": path}} if name == "mkdir" else {"params": {"path": path}}
        return client.request(method, f"/api/v1/fs/{name}", headers=bearer(user), **request)

    assert fs("POST", "mkdir", "/docs").status_code == 200
    assert fs("POST", "mkdir", "/docs/fotos").status_code == 200
    assert fs("POST", "mkdir", "/docs").json()["error"] == "E_EXISTS"
    assert fs("POST", "mkdir", "/nada/fotos").json()["error"] == "E_NOT_FOUND"
    assert [entry["name"] for entry in fs("GET", "ls", "/").json()["entries"]] == ["docs"]
    assert [entry["name"] for entry in fs("GET", "ls", "/docs").json()["entries"]] == ["fotos"]
    assert fs("GET", "stat", "/docs/fotos").json()["type"] == "dir"

    # rmdir: la comparación de rango del etcd real (sección 8).
    assert fs("DELETE", "rmdir", "/docs").json()["error"] == "E_NOT_EMPTY"
    assert fs("DELETE", "rmdir", "/docs/fotos").status_code == 200
    assert fs("DELETE", "rmdir", "/docs").status_code == 200
    assert fs("GET", "ls", "/").json() == {"entries": []}


def test_rm(client, user):
    file_id = str(uuid.uuid4())
    path = "/informe.pdf"

    async def add_file(etcd):
        await etcd.put(f"/ns/{user}{path}", {"type": "file", "file_id": file_id, "version": 1})
        manifest = {"user": user, "size": 3, "block_size": 8388608, "hashes": [], "file_sha256": "0" * 64}
        await etcd.put(f"/files/{file_id}/v/1", {**manifest, "created_at": 1793628090000, "replaced_at": None})
        writer = await etcd.lease_grant(LEASE_S)  # otro escritor tiene el lock
        await etcd.lock(f"/locks/{user}{path}", writer, wait_s=0)
        return writer

    async def state(etcd):
        return (
            await etcd.get(f"/ns/{user}{path}"),
            (await etcd.get(f"/files/{file_id}/v/1")).json(),
            await etcd.get_prefix(f"/locks/{user}/", keys_only=True),
        )

    def rm():
        return client.delete("/api/v1/fs/rm", params={"path": path}, headers=bearer(user))

    try:
        writer = with_etcd(add_file)
        assert rm().json()["error"] == "E_LOCKED"
        entry, manifest, locks = with_etcd(state)
        assert entry is not None and manifest["replaced_at"] is None
        assert len(locks) == 1  # solo el del otro escritor: el intento de rm salió de la fila

        with_etcd(lambda etcd: etcd.lease_revoke(writer))
        assert rm().json() == {"ok": True}
        entry, manifest, locks = with_etcd(state)
        assert entry is None
        assert manifest["replaced_at"] > 0 and manifest["user"] == user
        assert locks == []
    finally:
        with_etcd(lambda etcd: delete_prefixes(etcd, f"/files/{file_id}/"))


# --- Locks (tarea 11) ---


def test_locks(client, user):
    path = "/informe.pdf"
    name = f"/locks/{user}{path}"
    route = "/api/v1/locks"

    def take():
        return client.post(route, json={"path": path, "wait_s": 0}, headers=bearer(user))

    taken = take()
    lock_id = taken.json()["lock_id"]
    assert taken.json() == {"lock_id": lock_id, "lease_s": 30}
    assert take().json()["error"] == "E_LOCKED"
    in_line = with_etcd(lambda etcd: etcd.get_prefix(f"{name}/", keys_only=True))
    assert [kv.key for kv in in_line] == [f"{name}/{lock_id}"]  # el segundo intento salió de la fila

    assert client.put(f"{route}/{lock_id}", headers=bearer(user)).json() == {"lock_id": lock_id, "lease_s": 30}
    assert client.put(f"{route}/{lock_id}", headers=bearer(f"o{user}")).json()["error"] == "E_FORBIDDEN"
    assert client.delete(f"{route}/{lock_id}", headers=bearer(f"o{user}")).json()["error"] == "E_FORBIDDEN"

    assert client.delete(f"{route}/{lock_id}", headers=bearer(user)).json() == {"ok": True}
    assert client.delete(f"{route}/{lock_id}", headers=bearer(user)).json() == {"ok": True}
    assert client.put(f"{route}/{lock_id}", headers=bearer(user)).json()["error"] == "E_LOCK_LOST"
    assert with_etcd(lambda etcd: etcd.lease_keys(lease_from_hex(lock_id))) is None


def test_a_lease_that_is_not_a_lock_is_never_revoked_or_renewed(client, user):
    """Decisión 1: DELETE y PUT /locks no tocan un lease que no tiene clave de lock."""
    key = f"/test-{user}/nodes/dn/dn1"  # como la clave de latidos de un DataNode

    async def grant(etcd):
        lease_id = await etcd.lease_grant(LEASE_S)
        await etcd.put(key, {"ip": "172.31.0.10"}, lease=lease_id)
        return lease_id

    async def revoke(etcd):
        with contextlib.suppress(LeaseNotFound):
            await etcd.lease_revoke(lease_id)

    lease_id = with_etcd(grant)
    try:
        lock_id = lease_hex(lease_id)
        assert client.delete(f"/api/v1/locks/{lock_id}", headers=bearer(user)).json() == {"ok": True}
        assert client.put(f"/api/v1/locks/{lock_id}", headers=bearer(user)).json()["error"] == "E_LOCK_LOST"
        assert with_etcd(lambda etcd: etcd.lease_keys(lease_id)) == [key]  # sigue vivo, con su clave
    finally:
        with_etcd(revoke)


async def wait_behind_a_holder(etcd: Etcd, user: str, *, renew_s: float) -> tuple[str, list[str] | None]:
    """Un intento con un lease de SHORT_LEASE_S espera en la fila el doble de ese tiempo; después
    el dueño suelta el lock. Devuelve el lock_id que obtuvo y las claves de su lease."""
    path = "/informe.pdf"
    holder = await locks.acquire_lock(etcd, user, path, wait_s=0, lease_s=LEASE_S, renew_s=renew_s)
    waiter = asyncio.create_task(
        locks.acquire_lock(etcd, user, path, wait_s=10, lease_s=SHORT_LEASE_S, renew_s=renew_s)
    )
    await asyncio.sleep(2 * SHORT_LEASE_S)
    assert not waiter.done()
    await locks.release_lock(etcd, holder, user)
    lock_id = await waiter
    return lock_id, await etcd.lease_keys(lease_from_hex(lock_id))


def test_the_lease_of_a_waiting_attempt_is_renewed_until_it_enters(user):
    """El servicio de lock de etcd no renueva el lease del intento: lo renueva el MetaServer."""
    lock_id, keys = with_etcd(lambda etcd: wait_behind_a_holder(etcd, user, renew_s=0.5))
    assert keys == [f"/locks/{user}/informe.pdf/{lock_id}"]


def test_a_waiting_attempt_whose_lease_expires_is_423(user):
    """Decisión 2: sin renovación (renew_s mayor que la espera), el lease vence en la fila;
    etcd responde "mutex: session is expired" cuando se suelta el lock, y eso es un 423."""
    with pytest.raises(DFSError) as caught:
        with_etcd(lambda etcd: wait_behind_a_holder(etcd, user, renew_s=60))
    assert caught.value.code == ErrorCode.E_LOCKED
