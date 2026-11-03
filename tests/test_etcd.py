"""Pruebas de metaserver.etcd con una pasarela simulada (httpx.MockTransport), sin etcd real.

Las respuestas imitan las de etcd 3.5.35 (tareas/06-08/contexto.md, sección 4): base64,
enteros de 64 bits como texto y sin los campos que valen 0, vacío o falso.
"""

import base64
import json

import httpx
import pytest

from dfs_common.errors import DFSError, ErrorCode
from metaserver.etcd import (
    Etcd,
    EtcdError,
    KeyValue,
    LeaderKey,
    LeaseNotFound,
    TxnResult,
    compare,
    lease_from_hex,
    lease_hex,
    lock_key,
    op_delete,
    op_put,
    op_range,
    prefix_end,
)

ENDPOINTS = ["http://etcd1:2379", "http://etcd2:2379"]
HEADER = {"header": {"cluster_id": "14841639068965178418", "revision": "9", "raft_term": "2"}}
LEASE = 0x694D8A1C2E3B4F05  # el lock_id del ejemplo de docs/contratos.md
NAME = "/locks/ana/docs/informe.pdf"
MINE = lock_key(NAME, LEASE)
LEASE_MSG = "etcdserver: requested lease not found"


def b64(data: str | bytes) -> str:
    return base64.b64encode(data.encode() if isinstance(data, str) else data).decode()


def raw_kv(key: str, value: bytes = b"", create_revision: int = 1, lease: int = 0) -> dict:
    """Un kv como lo manda la pasarela: base64, enteros como texto y sin los campos en 0."""
    revision = str(create_revision)
    kv = {"key": b64(key), "create_revision": revision, "mod_revision": revision, "version": "1"}
    if value:
        kv["value"] = b64(value)
    if lease:
        kv["lease"] = str(lease)
    return kv


def make_etcd(handler, endpoints=ENDPOINTS[:1]) -> Etcd:
    return Etcd(endpoints, transport=httpx.MockTransport(handler))


def answer(bodies: dict[str, dict]):
    """Pasarela que responde 200 con el cuerpo que corresponde a cada ruta."""
    return lambda request: httpx.Response(200, json=bodies[request.url.path])


# --- Funciones auxiliares ---


def test_prefix_end():
    assert prefix_end("/ns/ana/") == b"/ns/ana0"
    assert prefix_end(b"a\xff") == b"b"
    assert prefix_end(b"\xff") == b"\0"


def test_lease_ids_are_hex_in_the_api_and_in_the_lock_key():
    lease_id = 7587898652998393091  # como lo entrega /v3/lease/grant (contexto.md, sección 4)
    assert lease_hex(lease_id) == "694da19f5ba25d03"
    assert lease_from_hex("694da19f5ba25d03") == lease_id
    assert lock_key("/locks/ana/docs/x", lease_id) == "/locks/ana/docs/x/694da19f5ba25d03"
    for valid in (1, 0xF, lease_id, 2**63 - 1):  # etcd da IDs positivos de 63 bits
        assert lease_from_hex(lease_hex(valid)) == valid
    # Solo se acepta lo que lease_hex produce: sin mayúsculas, signo, prefijo, ceros a la izquierda ni el 0.
    for text in ("", "no-es-hex", "694DA19F5BA25D03", "-1", "0x1f", "0001f", "0", "1" * 17):
        with pytest.raises(LeaseNotFound):
            lease_from_hex(text)


# --- Base64, enteros como texto y campos omitidos ---


