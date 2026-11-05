"""Pruebas de DfsApi (tarea 16): el formato de cada llamada, el paso al siguiente MetaServer y los errores.

Los MetaServers y DataNodes se simulan con httpx.MockTransport: el manejador ve a qué IP,
puerto y ruta va cada petición. El cliente descubre y llama por el puerto público (8443).
"""

import json

import httpx
import pytest

from dfs_client.api import DfsApi
from dfs_common.config import Settings
from dfs_common.errors import DFSError, ErrorCode
from dfs_common.models import (
    AdminStatusResponse,
    BlockSpec,
    BlockStoredResponse,
    CommitResponse,
    FileBlocksResponse,
    LockResponse,
    LoginResponse,
    LsResponse,
    OkResponse,
    ReallocResponse,
    StatResponse,
    UploadResponse,
)

SEEDS = ["10.88.0.21", "10.88.0.22", "10.88.0.23"]
HASH = "b64bb9046f61749097db060781bcbbba317c08ff0ac378e9aed9a6d18217e449"
DEFAULT_TIMEOUT = 5.0  # el de httpx por defecto


def example(model) -> dict:
    return model.model_config["json_schema_extra"]["examples"][0]


def listing(*ips: str) -> dict:
    metas = [{"meta_id": f"meta{i}", "ip": ip, "public_port": 8443, "internal_port": 8444} for i, ip in enumerate(ips, 1)]
    return {"metaservers": metas}


def error(status: int, code: str, message: str = "algo falló") -> httpx.Response:
    return httpx.Response(status, json={"error": code, "message": message, "retryable": False})


class Cluster:
    """MetaServers simulados. down: IP sin conexión; busy: IP que responde 503.

    answer: respuesta de las llamadas que no son el descubrimiento (por defecto {"ok": true}).
    """

    def __init__(self, down=(), busy=(), listings=None):
        self.down, self.busy = set(down), set(busy)
        self.listings = listings or [listing(*SEEDS)]
        self.discoveries = 0
        self.requests: list[httpx.Request] = []
        self.answer: httpx.Response | None = None

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.host in self.down:
            raise httpx.ConnectError("conexión rechazada", request=request)
        if request.url.host in self.busy:
            return error(503, "E_UNAVAILABLE", "etcd sin mayoría")
        if request.url.path == "/api/v1/cluster/metaservers":
            self.discoveries += 1
            return httpx.Response(200, json=self.listings[min(self.discoveries, len(self.listings)) - 1])
        return self.answer or httpx.Response(200, json={"ok": True})

    def api(self, token: str | None = "jwt", **settings) -> DfsApi:
        config = Settings(_env_file=None, meta_seeds=SEEDS, zone="us-east-1a", **settings)
        return DfsApi(config, token, transport=httpx.MockTransport(self.handler))

    def calls(self) -> list[tuple[str, int, str]]:
        return [(r.url.host, r.url.port, r.url.path) for r in self.requests]

    def last(self) -> httpx.Request:
        return self.requests[-1]


def body(request: httpx.Request) -> dict:
    return json.loads(request.content)


def read_timeout(request: httpx.Request) -> float:
    return request.extensions["timeout"]["read"]


# --- Descubrimiento y paso al siguiente MetaServer ---


def test_discovers_through_the_first_seed_and_uses_the_public_port():
    cluster = Cluster()
    cluster.api().mkdir("/docs")
    assert cluster.calls() == [
        ("10.88.0.21", 8443, "/api/v1/cluster/metaservers"),
        ("10.88.0.21", 8443, "/api/v1/fs/mkdir"),
    ]


def test_discovery_tries_the_seeds_in_order():
    cluster = Cluster(down={"10.88.0.21"}, listings=[listing("10.88.0.22", "10.88.0.23")])
    cluster.api().mkdir("/docs")
    assert [(host, path) for host, _, path in cluster.calls()] == [
        ("10.88.0.21", "/api/v1/cluster/metaservers"),
        ("10.88.0.22", "/api/v1/cluster/metaservers"),
        ("10.88.0.22", "/api/v1/fs/mkdir"),
    ]


