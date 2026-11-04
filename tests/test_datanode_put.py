"""Pruebas de la tarea 14: PUT /api/v1/blocks/{hash} completo en el DataNode primario (DA-04).

El primario guarda el bloque, lo reenvía a la vez a los secundarios del pase, espera hasta
DFS_PRIMARY_WAIT_S y confirma al MetaServer. Los secundarios y el MetaServer se simulan con
httpx.MockTransport; el almacén es uno de verdad, en una carpeta temporal.
"""

import asyncio
import hashlib
import json
import time
from contextlib import contextmanager

import httpx
import pytest
from fastapi.testclient import TestClient

from datanode import deps
from datanode.meta_client import MetaClient
from datanode.public_api import app
from datanode.storage import BlockStore
from dfs_common.config import Settings
from dfs_common.models import BlockPass
from dfs_common.security import sign_pass

MIB = 1024 * 1024
NODE_KEY = bytes(range(32))
PASS_KEY = b"p" * 32
BLOCK = b"bloque de ana " * 1000
HASH = hashlib.sha256(BLOCK).hexdigest()
# dn3 es el primario y dn4 y dn1 sus secundarios, como en el pase del ejemplo de docs/contratos.md.
SECONDARIES = {"172.31.16.20:9444": "dn4", "172.31.0.10:9444": "dn1"}
DN4, DN1 = SECONDARIES
CONFIRM = "/api/v1/internal/blocks/confirm"


class Secondaries:
    """Responden 201 con su node_id, salvo los de failing (500) y los de slow (tardan slow_s)."""

    def __init__(self, failing=(), slow=(), slow_s=0.0):
        self.failing, self.slow, self.slow_s = set(failing), set(slow), slow_s
        self.received: dict[str, bytes] = {}

    async def handler(self, request: httpx.Request) -> httpx.Response:
        address = f"{request.url.host}:{request.url.port}"
        assert (request.method, request.url.path) == ("PUT", f"/api/v1/internal/blocks/ana/{HASH}")
        self.received[address] = request.content
        if address in self.slow:
            await asyncio.sleep(self.slow_s)
        if address in self.failing:
            return httpx.Response(500, json={"error": "E_INTERNAL", "message": "falló", "retryable": False})
        return httpx.Response(201, json={"hash": HASH, "node_id": SECONDARIES[address]})


class MetaServer:
    """Descubrimiento y POST /internal/blocks/confirm, que responde answer. down: no responde nada."""

    def __init__(self, answer: httpx.Response | None = None, down: bool = False):
        self.answer = answer or httpx.Response(200, json={"ok": True})
        self.down = down
        self.confirms: list[dict] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        if self.down:
            raise httpx.ConnectError("conexión rechazada", request=request)
        if request.url.path == "/api/v1/cluster/metaservers":
            meta = {"meta_id": "meta1", "ip": "10.88.0.21", "public_port": 8443, "internal_port": 8444}
            return httpx.Response(200, json={"metaservers": [meta]})
        assert (request.url.port, request.url.path) == (8444, CONFIRM)
        self.confirms.append(json.loads(request.content))
        return self.answer


@pytest.fixture
def store(tmp_path):
    return BlockStore(tmp_path, NODE_KEY, 8 * MIB)


@contextmanager
def primary(store, *, secondaries=None, meta=None, **settings):
    """TestClient de la API pública del DataNode dn3, con sus clientes HTTP sobre MockTransport."""
    secondaries = secondaries or Secondaries()
    meta = meta or MetaServer()
    config = Settings(_env_file=None, node_id="dn3", meta_seeds=["10.88.0.21"], **settings)

    async def peers():
        async with httpx.AsyncClient(transport=httpx.MockTransport(secondaries.handler)) as client:
            yield client

    async def meta_client():
        async with MetaClient(config, None, transport=httpx.MockTransport(meta.handler)) as client:
            yield client

    app.dependency_overrides.update(
        {
            deps.get_store: lambda: store,
            deps.get_pass_key: lambda: PASS_KEY,
            deps.get_settings: lambda: config,
            deps.get_peers: peers,
            deps.get_meta_client: meta_client,
        }
    )
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def put(client, data: bytes = BLOCK, *, op: str = "w"):
    claims = BlockPass(
        user="ana",
        hash=HASH,
        op=op,
        secondaries=list(SECONDARIES) if op == "w" else None,
        exp=int(time.time()) + 600,
    )
    return client.put(f"/api/v1/blocks/{HASH}", content=data, headers={"Authorization": f"Bearer {sign_pass(claims, PASS_KEY)}"})


