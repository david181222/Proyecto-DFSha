"""Rutas de las tareas 07, 08 y 11 a 13 contra un etcd real, con el lifespan de verdad.

Las pruebas de tests/test_auth.py, tests/test_fs.py, tests/test_locks.py y las de la tarea 12
usan FakeEtcd. Estas confirman que con etcd pasa lo mismo: transacciones de creación y de
rango, revisiones, leases, el intento de lock sin espera, la fila del servicio de lock, la
renovación del lease mientras se espera en ella, y la subida completa con su commit. Se saltan sin la variable DFS_TEST_ETCD;
cómo levantar el etcd está en el docstring de tests/test_etcd_integration.py.

Cada prueba usa un usuario y un file_id propios y borra sus claves al terminar.
"""

import asyncio
import contextlib
import os
import time
import uuid

import anyio
import pytest
from fastapi.testclient import TestClient

from dfs_common.config import Settings
from dfs_common.errors import DFSError, ErrorCode
from dfs_common.security import verify_jwt
from metaserver import auth, deps, locks, metaservers
from metaserver.etcd import Etcd, LeaseNotFound, lease_from_hex, lease_hex, op_delete, prefix_end
from metaserver.internal_api import app as metaserver_internal
from metaserver.public_api import app as metaserver_public

from fakes import DA13, JWT_SECRET, PASS_KEY, bearer

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
    prefixes = [f"/ns/{name}/", f"/locks/{name}/", f"/users/{name}", f"/blocks/{name}/"]
    with_etcd(lambda etcd: delete_prefixes(etcd, *prefixes))


@pytest.fixture
def environment(monkeypatch, tmp_path):
    """Variables y secretos para arrancar las APIs del MetaServer con el etcd real (sin TLS) y sin admin inicial."""
    for key in list(os.environ):
        if key.upper().startswith("DFS_"):
            monkeypatch.delenv(key)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "jwt_secret").write_bytes(JWT_SECRET)
    monkeypatch.setenv("DFS_JWT_SECRET_FILE", str(tmp_path / "jwt_secret"))
    (tmp_path / "pass_key").write_bytes(PASS_KEY)
    monkeypatch.setenv("DFS_PASS_KEY_FILE", str(tmp_path / "pass_key"))
    # El MetaServer se registra al arrancar (tarea 13): un meta_id propio por prueba.
    meta_id = f"meta-{uuid.uuid4().hex[:8]}"
    monkeypatch.setenv("DFS_NODE_ID", meta_id)
    monkeypatch.setenv("DFS_NODE_IP", "10.88.0.21")
    monkeypatch.setattr(deps, "connect_etcd", lambda settings: Etcd([ENDPOINT]))
    return meta_id


@pytest.fixture
def client(environment):
    """La API pública arrancada con su lifespan."""
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


# --- Subidas (tarea 12) ---

MIB = 1024 * 1024
H0 = "6ce5a55b446b43e4e935c44695a9a09f161e8e87d1ff2359adebc5f9ee5ee5cf"
H1 = "b64bb9046f61749097db060781bcbbba317c08ff0ac378e9aed9a6d18217e449"


@pytest.fixture
def datanodes():
    """Los 4 DataNodes de DA-13, vivos en el etcd real mientras dura la prueba (atados a un lease)."""

    async def register(etcd):
        lease_id = await etcd.lease_grant(LEASE_S)
        for node_id, (ip, zone) in DA13.items():
            value = {"ip": ip, "public_port": 9443, "internal_port": 9444, "zone": zone, "free_bytes": 15 * 1024**3}
            await etcd.put(f"/nodes/dn/{node_id}", value, lease=lease_id)
        return lease_id

    async def revoke(etcd):
        with contextlib.suppress(LeaseNotFound):
            await etcd.lease_revoke(lease_id)

    lease_id = with_etcd(register)
    yield
    with_etcd(revoke)


def confirm_from_the_primary(item: dict, size: int, user: str):
    """El primario confirma su bloque en la API interna (con su lifespan), desde su IP, con una copia en dn1 o dn2."""
    primary = item["primary"]["node_id"]
    stored_on = [primary, "dn1" if primary != "dn1" else "dn2"]
    body = {"user": user, "hash": item["hash"], "size": size, "stored_on": stored_on}
    with TestClient(metaserver_internal, client=(DA13[primary][0], 50000)) as internal:
        return internal.post("/api/v1/internal/blocks/confirm", json=body)


