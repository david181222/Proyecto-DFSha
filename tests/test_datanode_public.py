"""Pruebas de la tarea 10: verificación de pases y GET /api/v1/blocks/{hash} del DataNode.

Los pases se arman con sign_pass y una clave de prueba. El almacén y la clave se
reemplazan con app.dependency_overrides, sin correr el lifespan; la última prueba
corre el lifespan de verdad con archivos temporales. El PUT completo (tarea 14) está en
tests/test_datanode_put.py.
"""

import asyncio
import hashlib
import importlib.util
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from datanode import deps
from datanode.public_api import app
from datanode.storage import BlockStore
from dfs_common.models import BlockPass
from dfs_common.security import sign_pass

MIB = 1024 * 1024
NODE_KEY = bytes(range(32))
PASS_KEY = b"p" * 32
OTHER_PASS_KEY = b"o" * 32
BLOCK = b"bloque de ana " * 1000
HASH = hashlib.sha256(BLOCK).hexdigest()
OTHER_HASH = hashlib.sha256(b"otro bloque").hexdigest()
URL = f"/api/v1/blocks/{HASH}"
SECONDARIES = ["172.31.16.20:9444", "172.31.0.10:9444"]


def make_pass(
    *, user: str = "ana", hash: str = HASH, op: str = "r", key: bytes = PASS_KEY, exp: int | None = None
) -> str:
    claims = BlockPass(
        user=user,
        hash=hash,
        op=op,
        secondaries=SECONDARIES if op == "w" else None,
        exp=int(time.time()) + 600 if exp is None else exp,
    )
    return sign_pass(claims, key)


def bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def chunks_of(data: bytes):
    yield data


def store_with_ana_block(folder) -> BlockStore:
    store = BlockStore(folder, NODE_KEY, 8 * MIB)
    asyncio.run(store.write("ana", HASH, chunks_of(BLOCK)))
    return store


@pytest.fixture
def store(tmp_path):
    return store_with_ana_block(tmp_path)


@pytest.fixture
def client(store):
    app.dependency_overrides[deps.get_store] = lambda: store
    app.dependency_overrides[deps.get_pass_key] = lambda: PASS_KEY
    yield TestClient(app)
    app.dependency_overrides.clear()


def error_of(response) -> tuple[int, str]:
    return response.status_code, response.json()["error"]


# --- GET con pase válido ---


def test_get_with_a_valid_pass_returns_exactly_the_stored_bytes(client):
    response = client.get(URL, headers=bearer(make_pass()))
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/octet-stream"
    assert response.content == BLOCK


# --- 401: sin pase o vencido ---


def test_get_without_header_is_401(client):
    assert error_of(client.get(URL)) == (401, "E_AUTH")


def test_get_with_another_scheme_is_401(client):
    assert error_of(client.get(URL, headers={"Authorization": f"Basic {make_pass()}"})) == (401, "E_AUTH")


def test_get_with_an_expired_pass_is_401(client):
    expired = make_pass(exp=int(time.time()) - 1)
    assert error_of(client.get(URL, headers=bearer(expired))) == (401, "E_AUTH")


# --- 403: pase inválido para esta petición ---


def test_pass_signed_with_another_key_is_403(client):
    assert error_of(client.get(URL, headers=bearer(make_pass(key=OTHER_PASS_KEY)))) == (403, "E_FORBIDDEN")


def test_pass_for_another_hash_is_403(client):
    assert error_of(client.get(URL, headers=bearer(make_pass(hash=OTHER_HASH)))) == (403, "E_FORBIDDEN")


def test_malformed_pass_is_403(client):
    assert error_of(client.get(URL, headers=bearer("no-es-un-pase"))) == (403, "E_FORBIDDEN")


def test_write_pass_on_get_is_403(client):
    assert error_of(client.get(URL, headers=bearer(make_pass(op="w")))) == (403, "E_FORBIDDEN")


def test_read_pass_on_put_is_403(client):
    response = client.put(URL, content=BLOCK, headers=bearer(make_pass(op="r")))
    assert error_of(response) == (403, "E_FORBIDDEN")