@pytest.mark.anyio
async def test_put_sends_key_and_value_in_base64_and_dicts_as_compact_json():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=HEADER)

    async with make_etcd(handler) as etcd:
        await etcd.put("/ns/ana/docs", {"type": "dir", "created_at": 1793628090000}, lease=LEASE)
        await etcd.put("/blocks/ana/x", b"\x00\xff")

    assert [request.url.path for request in requests] == ["/v3/kv/put", "/v3/kv/put"]
    assert json.loads(requests[0].content) == {
        "key": b64("/ns/ana/docs"),
        "value": b64('{"type":"dir","created_at":1793628090000}'),
        "lease": LEASE,
    }
    assert json.loads(requests[1].content) == {"key": b64("/blocks/ana/x"), "value": b64(b"\x00\xff"), "lease": 0}


@pytest.mark.anyio
async def test_get_decodes_base64_and_integers_sent_as_text():
    kv = raw_kv("/ns/ana/docs", b'{"type":"dir"}', create_revision=12, lease=7587898652998393091)
    kv["mod_revision"], kv["version"] = "15", "2"

    def handler(request):
        assert json.loads(request.content) == {"key": b64("/ns/ana/docs")}
        return httpx.Response(200, json={**HEADER, "kvs": [kv], "count": "1"})

    async with make_etcd(handler) as etcd:
        got = await etcd.get("/ns/ana/docs")

    assert got == KeyValue(
        key="/ns/ana/docs",
        value=b'{"type":"dir"}',
        create_revision=12,
        mod_revision=15,
        version=2,
        lease=7587898652998393091,
    )
    assert got.json() == {"type": "dir"}


@pytest.mark.anyio
async def test_lease_grant_and_keepalive_read_integers_sent_as_text():
    requests = []
    bodies = {
        "/v3/lease/grant": {**HEADER, "ID": "7587898652998393091", "TTL": "30"},
        "/v3/lease/keepalive": {"result": {**HEADER, "ID": "7587898652998393091", "TTL": "30"}},
    }

    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=bodies[request.url.path])

    async with make_etcd(handler) as etcd:
        lease_id = await etcd.lease_grant(30)
        assert lease_id == 7587898652998393091
        assert await etcd.lease_keepalive(lease_id) == 30

    assert requests == [{"TTL": 30}, {"ID": 7587898652998393091}]


@pytest.mark.anyio
async def test_missing_fields_mean_zero_empty_or_false():
    bodies = {"/v3/kv/range": HEADER, "/v3/kv/deleterange": HEADER, "/v3/kv/txn": HEADER}
    async with make_etcd(answer(bodies)) as etcd:
        assert await etcd.get("/nada") is None
        assert await etcd.get_prefix("/nada/") == []
        assert await etcd.delete("/nada") == 0
        assert await etcd.txn([], [], []) == TxnResult(succeeded=False, responses=[])

    # Leída con keys_only y sin lease: no llegan value ni lease.
    async with make_etcd(answer({"/v3/kv/range": {**HEADER, "kvs": [raw_kv("/a/1")]}})) as etcd:
        [kv] = await etcd.get_prefix("/a/", keys_only=True)
    assert (kv.value, kv.lease) == (b"", 0)


@pytest.mark.anyio
async def test_get_prefix_sends_the_range_keys_only_and_limit():
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json=HEADER)

    async with make_etcd(handler) as etcd:
        await etcd.get_prefix("/ns/ana/", keys_only=True, limit=10)

    assert seen == [{"key": b64("/ns/ana/"), "range_end": b64("/ns/ana0"), "keys_only": True, "limit": 10}]


