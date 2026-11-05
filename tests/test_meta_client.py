"""Pruebas de MetaClient (tarea 14): cómo el DataNode encuentra un MetaServer y le habla (DA-11).

Los MetaServers se simulan con httpx.MockTransport: el manejador ve a qué IP y puerto va cada
petición. Descubrimiento por el puerto público (8443); llamadas por el interno (8444).
"""

import httpx
import pytest

from datanode.meta_client import MetaClient
from dfs_common.config import Settings
from dfs_common.errors import DFSError, ErrorCode

pytestmark = pytest.mark.anyio

SEEDS = ["10.88.0.21", "10.88.0.22", "10.88.0.23"]
CONFIRM = "/api/v1/internal/blocks/confirm"


def listing(*ips: str) -> dict:
    return {"metaservers": [{"meta_id": f"meta{i}", "ip": ip, "public_port": 8443, "internal_port": 8444} for i, ip in enumerate(ips, 1)]}


class Cluster:
    """MetaServers simulados. down: IP sin conexión; busy: IP que responde 503; answers: respuesta de cada llamada interna."""

    def __init__(self, listings=None, down=(), busy=()):
        self.listings = listings or [listing(*SEEDS)]  # lo que responde cada descubrimiento, en orden
        self.down, self.busy = set(down), set(busy)
        self.calls: list[tuple[str, int, str]] = []
        self.discoveries = 0  # descubrimientos que recibieron la lista
        self.answer = httpx.Response(200, json={"ok": True})

    def handler(self, request: httpx.Request) -> httpx.Response:
        host, port, path = request.url.host, request.url.port, request.url.path
        self.calls.append((host, port, path))
        if host in self.down:
            raise httpx.ConnectError("conexión rechazada", request=request)
        if host in self.busy:
            return httpx.Response(503, json={"error": "E_UNAVAILABLE", "message": "etcd sin mayoría", "retryable": True})
        if path == "/api/v1/cluster/metaservers":
            self.discoveries += 1
            return httpx.Response(200, json=self.listings[min(self.discoveries, len(self.listings)) - 1])
        return self.answer

    def internal_calls(self) -> list[str]:
        return [host for host, port, path in self.calls if port == 8444]

    def client(self) -> MetaClient:
        settings = Settings(_env_file=None, meta_seeds=SEEDS)
        return MetaClient(settings, None, transport=httpx.MockTransport(self.handler))


async def test_discovers_through_the_public_port_and_calls_the_internal_one():
    cluster = Cluster()
    async with cluster.client() as meta:
        assert await meta.post(CONFIRM, {"user": "ana"}) == {"ok": True}
    assert cluster.calls == [("10.88.0.21", 8443, "/api/v1/cluster/metaservers"), ("10.88.0.21", 8444, CONFIRM)]


async def test_discovery_tries_the_seeds_in_order():
    cluster = Cluster(down={"10.88.0.21"})
    cluster.listings = [listing("10.88.0.22", "10.88.0.23")]
    async with cluster.client() as meta:
        await meta.post(CONFIRM, {})
    assert [(host, port) for host, port, _ in cluster.calls] == [
        ("10.88.0.21", 8443),
        ("10.88.0.22", 8443),
        ("10.88.0.22", 8444),
    ]


async def test_a_connection_error_or_a_503_moves_to_the_next_metaserver_and_it_is_remembered():
    cluster = Cluster()
    async with cluster.client() as meta:
        await meta.post(CONFIRM, {})  # descubre con la primera semilla y llama a meta1
        cluster.down, cluster.busy = {"10.88.0.21"}, {"10.88.0.22"}
        await meta.post(CONFIRM, {})
        await meta.post(CONFIRM, {})
    # La segunda llamada prueba meta1 (sin conexión), meta2 (503) y meta3; la tercera empieza por meta3.
    assert cluster.internal_calls() == ["10.88.0.21", "10.88.0.21", "10.88.0.22", "10.88.0.23", "10.88.0.23"]


async def test_if_every_metaserver_fails_it_discovers_again_once_and_retries():
    cluster = Cluster(listings=[listing("10.88.0.21"), listing("10.88.0.23")])
    async with cluster.client() as meta:
        await meta.post(CONFIRM, {})
        cluster.busy = {"10.88.0.21"}  # meta1 deja de responder; la lista nueva tiene a meta3
        assert await meta.post(CONFIRM, {}) == {"ok": True}
    assert cluster.discoveries == 2
    assert cluster.internal_calls() == ["10.88.0.21", "10.88.0.21", "10.88.0.23"]


async def test_if_they_still_fail_after_discovering_again_it_is_e_unavailable():
    cluster = Cluster()
    async with cluster.client() as meta:
        await meta.post(CONFIRM, {})
        cluster.down = set(SEEDS)  # ningún MetaServer responde, ni para descubrir
        with pytest.raises(DFSError) as caught:
            await meta.post(CONFIRM, {})
    assert caught.value.code == ErrorCode.E_UNAVAILABLE


async def test_without_any_seed_answering_it_is_e_unavailable():
    cluster = Cluster(down=set(SEEDS))
    async with cluster.client() as meta:
        with pytest.raises(DFSError) as caught:
            await meta.post(CONFIRM, {})
    assert caught.value.code == ErrorCode.E_UNAVAILABLE
    assert cluster.internal_calls() == []


@pytest.mark.parametrize(
    ("status", "code"),
    [(422, "E_REPLICAS"), (403, "E_FORBIDDEN"), (404, "E_NOT_FOUND"), (500, "E_INTERNAL")],
)
async def test_an_error_answer_becomes_a_dfs_error_with_the_same_code(status, code):
    cluster = Cluster()
    cluster.answer = httpx.Response(status, json={"error": code, "message": "mensaje del MetaServer", "retryable": False})
    async with cluster.client() as meta:
        with pytest.raises(DFSError) as caught:
            await meta.post(CONFIRM, {})
    assert (caught.value.code, caught.value.message) == (code, "mensaje del MetaServer")
    assert cluster.internal_calls() == ["10.88.0.21"]  # quien llama decide: no se prueba otro MetaServer


async def test_an_answer_that_is_not_from_the_api_is_e_unavailable():
    cluster = Cluster()
    cluster.answer = httpx.Response(502, text="<html>Bad Gateway</html>")
    async with cluster.client() as meta:
        with pytest.raises(DFSError) as caught:
            await meta.post(CONFIRM, {})
    assert caught.value.code == ErrorCode.E_UNAVAILABLE
