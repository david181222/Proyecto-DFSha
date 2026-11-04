"""Pruebas de la tarea 09: PUT /api/v1/internal/blocks/{user}/{hash} del DataNode.

Las pruebas reemplazan el almacén y la configuración con app.dependency_overrides, sin
correr el lifespan; la última corre el lifespan de verdad con una carpeta temporal.
"""

import hashlib
import os

import pytest
from fastapi.testclient import TestClient

from datanode import deps
from datanode.internal_api import app
from datanode.storage import BlockStore
from dfs_common.config import Settings

MIB = 1024 * 1024
KEY = bytes(range(32))
BLOCK = b"bloque de prueba " * 1000
HASH = hashlib.sha256(BLOCK).hexdigest()
URL = f"/api/v1/internal/blocks/ana/{HASH}"


@pytest.fixture
def store(tmp_path):
    return BlockStore(tmp_path, KEY, 8 * MIB)


@pytest.fixture
def client(store):
    app.dependency_overrides[deps.get_store] = lambda: store
    app.dependency_overrides[deps.get_settings] = lambda: Settings(node_id="dn4")
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_put_stores_the_block_and_answers_201_with_node_id(client, store):
    response = client.put(URL, content=BLOCK)
    assert response.status_code == 201
    assert response.json() == {"hash": HASH, "node_id": "dn4"}
    assert store.read("ana", HASH) == BLOCK


def test_put_with_wrong_content_is_422_checksum(client, tmp_path):
    response = client.put(URL, content=b"otro contenido")
    assert response.status_code == 422
    assert response.json()["error"] == "E_CHECKSUM"
    assert response.json()["retryable"] is True
    assert not any(path.is_file() for path in tmp_path.rglob("*"))


def test_put_bigger_than_the_block_size_is_400(tmp_path):
    small_store = BlockStore(tmp_path, KEY, 1000)
    app.dependency_overrides[deps.get_store] = lambda: small_store
    app.dependency_overrides[deps.get_settings] = lambda: Settings(node_id="dn4")
    try:
        response = TestClient(app).put(URL, content=BLOCK)
    finally:
        app.dependency_overrides.clear()
    assert response.status_code == 400
    assert response.json()["error"] == "E_BAD_REQUEST"
    assert not any(path.is_file() for path in tmp_path.rglob("*"))


def test_unexpected_exception_responds_500_internal(caplog):
    class BrokenStore:
        async def write(self, user, hash, chunks):
            raise RuntimeError("detalle interno que no debe salir")

    app.dependency_overrides[deps.get_store] = lambda: BrokenStore()
    app.dependency_overrides[deps.get_settings] = lambda: Settings(node_id="dn4")
    try:
        # Starlette vuelve a lanzar la excepción después de responder; aquí solo interesa la respuesta.
        response = TestClient(app, raise_server_exceptions=False).put(URL, content=BLOCK)
    finally:
        app.dependency_overrides.clear()
    assert response.status_code == 500
    assert response.json() == {"error": "E_INTERNAL", "message": "error inesperado del servidor", "retryable": False}
    assert any(record.levelname == "ERROR" and record.exc_info for record in caplog.records)


def test_lifespan_opens_the_store_in_the_data_dir(tmp_path, monkeypatch):
    key_file = tmp_path / "node_key"
    key_file.write_bytes(os.urandom(32))
    data_dir = tmp_path / "data"
    monkeypatch.setenv("DFS_DATA_DIR", str(data_dir))
    monkeypatch.setenv("DFS_NODE_KEY_FILE", str(key_file))
    pass_key_file = tmp_path / "pass_key"
    pass_key_file.write_bytes(os.urandom(32))
    monkeypatch.setenv("DFS_PASS_KEY_FILE", str(pass_key_file))
    monkeypatch.setenv("DFS_NODE_ID", "dn2")
    monkeypatch.setattr(deps, "_store", None)  # el almacén compartido se crea de nuevo

    with TestClient(app) as client:
        response = client.put(URL, content=BLOCK)

    assert response.status_code == 201
    assert response.json()["node_id"] == "dn2"
    assert (data_dir / "ana" / HASH).is_file()