@pytest.mark.anyio
async def test_txn_sends_compares_and_ops_and_reads_each_response():
    directory, children = "/ns/ana/docs", "/ns/ana/docs/"
    seen = {}

    def handler(request):
        seen.update(json.loads(request.content))
        responses = [
            {"response_put": HEADER},
            {"response_range": {**HEADER, "kvs": [raw_kv(directory, b"{}", create_revision=7)], "count": "1"}},
            {"response_delete_range": {**HEADER, "deleted": "2"}},
        ]
        return httpx.Response(200, json={**HEADER, "succeeded": True, "responses": responses})

    async with make_etcd(handler) as etcd:
        result = await etcd.txn(
            [
                compare(children, "CREATE", "EQUAL", 0, range_end=prefix_end(children)),
                compare(directory, "MOD", "EQUAL", 15),
                compare(directory, "VALUE", "EQUAL", {"type": "dir"}),
            ],
            [
                op_put(children + "a", {"type": "dir"}, lease=LEASE),
                op_range(directory),
                op_delete(children, range_end=prefix_end(children)),
            ],
            [op_range(directory)],
        )

    assert seen["compare"] == [
        {
            "key": b64(children),
            "range_end": b64("/ns/ana/docs0"),
            "target": "CREATE",
            "result": "EQUAL",
            "create_revision": 0,
        },
        {"key": b64(directory), "target": "MOD", "result": "EQUAL", "mod_revision": 15},
        {"key": b64(directory), "target": "VALUE", "result": "EQUAL", "value": b64('{"type":"dir"}')},
    ]
    assert seen["success"] == [
        {"request_put": {"key": b64(children + "a"), "value": b64('{"type":"dir"}'), "lease": LEASE}},
        {"request_range": {"key": b64(directory)}},
        {"request_delete_range": {"key": b64(children), "range_end": b64("/ns/ana/docs0")}},
    ]
    assert seen["failure"] == [{"request_range": {"key": b64(directory)}}]
    assert result.succeeded
    assert result.responses == [None, [KeyValue(directory, b"{}", 7, 7, 1, 0)], 2]


# --- Varios endpoints ---


@pytest.mark.anyio
async def test_connection_error_moves_to_the_next_endpoint_and_remembers_it():
    calls = []

    def handler(request):
        calls.append(request.url.host)
        if request.url.host == "etcd1":
            raise httpx.ConnectError("conexión rechazada", request=request)
        return httpx.Response(200, json=HEADER)

    async with make_etcd(handler, ENDPOINTS) as etcd:
        assert await etcd.get("/a") is None
        assert await etcd.get("/a") is None

    assert calls == ["etcd1", "etcd2", "etcd2"]


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("status", "body"),
    [
        (503, {"error": "etcdserver: no leader", "code": 14, "message": "etcdserver: no leader"}),
        (504, {"error": "context deadline exceeded", "code": 4, "message": "context deadline exceeded"}),
        # Así llega un error de etcd en un endpoint de streaming, como keepalive (grpc-gateway v1.16).
        (503, {"error": {"grpc_code": 14, "http_code": 503, "message": "etcdserver: no leader"}}),
    ],
)
async def test_grpc_unavailable_or_deadline_exceeded_moves_to_the_next_endpoint(status, body):
    def handler(request):
        if request.url.host == "etcd1":
            return httpx.Response(status, json=body)
        return httpx.Response(200, json={"result": {**HEADER, "ID": str(LEASE), "TTL": "30"}})

    async with make_etcd(handler, ENDPOINTS) as etcd:
        assert await etcd.lease_keepalive(LEASE) == 30


@pytest.mark.anyio
async def test_all_endpoints_down_is_e_unavailable():
    def handler(request):
        raise httpx.ConnectError("conexión rechazada", request=request)

    async with make_etcd(handler, ENDPOINTS) as etcd:
        with pytest.raises(DFSError) as caught:
            await etcd.get("/a")

    assert caught.value.code == ErrorCode.E_UNAVAILABLE


@pytest.mark.anyio
async def test_a_txn_that_was_not_sent_goes_to_the_next_endpoint():
    def handler(request):
        if request.url.host == "etcd1":
            raise httpx.ConnectError("conexión rechazada", request=request)
        return httpx.Response(200, json={**HEADER, "succeeded": True})

    async with make_etcd(handler, ENDPOINTS) as etcd:
        assert (await etcd.txn([], [op_put("/a", b"")], [])).succeeded


