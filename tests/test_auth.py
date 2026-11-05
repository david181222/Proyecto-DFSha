"""Pruebas de la tarea 07: login, crear usuarios, require_user/require_admin y primer administrador.

Usan FakeEtcd (tests/fakes.py). Las pruebas del primer administrador corren el lifespan
de verdad (TestClient dentro de un with), con connect_etcd reemplazado por el FakeEtcd.
"""

import os
import time

import anyio
import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient

from dfs_common.config import Settings
from dfs_common.security import create_jwt, verify_jwt
from metaserver import auth, deps
from metaserver.public_api import app as metaserver_public

from fakes import JWT_SECRET, PASS_KEY, FakeEtcd, bearer, public_client

LOGIN = "/api/v1/auth/login"
USERS = "/api/v1/admin/users"
PASSWORD = "caballo-correcto-bateria"


def add_user(etcd: FakeEtcd, user: str, password: str, *, is_admin: bool = False) -> None:
    etcd.seed(f"/users/{user}", {"password_hash": PasswordHasher().hash(password), "is_admin": is_admin})


# --- POST /auth/login ---


@pytest.mark.parametrize("is_admin", [False, True])
def test_login_returns_a_jwt_with_the_stored_role_and_the_configured_ttl(is_admin):
    etcd = FakeEtcd()
    add_user(etcd, "ana", PASSWORD, is_admin=is_admin)
    with public_client(etcd, jwt_ttl_s=1234) as client:
        response = client.post(LOGIN, json={"user": "ana", "password": PASSWORD})

    assert response.status_code == 200
    body = response.json()
    assert body["expires_in"] == 1234
    claims = verify_jwt(body["token"], JWT_SECRET)
    assert (claims.sub, claims.admin) == ("ana", is_admin)
    assert claims.exp - claims.iat == 1234


def test_wrong_password_and_unknown_user_give_the_same_401():
    etcd = FakeEtcd()
    add_user(etcd, "ana", PASSWORD)
    with public_client(etcd) as client:
        wrong_password = client.post(LOGIN, json={"user": "ana", "password": "otra"})
        unknown_user = client.post(LOGIN, json={"user": "nadie", "password": PASSWORD})

    assert wrong_password.status_code == unknown_user.status_code == 401
    assert wrong_password.json() == unknown_user.json()
    assert wrong_password.json()["error"] == "E_AUTH"


def test_login_with_an_invalid_body_is_bad_request():
    with public_client() as client:
        response = client.post(LOGIN, json={"user": "Ana Mayúscula", "password": PASSWORD})
    assert response.status_code == 400
    assert response.json()["error"] == "E_BAD_REQUEST"


# --- JWT en las rutas protegidas ---


@pytest.mark.parametrize(
    "headers",
    [
        {},
        bearer("admin", admin=True, ttl_s=60, now=int(time.time()) - 3600),  # vencido hace casi una hora
        {"Authorization": "Bearer no-es-un-jwt"},
        {"Authorization": "Basic YWRtaW46Y2xhdmU="},  # otro esquema
    ],
    ids=["sin-token", "vencido", "basura", "otro-esquema"],
)
@pytest.mark.parametrize(
    ("method", "path", "request_args"),
    [
        ("POST", USERS, {"json": {"user": "bruno", "password": "x"}}),
        ("GET", "/api/v1/fs/ls", {"params": {"path": "/"}}),
    ],
    ids=["admin-users", "fs-ls"],
)
def test_missing_expired_or_garbage_token_is_401(headers, method, path, request_args):
    with public_client() as client:
        response = client.request(method, path, headers=headers, **request_args)
    assert response.status_code == 401
    assert response.json()["error"] == "E_AUTH"


def test_token_signed_with_another_key_is_401():
    forged = create_jwt("ana", True, b"o" * 32, 3600)
    with public_client() as client:
        response = client.get("/api/v1/fs/ls", params={"path": "/"}, headers={"Authorization": f"Bearer {forged}"})
    assert response.status_code == 401


def test_admin_users_without_admin_role_is_403():
    with public_client() as client:
        response = client.post(USERS, json={"user": "bruno", "password": "x"}, headers=bearer("ana"))
    assert response.status_code == 403
    assert response.json()["error"] == "E_FORBIDDEN"


# --- POST /admin/users ---


def test_create_user_then_repeat_is_409_and_the_new_user_can_log_in():
    etcd = FakeEtcd()
    admin = bearer("admin", admin=True)
    with public_client(etcd) as client:
        created = client.post(USERS, json={"user": "bruno", "password": "otra-clave-larga"}, headers=admin)
        repeated = client.post(USERS, json={"user": "bruno", "password": "cualquiera"}, headers=admin)
        login = client.post(LOGIN, json={"user": "bruno", "password": "otra-clave-larga"})

    assert created.status_code == 200
    assert created.json() == {"ok": True}
    assert repeated.status_code == 409
    assert repeated.json()["error"] == "E_EXISTS"
    assert login.status_code == 200
    assert verify_jwt(login.json()["token"], JWT_SECRET).admin is False  # los usuarios nuevos no son admin
    assert etcd.kvs["/users/bruno"].version == 1  # el segundo intento no lo tocó