def test_upload_confirm_commit_and_read(client, user, datanodes):
    path = "/docs/informe.pdf"
    sizes = {H0: 8 * MIB, H1: MIB}
    blocks = [{"index": 0, "hash": H0, "size": 8 * MIB}, {"index": 1, "hash": H1, "size": MIB}]
    headers = bearer(user)
    assert client.post("/api/v1/fs/mkdir", json={"path": "/docs"}, headers=headers).status_code == 200
    lock_id = client.post("/api/v1/locks", json={"path": path, "wait_s": 0}, headers=headers).json()["lock_id"]

    def reserve(base_version):
        body = {"path": path, "mode": "w", "base_version": base_version, "block_size": 8 * MIB, "lock_id": lock_id}
        return client.post("/api/v1/uploads", json={**body, "blocks": blocks}, headers=headers).json()

    def commit(upload_id, base_version):
        body = {"base_version": base_version, "lock_id": lock_id, "hashes": [H0, H1], "size": 9 * MIB}
        return client.post(f"/api/v1/uploads/{upload_id}/commit", json=body, headers=headers).json()

    file_id = None
    try:
        first = reserve(0)
        assert first["present"] == [] and len(first["missing"]) == 2
        # La subida quedó atada al lease del lock.
        lease_keys = with_etcd(lambda etcd: etcd.lease_keys(lease_from_hex(lock_id)))
        assert f"/uploads/{first['upload_id']}" in lease_keys
        for item in first["missing"]:
            assert confirm_from_the_primary(item, sizes[item["hash"]], user).json() == {"ok": True}
        assert commit(first["upload_id"], 0) == {"path": path, "version": 1}

        # La misma lista otra vez: todo está presente, y el commit pasa a la versión 2 (con base_version > 0).
        second = reserve(1)
        assert (second["present"], second["missing"]) == ([0, 1], [])
        assert commit(second["upload_id"], 1) == {"path": path, "version": 2}

        read = client.get("/api/v1/files/blocks", params={"path": path}, headers=headers).json()
        assert read["version"] == 2 and [block["size"] for block in read["blocks"]] == [8 * MIB, MIB]
        assert all(len(block["replicas"]) == 2 for block in read["blocks"])
        retained = client.get("/api/v1/files/blocks", params={"path": path, "version": 1}, headers=headers)
        assert retained.json()["version"] == 1  # la versión 1 sigue retenida (DA-14)
        file_id = with_etcd(lambda etcd: etcd.get(f"/ns/{user}{path}")).json()["file_id"]
        assert with_etcd(lambda etcd: etcd.get_prefix("/uploads/", keys_only=True)) == []
    finally:
        client.delete(f"/api/v1/locks/{lock_id}", headers=headers)
        if file_id:
            with_etcd(lambda etcd: delete_prefixes(etcd, f"/files/{file_id}/"))


def test_a_commit_that_loses_its_parent_is_404(client, user, datanodes):
    """La regla de creación del commit (sección 8) con el etcd real: el padre se borra antes de publicar."""
    path = "/docs/nuevo.txt"
    headers = bearer(user)
    client.post("/api/v1/fs/mkdir", json={"path": "/docs"}, headers=headers)
    lock_id = client.post("/api/v1/locks", json={"path": path, "wait_s": 0}, headers=headers).json()["lock_id"]
    body = {"path": path, "mode": "w", "base_version": 0, "block_size": 8 * MIB, "lock_id": lock_id, "blocks": []}
    upload_id = client.post("/api/v1/uploads", json=body, headers=headers).json()["upload_id"]
    try:
        assert client.delete("/api/v1/fs/rmdir", params={"path": "/docs"}, headers=headers).status_code == 200
        commit = {"base_version": 0, "lock_id": lock_id, "hashes": [], "size": 0}
        response = client.post(f"/api/v1/uploads/{upload_id}/commit", json=commit, headers=headers)
        assert response.status_code == 404 and response.json()["error"] == "E_NOT_FOUND"
    finally:
        client.delete(f"/api/v1/locks/{lock_id}", headers=headers)


# --- Registro de nodos, latidos y /admin/status (tarea 13) ---


def test_the_metaserver_registers_itself_and_appears_in_the_list(environment, client):
    listed = client.get("/api/v1/cluster/metaservers").json()["metaservers"]
    assert {"meta_id": environment, "ip": "10.88.0.21", "public_port": 8443, "internal_port": 8444} in listed
    kv = with_etcd(lambda etcd: etcd.get(f"/nodes/meta/{environment}"))
    assert kv.lease != 0  # atada a su lease: si el MetaServer cae, desaparece sola