@pytest.mark.anyio
@pytest.mark.parametrize("lost", [httpx.ReadTimeout, httpx.RemoteProtocolError], ids=["sin-respuesta", "cortada"])
async def test_a_txn_that_got_no_response_is_not_resent(lost):
    """etcd pudo haberla aplicado: repetida en otro nodo tomaría la otra rama.

    Responde 503, y la siguiente llamada empieza por otro nodo.
    """
    calls = []

    def handler(request):
        calls.append(request.url.host)
        if request.url.host == "etcd1":
            raise lost("etcd1 recibió la petición y no respondió", request=request)
        return httpx.Response(200, json={**HEADER, "succeeded": True})

    async with make_etcd(handler, ENDPOINTS) as etcd:
        with pytest.raises(DFSError) as caught:
            await etcd.txn([compare("/a", "CREATE", "EQUAL", 0)], [op_put("/a", b"")], [])
        assert caught.value.code == ErrorCode.E_UNAVAILABLE
        assert (await etcd.txn([], [], [])).succeeded

    assert calls == ["etcd1", "etcd2"]


def grpc_error(code: int, message: str) -> httpx.Response:
    return httpx.Response(503 if code == 14 else 504, json={"error": message, "code": code, "message": message})


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("code", "message"),
    [(14, "etcdserver: request timed out"), (14, "etcdserver: leader changed"), (4, "context deadline exceeded")],
)
async def test_a_txn_whose_outcome_etcd_does_not_know_is_not_resent(code, message):
    """etcd dice que no terminó: pudo haberla aplicado. 503, y la siguiente llamada empieza por otro nodo."""
    calls = []

    def handler(request):
        calls.append(request.url.host)
        if request.url.host == "etcd1":
            return grpc_error(code, message)
        return httpx.Response(200, json={**HEADER, "succeeded": True})

    async with make_etcd(handler, ENDPOINTS) as etcd:
        with pytest.raises(DFSError) as caught:
            await etcd.txn([compare("/a", "CREATE", "EQUAL", 0)], [op_put("/a", b"")], [])
        assert caught.value.code == ErrorCode.E_UNAVAILABLE
        assert (await etcd.txn([], [], [])).succeeded

    assert calls == ["etcd1", "etcd2"]


@pytest.mark.anyio
async def test_a_txn_rejected_for_lack_of_leader_is_resent():
    # Un nodo sin líder rechaza la petición antes de proponerla: no la aplicó.
    def handler(request):
        if request.url.host == "etcd1":
            return grpc_error(14, "etcdserver: no leader")
        return httpx.Response(200, json={**HEADER, "succeeded": True})

    async with make_etcd(handler, ENDPOINTS) as etcd:
        assert (await etcd.txn([], [op_put("/a", b"")], [])).succeeded


@pytest.mark.anyio
async def test_lock_with_wait_returns_none_if_etcd_does_not_finish_it():
    async with make_etcd(lambda request: grpc_error(14, "etcdserver: request timed out"), ENDPOINTS) as etcd:
        assert await etcd.lock(NAME, LEASE, wait_s=5) is None


@pytest.mark.anyio
@pytest.mark.parametrize("lost", [httpx.ReadTimeout, httpx.RemoteProtocolError], ids=["sin-respuesta", "cortada"])
async def test_calls_that_can_be_repeated_are_resent_when_the_response_is_lost(lost):
    calls = []

    def handler(request):
        calls.append(request.url.host)
        if request.url.host == "etcd1":
            raise lost("etcd1 recibió la petición y no respondió", request=request)
        return httpx.Response(200, json=HEADER)

    async with make_etcd(handler, ENDPOINTS) as etcd:
        await etcd.put("/a", b"1")  # repetir el mismo put deja el mismo valor

    assert calls == ["etcd1", "etcd2"]


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("status", "content"),
    [(502, b"<html>Bad Gateway</html>"), (502, b'{"message": "bad gateway"}'), (200, b"no es JSON")],
    ids=["html", "json-sin-error", "200-sin-json"],
)
async def test_a_response_that_is_not_from_the_gateway_is_never_taken_as_valid(status, content):
    """Sin esto, un cuerpo sin kvs ni succeeded se leería como "no existe" o "la transacción falló"."""
    async with make_etcd(lambda request: httpx.Response(status, content=content)) as etcd:
        with pytest.raises(EtcdError, match=f"HTTP {status}"):
            await etcd.get("/a")
        with pytest.raises(EtcdError, match=f"HTTP {status}"):
            await etcd.txn([], [], [])