def test_a_seed_that_answers_503_is_skipped():
    cluster = Cluster(busy={"10.88.0.21"})
    cluster.api().mkdir("/docs")
    assert cluster.calls()[1] == ("10.88.0.22", 8443, "/api/v1/cluster/metaservers")


def test_without_seeds_it_is_unavailable():
    api = DfsApi(Settings(_env_file=None, meta_seeds=[]), "jwt", transport=httpx.MockTransport(Cluster().handler))
    with pytest.raises(DFSError) as caught:
        api.ls("/")
    assert caught.value.code == ErrorCode.E_UNAVAILABLE


def test_a_metaserver_down_or_answering_503_passes_to_the_next_and_is_remembered():
    cluster = Cluster()
    api = cluster.api()
    api.mkdir("/a")  # descubre con meta1
    cluster.down.add("10.88.0.21")
    cluster.busy.add("10.88.0.22")
    api.mkdir("/b")
    api.mkdir("/c")  # empieza por meta3, el último que respondió
    hosts = [host for host, _, path in cluster.calls() if path == "/api/v1/fs/mkdir"]
    assert hosts == ["10.88.0.21", "10.88.0.21", "10.88.0.22", "10.88.0.23", "10.88.0.23"]


def test_if_no_metaserver_answers_it_rediscovers_once_and_fails_with_503():
    cluster = Cluster()
    api = cluster.api()
    api.mkdir("/a")
    cluster.down.update(SEEDS)
    with pytest.raises(DFSError) as caught:
        api.mkdir("/b")
    assert caught.value.code == ErrorCode.E_UNAVAILABLE
    mkdirs = [host for host, _, path in cluster.calls() if path == "/api/v1/fs/mkdir"]
    assert len(mkdirs) == 1 + 3  # el primero, y los tres que no respondieron
    assert [path for _, _, path in cluster.calls()].count("/api/v1/cluster/metaservers") == 1 + 3


def test_the_list_from_discovery_replaces_the_seeds():
    cluster = Cluster(listings=[listing("10.88.0.99")])
    cluster.api().mkdir("/docs")
    assert cluster.calls()[1] == ("10.88.0.99", 8443, "/api/v1/fs/mkdir")


# --- Formato de cada llamada ---


def test_login_sends_no_token_and_keeps_the_new_one():
    cluster = Cluster()
    cluster.answer = httpx.Response(200, json=example(LoginResponse))
    api = cluster.api(token=None)
    answer = api.login("ana", "clave")
    request = cluster.last()
    assert (request.method, request.url.path) == ("POST", "/api/v1/auth/login")
    assert body(request) == {"user": "ana", "password": "clave"}
    assert "authorization" not in request.headers
    assert answer == LoginResponse.model_validate(example(LoginResponse))
    assert api.token == answer.token


def test_discovery_sends_no_token():
    cluster = Cluster()
    cluster.api().mkdir("/docs")
    assert "authorization" not in cluster.requests[0].headers


QUERY_CALLS = [
    # (llamada, modelo de la respuesta, método, ruta, parámetros)
    (lambda api: api.ls("/docs"), LsResponse, "GET", "/api/v1/fs/ls", {"path": "/docs"}),
    (lambda api: api.stat("/docs"), StatResponse, "GET", "/api/v1/fs/stat", {"path": "/docs"}),
    (lambda api: api.rmdir("/docs"), OkResponse, "DELETE", "/api/v1/fs/rmdir", {"path": "/docs"}),
    (lambda api: api.rm("/docs/a"), OkResponse, "DELETE", "/api/v1/fs/rm", {"path": "/docs/a"}),
    (lambda api: api.admin_status(), AdminStatusResponse, "GET", "/api/v1/admin/status", {}),
    (lambda api: api.renew_lock("694d8a1c2e3b4f05"), LockResponse, "PUT", "/api/v1/locks/694d8a1c2e3b4f05", {}),
    (lambda api: api.unlock("694d8a1c2e3b4f05"), OkResponse, "DELETE", "/api/v1/locks/694d8a1c2e3b4f05", {}),
]