def test_keep_registered_registers_again_when_the_lease_is_lost():
    meta_id = f"meta-{uuid.uuid4().hex[:8]}"
    settings = Settings(_env_file=None, node_id=meta_id, node_ip="10.88.0.22")

    async def scenario(etcd):
        lease_id = await metaservers.register_self(etcd, settings)
        renewer = asyncio.create_task(metaservers.keep_registered(etcd, settings, lease_id, 0.2))
        try:
            await etcd.lease_revoke(lease_id)  # como si hubiera vencido
            await asyncio.sleep(1)
            kv = await etcd.get(f"/nodes/meta/{meta_id}")
        finally:
            renewer.cancel()
        assert kv is not None and kv.lease != lease_id
        await etcd.lease_revoke(kv.lease)

    with_etcd(scenario)


def test_register_and_heartbeat(environment):
    node_id = f"dn-{uuid.uuid4().hex[:8]}"
    ip = "10.88.0.31"
    body = {
        "node_id": node_id,
        "disk_id": "disco-1",
        "ip": ip,
        "public_port": 9443,
        "internal_port": 9444,
        "zone": "us-east-1a",
        "free_bytes": 100,
    }
    user = f"t{uuid.uuid4().hex[:12]}"
    hash = "6ce5a55b446b43e4e935c44695a9a09f161e8e87d1ff2359adebc5f9ee5ee5cf"
    order = {
        "order_id": "o1",
        "type": "copy",
        "user": user,
        "hash": hash,
        "target": {"node_id": "dn-destino", "address": "10.88.0.32:9444"},
    }

    async def prepare(etcd):
        await etcd.put(f"/blocks/{user}/{hash}", {"size": 1, "stored_on": [node_id], "touched_at": 1})
        await etcd.put(f"/orders/{node_id}/o1", order)

    async def cleanup(etcd):
        await delete_prefixes(etcd, f"/blocks/{user}/", f"/orders/{node_id}/", f"/nodes/disk/{node_id}")
        kv = await etcd.get(f"/nodes/dn/{node_id}")
        if kv is not None:
            with contextlib.suppress(LeaseNotFound):
                await etcd.lease_revoke(kv.lease)

    with_etcd(prepare)
    try:
        with TestClient(metaserver_internal, client=(ip, 50000)) as internal:
            registered = internal.post("/api/v1/internal/register", json=body).json()
            lease_id = lease_from_hex(registered["lease_id"])
            assert registered["heartbeat_s"] == 5
            assert with_etcd(lambda etcd: etcd.lease_keys(lease_id)) == [f"/nodes/dn/{node_id}"]

            beat = {"node_id": node_id, "lease_id": registered["lease_id"], "free_bytes": 50, "done_orders": []}
            assert internal.post("/api/v1/internal/heartbeat", json=beat).json() == {"orders": [order]}
            done = {**beat, "done_orders": ["o1"]}
            assert internal.post("/api/v1/internal/heartbeat", json=done).json() == {"orders": []}
            block = with_etcd(lambda etcd: etcd.get(f"/blocks/{user}/{hash}")).json()
            assert block["stored_on"] == [node_id, "dn-destino"]
            assert with_etcd(lambda etcd: etcd.get(f"/nodes/dn/{node_id}")).json()["free_bytes"] == 50

            # Un disco nuevo saca al nodo de los stored_on, y el lease viejo ya no sirve para latir.
            again = internal.post("/api/v1/internal/register", json={**body, "disk_id": "disco-2"}).json()
            block = with_etcd(lambda etcd: etcd.get(f"/blocks/{user}/{hash}")).json()
            assert block["stored_on"] == ["dn-destino"]
            assert internal.post("/api/v1/internal/heartbeat", json=beat).json()["error"] == "E_NOT_FOUND"
            assert internal.post("/api/v1/internal/heartbeat", json={**beat, "lease_id": again["lease_id"]}).status_code == 200
    finally:
        with_etcd(cleanup)


def test_admin_status_shows_the_leader_of_a_real_election(environment, client):
    # El lifespan hace campaña con su meta_id (tarea 19). Puede quedar detrás de un candidato de
    # una prueba anterior hasta que el lease de ese candidato venza (DFS_NODE_LEASE_S).
    for _ in range(40):
        response = client.get("/api/v1/admin/status", headers=bearer("admin", admin=True))
        assert response.status_code == 200, response.json()
        if response.json()["leader"] == environment:
            break
        time.sleep(0.5)
    assert response.json()["leader"] == environment
    assert environment in [meta["meta_id"] for meta in response.json()["metaservers"]]