@pytest.mark.anyio
async def test_other_etcd_errors_are_raised_without_trying_another_endpoint():
    calls = []
    message = "etcdserver: duplicate key given in txn request"

    def handler(request):
        calls.append(request.url.host)
        return httpx.Response(400, json={"error": message, "code": 3, "message": message})

    async with make_etcd(handler, ENDPOINTS) as etcd:
        with pytest.raises(EtcdError, match="duplicate key"):
            await etcd.txn([], [op_put("/a", b""), op_delete("/a")], [])

    assert calls == ["etcd1"]


# --- Lease inexistente ---


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("call", "path", "status", "body"),
    [
        # keepalive no da error: responde sin TTL.
        (lambda etcd: etcd.lease_keepalive(LEASE), "/v3/lease/keepalive", 200, {"result": {**HEADER, "ID": "1"}}),
        # En /v3/kv y /v3/lease llega con code 5; en /v3/lock y /v3/election, con code 2.
        (lambda etcd: etcd.lease_revoke(LEASE), "/v3/lease/revoke", 404, {"code": 5}),
        (lambda etcd: etcd.put("/a", b"", lease=LEASE), "/v3/kv/put", 404, {"code": 5}),
        (lambda etcd: etcd.lock(NAME, LEASE, wait_s=5), "/v3/lock/lock", 500, {"code": 2}),
        (lambda etcd: etcd.campaign("/election/leader", LEASE, b"m"), "/v3/election/campaign", 500, {"code": 2}),
    ],
)
async def test_missing_lease_raises_lease_not_found(call, path, status, body):
    if "code" in body:
        body = {"error": LEASE_MSG, "message": LEASE_MSG, **body}

    def handler(request):
        assert request.url.path == path
        return httpx.Response(status, json=body)

    async with make_etcd(handler) as etcd:
        with pytest.raises(LeaseNotFound):
            await call(etcd)


# --- Lock ---


def lock_gateway(holders: list[dict], requests: list):
    """Pasarela para el intento sin espera: anota cada petición y responde el range con holders."""

    def handler(request):
        requests.append((request.url.path, json.loads(request.content)))
        if request.url.path == "/v3/kv/range":
            return httpx.Response(200, json={**HEADER, "kvs": holders})
        return httpx.Response(200, json=HEADER)

    return handler


@pytest.mark.anyio
async def test_lock_without_wait_when_free():
    requests = []
    holders = [raw_kv(MINE, create_revision=10, lease=LEASE)]

    async with make_etcd(lock_gateway(holders, requests)) as etcd:
        assert await etcd.lock(NAME, LEASE, wait_s=0) == MINE

    assert requests == [
        ("/v3/kv/put", {"key": b64(MINE), "value": "", "lease": LEASE}),
        ("/v3/kv/range", {"key": b64(NAME + "/"), "range_end": b64(NAME + "0"), "keys_only": True, "limit": 0}),
    ]