@pytest.mark.parametrize("call, model, method, path, params", QUERY_CALLS)
def test_calls_without_body(call, model, method, path, params):
    cluster = Cluster()
    cluster.answer = httpx.Response(200, json=example(model))
    answer = call(cluster.api())
    request = cluster.last()
    assert (request.method, request.url.path, dict(request.url.params)) == (method, path, params)
    assert request.headers["authorization"] == "Bearer jwt"
    assert read_timeout(request) == DEFAULT_TIMEOUT
    assert answer == model.model_validate(example(model))


BLOCKS = [BlockSpec(index=0, hash=HASH, size=10)]

BODY_CALLS = [
    (lambda api: api.mkdir("/docs"), OkResponse, "/api/v1/fs/mkdir", {"path": "/docs"}),
    (
        lambda api: api.add_user("bruno", "clave"),
        OkResponse,
        "/api/v1/admin/users",
        {"user": "bruno", "password": "clave"},
    ),
    (
        lambda api: api.uploads(path="/a", mode="w", base_version=0, block_size=8, lock_id="1f", blocks=BLOCKS),
        UploadResponse,
        "/api/v1/uploads",
        {
            "path": "/a",
            "mode": "w",
            "base_version": 0,
            "block_size": 8,
            "lock_id": "1f",
            "blocks": [{"index": 0, "hash": HASH, "size": 10}],
        },
    ),
    (
        # Los bloques también pueden ir como tuplas (index, hash, size).
        lambda api: api.uploads(path="/a", mode="a", base_version=2, block_size=8, lock_id="1f", blocks=[(0, HASH, 10)]),
        UploadResponse,
        "/api/v1/uploads",
        {
            "path": "/a",
            "mode": "a",
            "base_version": 2,
            "block_size": 8,
            "lock_id": "1f",
            "blocks": [{"index": 0, "hash": HASH, "size": 10}],
        },
    ),
    (
        lambda api: api.realloc("u-1", indices=[2], exclude_nodes=["dn4"]),
        ReallocResponse,
        "/api/v1/uploads/u-1/realloc",
        {"indices": [2], "exclude_nodes": ["dn4"]},
    ),
    (
        lambda api: api.commit("u-1", base_version=3, lock_id="1f", hashes=[HASH], size=10, file_sha256=None),
        CommitResponse,
        "/api/v1/uploads/u-1/commit",
        {"base_version": 3, "lock_id": "1f", "hashes": [HASH], "size": 10, "file_sha256": None},
    ),
]


@pytest.mark.parametrize("call, model, path, expected_body", BODY_CALLS)
def test_calls_with_body(call, model, path, expected_body):
    cluster = Cluster()
    cluster.answer = httpx.Response(200, json=example(model))
    answer = call(cluster.api())
    request = cluster.last()
    assert (request.method, request.url.path) == ("POST", path)
    assert body(request) == expected_body
    assert request.headers["authorization"] == "Bearer jwt"
    assert answer == model.model_validate(example(model))


def test_lock_waits_wait_s_plus_the_renewal_interval():
    cluster = Cluster()
    cluster.answer = httpx.Response(200, json=example(LockResponse))
    answer = cluster.api(lock_renew_s=7).lock("/docs/a", wait_s=20)
    request = cluster.last()
    assert (request.method, request.url.path, body(request)) == ("POST", "/api/v1/locks", {"path": "/docs/a", "wait_s": 20})
    assert read_timeout(request) == 20 + 7
    assert answer.lock_id == "694d8a1c2e3b4f05"


def test_lock_waits_30_s_by_default():
    cluster = Cluster()
    cluster.answer = httpx.Response(200, json=example(LockResponse))
    cluster.api().lock("/docs/a")
    assert body(cluster.last())["wait_s"] == 30


def test_files_blocks_sends_the_zone_and_the_version():
    cluster = Cluster()
    cluster.answer = httpx.Response(200, json=example(FileBlocksResponse))
    answer = cluster.api().files_blocks("/docs/informe.pdf", version=4)
    request = cluster.last()
    assert (request.method, request.url.path) == ("GET", "/api/v1/files/blocks")
    assert dict(request.url.params) == {"path": "/docs/informe.pdf", "version": "4"}
    assert request.headers["x-client-zone"] == "us-east-1a"
    assert answer.blocks[0].pass_ == example(FileBlocksResponse)["blocks"][0]["pass"]