def assert_error(response, status: int, code: str) -> None:
    assert response.status_code == status, response.json()
    assert response.json()["error"] == code


def test_with_both_secondaries_it_answers_201_with_three_nodes_after_confirming(store):
    secondaries, meta = Secondaries(), MetaServer()
    with primary(store, secondaries=secondaries, meta=meta) as client:
        response = put(client)
    assert response.status_code == 201
    assert response.json() == {"hash": HASH, "stored_on": ["dn3", "dn4", "dn1"]}
    assert secondaries.received == {DN4: BLOCK, DN1: BLOCK}  # el bloque viaja sin cifrar (DA-09)
    assert meta.confirms == [{"user": "ana", "hash": HASH, "size": len(BLOCK), "stored_on": ["dn3", "dn4", "dn1"]}]
    assert store.read("ana", HASH) == BLOCK


def test_with_one_secondary_failing_and_w_2_it_answers_201_with_two_nodes(store):
    meta = MetaServer()
    with primary(store, secondaries=Secondaries(failing={DN4}), meta=meta, w=2) as client:
        response = put(client)
    assert response.json() == {"hash": HASH, "stored_on": ["dn3", "dn1"]}
    assert meta.confirms[0]["stored_on"] == ["dn3", "dn1"]


@pytest.mark.parametrize(("failing", "w"), [({DN4, DN1}, 2), ({DN4}, 3)], ids=["fallan-los-dos", "falla-uno-con-w-3"])
def test_without_dfs_w_copies_it_is_422_and_does_not_confirm(store, failing, w):
    meta = MetaServer()
    with primary(store, secondaries=Secondaries(failing=failing), meta=meta, w=w) as client:
        response = put(client)
    assert_error(response, 422, "E_REPLICAS")
    assert response.json()["retryable"] is True  # el cliente pide realloc
    assert meta.confirms == []
    assert store.read("ana", HASH) == BLOCK  # copia huérfana: queda en disco sin clave en etcd (aceptado)


def test_a_secondary_slower_than_dfs_primary_wait_s_does_not_count(store):
    secondaries = Secondaries(slow={DN1}, slow_s=5)
    started = time.monotonic()
    with primary(store, secondaries=secondaries, primary_wait_s=1) as client:
        response = put(client)
    assert response.json()["stored_on"] == ["dn3", "dn4"]
    assert time.monotonic() - started < 4  # no esperó los 5 s del lento


def test_the_secondaries_are_sent_at_the_same_time(store):
    secondaries = Secondaries(slow={DN4, DN1}, slow_s=1)
    started = time.monotonic()
    with primary(store, secondaries=secondaries, primary_wait_s=3) as client:
        response = put(client)
    assert response.json()["stored_on"] == ["dn3", "dn4", "dn1"]
    assert time.monotonic() - started < 1.9  # uno después del otro serían 2 s


def test_if_confirm_answers_422_the_client_gets_that_422(store):
    error = {"error": "E_REPLICAS", "message": "las copias vivas del bloque no cubren 2 zonas", "retryable": True}
    with primary(store, meta=MetaServer(answer=httpx.Response(422, json=error))) as client:
        response = put(client)
    assert response.status_code == 422
    assert response.json() == error


def test_if_no_metaserver_answers_it_is_503(store):
    with primary(store, meta=MetaServer(down=True)) as client:
        assert_error(put(client), 503, "E_UNAVAILABLE")


def test_a_body_that_does_not_match_the_hash_is_422_and_nothing_is_forwarded(store, tmp_path):
    secondaries, meta = Secondaries(), MetaServer()
    with primary(store, secondaries=secondaries, meta=meta) as client:
        assert_error(put(client, b"otro contenido"), 422, "E_CHECKSUM")
    assert secondaries.received == {} and meta.confirms == []
    assert not any(path.is_file() for path in tmp_path.rglob("*"))


def test_a_read_pass_is_403_before_reading_the_body(store, tmp_path):
    secondaries, meta = Secondaries(), MetaServer()
    with primary(store, secondaries=secondaries, meta=meta) as client:
        assert_error(put(client, op="r"), 403, "E_FORBIDDEN")
    assert secondaries.received == {} and meta.confirms == []
    assert not any(path.is_file() for path in tmp_path.rglob("*"))  # no guardó nada