def test_stored_password_hash_is_argon2id_and_does_not_contain_the_password():
    etcd = FakeEtcd()
    with public_client(etcd) as client:
        client.post(USERS, json={"user": "bruno", "password": "otra-clave-larga"}, headers=bearer("admin", admin=True))

    stored = etcd.value("/users/bruno")
    assert stored["password_hash"].startswith("$argon2id$")
    assert "otra-clave-larga" not in stored["password_hash"]
    assert stored == {"password_hash": stored["password_hash"], "is_admin": False}


# --- Primer administrador (lifespan) ---


@pytest.fixture
def startup(monkeypatch, tmp_path):
    """Prepara el arranque de la app pública: un FakeEtcd en lugar de etcd y la clave del JWT.

    Devuelve el FakeEtcd que comparten todos los arranques (como los 3 MetaServers con un
    mismo etcd) y la carpeta donde poner los secretos.
    """
    for key in list(os.environ):
        if key.upper().startswith("DFS_"):
            monkeypatch.delenv(key)
    monkeypatch.chdir(tmp_path)  # sin .env: solo cuentan las variables de abajo
    (tmp_path / "jwt_secret").write_bytes(JWT_SECRET)
    monkeypatch.setenv("DFS_JWT_SECRET_FILE", str(tmp_path / "jwt_secret"))
    (tmp_path / "pass_key").write_bytes(PASS_KEY)  # la reserva y la lectura firman pases (tarea 12)
    monkeypatch.setenv("DFS_PASS_KEY_FILE", str(tmp_path / "pass_key"))
    monkeypatch.setenv("DFS_NODE_ID", "meta1")  # el MetaServer se registra en /nodes/meta/ (tarea 13)
    monkeypatch.setenv("DFS_NODE_IP", "172.31.0.10")
    etcd = FakeEtcd()
    monkeypatch.setattr(deps, "connect_etcd", lambda settings: etcd)
    return etcd, tmp_path


def test_three_startups_create_a_single_admin(startup, monkeypatch):
    etcd, folder = startup
    # Con espacios y salto de línea al final, que se quitan.
    (folder / "admin_password").write_text("clave-del-admin \n", encoding="utf-8")
    monkeypatch.setenv("DFS_ADMIN_PASSWORD_FILE", str(folder / "admin_password"))

    for _ in range(3):
        with TestClient(metaserver_public) as client:
            login = client.post(LOGIN, json={"user": "admin", "password": "clave-del-admin"})
            assert login.status_code == 200
            assert verify_jwt(login.json()["token"], JWT_SECRET).admin is True

    assert etcd.keys("/users/") == ["/users/admin"]
    assert etcd.kvs["/users/admin"].version == 1  # lo creó el primer arranque; los otros dos no lo tocaron
    assert etcd.value("/users/admin")["is_admin"] is True
    assert etcd.closed  # al apagar, el lifespan cierra la conexión con etcd


@pytest.mark.anyio
async def test_three_metaservers_starting_at_once_create_a_single_admin(tmp_path):
    (tmp_path / "admin_password").write_text("clave-del-admin", encoding="utf-8")
    settings = Settings(_env_file=None, admin_password_file=tmp_path / "admin_password")
    etcd = FakeEtcd()
    async with anyio.create_task_group() as tasks:
        for _ in range(3):
            tasks.start_soon(auth.create_first_admin, etcd, settings)
    assert etcd.keys("/users/") == ["/users/admin"]
    assert etcd.kvs["/users/admin"].version == 1


@pytest.mark.parametrize(("stored_password", "warns"), [("clave-del-admin", False), ("clave-vieja", True)])
def test_an_existing_admin_is_not_changed_and_a_different_password_is_logged(
    startup, monkeypatch, caplog, stored_password, warns
):
    # Por ejemplo: se regeneraron los secretos y etcd conservó sus datos.
    etcd, folder = startup
    add_user(etcd, "admin", stored_password, is_admin=True)
    (folder / "admin_password").write_text("clave-del-admin", encoding="utf-8")
    monkeypatch.setenv("DFS_ADMIN_PASSWORD_FILE", str(folder / "admin_password"))

    with TestClient(metaserver_public):
        pass

    assert etcd.kvs["/users/admin"].version == 1  # no se tocó
    assert ("no se cambia" in caplog.text) is warns


def test_without_admin_password_file_no_admin_is_created(startup):
    etcd, _ = startup
    with TestClient(metaserver_public):
        pass
    assert etcd.keys("/users/") == []


def test_empty_admin_password_file_stops_the_startup(startup, monkeypatch):
    etcd, folder = startup
    (folder / "admin_password").write_text(" \n", encoding="utf-8")
    monkeypatch.setenv("DFS_ADMIN_PASSWORD_FILE", str(folder / "admin_password"))
    with pytest.raises(ValueError, match="no tiene contraseña"):
        with TestClient(metaserver_public):
            pass
    assert etcd.keys("/users/") == []