def test_files_blocks_without_version_asks_for_the_current_one():
    cluster = Cluster()
    cluster.answer = httpx.Response(200, json=example(FileBlocksResponse))
    cluster.api().files_blocks("/docs/informe.pdf")
    assert dict(cluster.last().url.params) == {"path": "/docs/informe.pdf"}


# --- Bloques: directo al DataNode, sin MetaServer ---


def test_put_block_goes_to_the_datanode_with_the_pass():
    cluster = Cluster()
    cluster.answer = httpx.Response(201, json=example(BlockStoredResponse))
    answer = cluster.api(primary_wait_s=4).put_block("172.31.32.10:9443", HASH, "pase-w", b"datos")
    request = cluster.last()
    assert cluster.calls() == [("172.31.32.10", 9443, f"/api/v1/blocks/{HASH}")]
    assert request.method == "PUT"
    assert request.headers["authorization"] == "Bearer pase-w"
    assert request.headers["content-type"] == "application/octet-stream"
    assert request.content == b"datos"
    assert read_timeout(request) == 3 * 4
    assert answer.stored_on == ["dn3", "dn4", "dn1"]


def test_get_block_returns_the_bytes():
    cluster = Cluster()
    cluster.answer = httpx.Response(200, content=b"contenido")
    data = cluster.api(primary_wait_s=4).get_block("172.31.0.10:9443", HASH, "pase-r")
    request = cluster.last()
    assert (request.method, request.url.host, request.url.path) == ("GET", "172.31.0.10", f"/api/v1/blocks/{HASH}")
    assert request.headers["authorization"] == "Bearer pase-r"
    assert read_timeout(request) == 3 * 4
    assert data == b"contenido"


@pytest.mark.parametrize("call", [
    lambda api: api.put_block("172.31.0.10:9443", HASH, "p", b"x"),
    lambda api: api.get_block("172.31.0.10:9443", HASH, "p"),
])
def test_a_datanode_that_does_not_answer_is_unavailable(call):
    cluster = Cluster(down={"172.31.0.10"})
    with pytest.raises(DFSError) as caught:
        call(cluster.api())
    assert caught.value.code == ErrorCode.E_UNAVAILABLE
    assert "172.31.0.10:9443" in caught.value.message


@pytest.mark.parametrize("status, code", [(422, "E_CHECKSUM"), (401, "E_AUTH"), (404, "E_NOT_FOUND"), (503, "E_UNAVAILABLE")])
def test_block_errors_keep_their_code(status, code):
    cluster = Cluster()
    cluster.answer = error(status, code, "mensaje del DataNode")
    with pytest.raises(DFSError) as caught:
        cluster.api().get_block("172.31.0.10:9443", HASH, "p")
    assert (caught.value.code, caught.value.message) == (ErrorCode(code), "mensaje del DataNode")


# --- Errores del MetaServer ---


@pytest.mark.parametrize(
    "status, code",
    [(400, "E_BAD_REQUEST"), (401, "E_AUTH"), (403, "E_FORBIDDEN"), (404, "E_NOT_FOUND"), (409, "E_EXISTS"),
     (423, "E_LOCKED"), (500, "E_INTERNAL")],
)
def test_an_error_answer_becomes_a_dfs_error_with_the_same_code(status, code):
    cluster = Cluster()
    cluster.answer = error(status, code, "mensaje del servidor")
    with pytest.raises(DFSError) as caught:
        cluster.api().mkdir("/docs")
    assert (caught.value.code, caught.value.message) == (ErrorCode(code), "mensaje del servidor")
    # No pasa al siguiente MetaServer: la respuesta es la de la operación.
    assert [path for _, _, path in cluster.calls()].count("/api/v1/fs/mkdir") == 1


def test_an_answer_that_is_not_an_api_error_is_unavailable():
    cluster = Cluster()
    cluster.answer = httpx.Response(502, text="Bad Gateway")
    with pytest.raises(DFSError) as caught:
        cluster.api().ls("/")
    assert caught.value.code == ErrorCode.E_UNAVAILABLE
    assert "502" in caught.value.message