@pytest.mark.anyio
async def test_lock_without_wait_when_taken_deletes_its_key():
    # La otra clave va después en orden alfabético pero se creó antes: manda create_revision.
    other_lease = 0x7A00000000000001
    other = lock_key(NAME, other_lease)
    requests = []
    holders = [raw_kv(MINE, create_revision=10, lease=LEASE), raw_kv(other, create_revision=5, lease=other_lease)]

    async with make_etcd(lock_gateway(holders, requests)) as etcd:
        assert await etcd.lock(NAME, LEASE, wait_s=0) is None

    assert [path for path, _ in requests] == ["/v3/kv/put", "/v3/kv/range", "/v3/kv/deleterange"]
    assert requests[2][1] == {"key": b64(MINE)}


@pytest.mark.anyio
async def test_lock_with_wait_uses_the_lock_service_with_wait_s_as_timeout():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={**HEADER, "key": b64(MINE)})

    async with make_etcd(handler) as etcd:
        assert await etcd.lock(NAME, LEASE, wait_s=7) == MINE

    assert seen[0].url.path == "/v3/lock/lock"
    assert json.loads(seen[0].content) == {"name": b64(NAME), "lease": LEASE}
    assert seen[0].extensions["timeout"]["read"] == 7


@pytest.mark.anyio
@pytest.mark.parametrize("lost", [httpx.ReadTimeout, httpx.RemoteProtocolError], ids=["se-agoto", "cortada"])
async def test_lock_with_wait_returns_none_when_the_wait_runs_out_or_is_cut(lost):
    calls = []

    def handler(request):
        calls.append(request.url.host)
        raise lost("se agotó o se cortó la espera", request=request)

    async with make_etcd(handler, ENDPOINTS) as etcd:
        assert await etcd.lock(NAME, LEASE, wait_s=1) is None

    assert calls == ["etcd1"]  # agotar la espera no es una caída: no prueba otro nodo


@pytest.mark.anyio
async def test_unlock_deletes_the_lock_key():
    seen = []

    def handler(request):
        seen.append((request.url.path, json.loads(request.content)))
        return httpx.Response(200, json=HEADER)

    async with make_etcd(handler) as etcd:
        await etcd.unlock(MINE)

    assert seen == [("/v3/lock/unlock", {"key": b64(MINE)})]


# --- Elección ---


@pytest.mark.anyio
async def test_campaign_waits_without_timeout_and_resign_sends_back_the_leader_key():
    name = "/election/leader"
    leader = {"name": b64(name), "key": b64(f"{name}/{lease_hex(LEASE)}"), "rev": "3", "lease": str(LEASE)}
    seen = []

    def handler(request):
        seen.append(request)
        if request.url.path == "/v3/election/campaign":
            return httpx.Response(200, json={**HEADER, "leader": leader})
        return httpx.Response(200, json=HEADER)

    async with make_etcd(handler) as etcd:
        won = await etcd.campaign(name, LEASE, {"meta_id": "meta1"})
        await etcd.resign(won)

    assert won == LeaderKey(name=name, key=f"{name}/694d8a1c2e3b4f05", rev=3, lease=LEASE)
    campaign, resign = seen
    assert json.loads(campaign.content) == {"name": b64(name), "lease": LEASE, "value": b64('{"meta_id":"meta1"}')}
    assert campaign.extensions["timeout"]["read"] is None
    assert resign.url.path == "/v3/election/resign"
    assert json.loads(resign.content) == {"leader": {**leader, "rev": 3, "lease": LEASE}}


@pytest.mark.anyio
async def test_leader_returns_the_leader_key_or_none():
    name = "/election/leader"
    no_leader = {"error": "election: no leader", "code": 2, "message": "election: no leader"}
    kv = raw_kv(f"{name}/{lease_hex(LEASE)}", b'{"meta_id":"meta1"}', create_revision=3, lease=LEASE)
    answers = [httpx.Response(200, json={**HEADER, "kv": kv}), httpx.Response(500, json=no_leader)]

    async with make_etcd(lambda request: answers.pop(0)) as etcd:
        leader = await etcd.leader(name)
        assert await etcd.leader(name) is None

    assert leader.json() == {"meta_id": "meta1"}
    assert leader.lease == LEASE