# --- Cada usuario solo ve sus bloques ---


def test_valid_pass_of_bob_for_a_block_only_ana_has_is_404(client, store):
    response = client.get(URL, headers=bearer(make_pass(user="bob")))
    assert error_of(response) == (404, "E_NOT_FOUND")  # se buscó en la carpeta de bob
    assert store.pop_bad_blocks() == [("bob", HASH)]


# --- Copia dañada ---


def test_damaged_copy_is_422_and_goes_to_bad_blocks(client, store, tmp_path):
    path = tmp_path / "ana" / HASH
    path.write_bytes(path.read_bytes()[:-1] + b"\x00")
    response = client.get(URL, headers=bearer(make_pass()))
    assert error_of(response) == (422, "E_CHECKSUM")
    assert response.json()["retryable"] is True
    assert store.pop_bad_blocks() == [("ana", HASH)]


# --- PUT: el pase se comprueba antes que todo lo demás (el resto, en tests/test_datanode_put.py) ---


def test_put_without_pass_is_401(client):
    assert error_of(client.put(URL, content=BLOCK)) == (401, "E_AUTH")


# --- Errores inesperados y lifespan ---


def test_unexpected_exception_responds_500_internal(caplog):
    class BrokenStore:
        def read(self, user, hash):
            raise RuntimeError("detalle interno que no debe salir")

    app.dependency_overrides[deps.get_store] = lambda: BrokenStore()
    app.dependency_overrides[deps.get_pass_key] = lambda: PASS_KEY
    try:
        # Starlette vuelve a lanzar la excepción después de responder; aquí solo interesa la respuesta.
        response = TestClient(app, raise_server_exceptions=False).get(URL, headers=bearer(make_pass()))
    finally:
        app.dependency_overrides.clear()
    assert response.status_code == 500
    assert response.json() == {"error": "E_INTERNAL", "message": "error inesperado del servidor", "retryable": False}
    assert any(record.levelname == "ERROR" and record.exc_info for record in caplog.records)


def test_lifespan_reads_the_pass_key_and_opens_the_store(tmp_path, monkeypatch):
    # Los clientes HTTP del primario (tarea 14) usan el certificado del nodo: uno real de la CA de prueba.
    spec = importlib.util.spec_from_file_location("gen_secrets", Path(__file__).parents[1] / "scripts" / "gen_secrets.py")
    gen_secrets = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gen_secrets)
    secrets = tmp_path / "secrets"
    gen_secrets.main(["--out", str(secrets), "--nodes", "dn1=10.88.0.31"])
    monkeypatch.setenv("DFS_TLS_CA_FILE", str(secrets / "ca.crt"))
    monkeypatch.setenv("DFS_TLS_CERT_FILE", str(secrets / "dn1" / "tls.crt"))
    monkeypatch.setenv("DFS_TLS_KEY_FILE", str(secrets / "dn1" / "tls.key"))
    data_dir = tmp_path / "data"
    store_with_ana_block(data_dir)
    node_key_file = tmp_path / "node_key"
    node_key_file.write_bytes(NODE_KEY)
    pass_key_file = tmp_path / "pass_key"
    pass_key_file.write_bytes(PASS_KEY)
    monkeypatch.setenv("DFS_DATA_DIR", str(data_dir))
    monkeypatch.setenv("DFS_NODE_KEY_FILE", str(node_key_file))
    monkeypatch.setenv("DFS_PASS_KEY_FILE", str(pass_key_file))
    monkeypatch.setattr(deps, "_store", None)  # el almacén compartido se crea de nuevo

    with TestClient(app) as client:
        response = client.get(URL, headers=bearer(make_pass()))
        peers, meta_client = app.state.peers, app.state.meta_client

    assert response.status_code == 200
    assert response.content == BLOCK
    assert peers.is_closed  # al apagar, el lifespan cierra los clientes HTTP
    assert meta_client._client.is_closed

