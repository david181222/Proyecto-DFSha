"""Pruebas de la tarea 15: el DataNode se registra, late y cumple órdenes (DA-11, DA-12).

El MetaServer y los otros DataNodes se simulan con httpx.MockTransport; el almacén es uno de
verdad, en una carpeta temporal. Las pruebas llaman a beat_once y run_order una vez cada una,
sin esperar los segundos del latido.
"""

import asyncio
import hashlib
import json
import os
import time

import httpx
import pytest

from datanode import __main__ as datanode_main
from datanode.agent import NodeAgent, load_disk_id, parse_order
from datanode.meta_client import MetaClient
from datanode.storage import BlockStore
from dfs_common.config import Settings

pytestmark = pytest.mark.anyio

MIB = 1024 * 1024
NODE_KEY = bytes(range(32))
BLOCK = b"bloque de ana " * 1000
HASH = hashlib.sha256(BLOCK).hexdigest()
MISSING = "0" * 64
REGISTER = "/api/v1/internal/register"
HEARTBEAT = "/api/v1/internal/heartbeat"
TARGET = {"node_id": "dn1", "address": "10.88.0.31:9444"}


def copy_order(order_id: str = "copia-1", hash: str = HASH) -> dict:
    return {"order_id": order_id, "type": "copy", "user": "ana", "hash": hash, "target": TARGET}


def delete_order(order_id: str, written_before_ms: int) -> dict:
    return {"order_id": order_id, "type": "delete", "user": "ana", "hash": HASH, "written_before_ms": written_before_ms}


async def chunks(data: bytes):
    yield data


class MetaServer:
    """Un MetaServer simulado. heartbeats: respuestas de los latidos, en orden (por defecto, sin órdenes).

    down: no acepta conexiones. on_heartbeat: se llama mientras el latido "va en camino".
    Anota el cuerpo de cada registro y latido en calls.
    """

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.heartbeats: list[httpx.Response] = []
        self.down = False
        self.on_heartbeat = None

    def handler(self, request: httpx.Request) -> httpx.Response:
        if self.down:
            raise httpx.ConnectError("conexión rechazada", request=request)
        if request.url.path == "/api/v1/cluster/metaservers":
            meta = {"meta_id": "meta1", "ip": "10.88.0.21", "public_port": 8443, "internal_port": 8444}
            return httpx.Response(200, json={"metaservers": [meta]})
        self.calls.append((request.url.path, json.loads(request.content)))
        if request.url.path == REGISTER:
            return httpx.Response(200, json={"lease_id": f"lease-{len(self.registers())}", "heartbeat_s": 7})
        if self.on_heartbeat:
            self.on_heartbeat()
        if self.heartbeats:
            return self.heartbeats.pop(0)
        return orders()

    def registers(self) -> list[dict]:
        return [body for path, body in self.calls if path == REGISTER]

    def beats(self) -> list[dict]:
        return [body for path, body in self.calls if path == HEARTBEAT]


def orders(*listed: dict) -> httpx.Response:
    return httpx.Response(200, json={"orders": list(listed)})


def lease_expired() -> httpx.Response:
    return httpx.Response(404, json={"error": "E_NOT_FOUND", "message": "regístrate de nuevo", "retryable": False})


class Peers:
    """Los otros DataNodes: guardan lo que reciben y responden status (201 por defecto)."""

    def __init__(self):
        self.received: list[tuple[str, str, bytes]] = []
        self.status = 201

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.received.append((f"{request.url.host}:{request.url.port}", request.url.path, request.content))
        if self.status == 201:
            return httpx.Response(201, json={"hash": HASH, "node_id": TARGET["node_id"]})
        return httpx.Response(self.status, json={"error": "E_INTERNAL", "message": "falló", "retryable": False})


@pytest.fixture
async def node(tmp_path):
    """(agente, MetaServer, otros DataNodes, almacén) de dn2, con el bloque de ana guardado."""
    settings = Settings(
        _env_file=None, node_id="dn2", node_ip="10.88.0.32", zone="us-east-1b", meta_seeds=["10.88.0.21"]
    )
    store = BlockStore(tmp_path / "data", NODE_KEY, 8 * MIB)
    await store.write("ana", HASH, chunks(BLOCK))
    meta_server, peers = MetaServer(), Peers()
    meta = MetaClient(settings, None, transport=httpx.MockTransport(meta_server.handler))
    peer_client = httpx.AsyncClient(transport=httpx.MockTransport(peers.handler))
    agent = NodeAgent(settings, store, meta, peer_client, disk_id="disco-dn2")
    yield agent, meta_server, peers, store
    await meta.aclose()
    await peer_client.aclose()


# --- Registro y latido ---


async def test_it_registers_at_start_and_uses_the_heartbeat_s_it_receives(node):
    agent, meta, _, store = node
    assert await agent.beat_once() == 7
    [body] = meta.registers()
    assert body == {
        "node_id": "dn2",
        "disk_id": "disco-dn2",
        "ip": "10.88.0.32",
        "public_port": 9443,
        "internal_port": 9444,
        "zone": "us-east-1b",
        "free_bytes": body["free_bytes"],
    }
    assert body["free_bytes"] > 0
    # Ya registrado, la siguiente vuelta late con el lease recibido.
    assert await agent.beat_once() == 7
    [beat] = meta.beats()
    assert beat == {"node_id": "dn2", "lease_id": "lease-1", "free_bytes": beat["free_bytes"], "done_orders": [], "bad_blocks": []}


async def test_without_metaserver_it_waits_dfs_heartbeat_s_and_tries_again(node):
    agent, meta, _, _ = node
    meta.down = True
    assert await agent.beat_once() == 5  # DFS_HEARTBEAT_S por defecto: todavía no hay registro
    meta.down = False
    assert await agent.beat_once() == 7
    assert len(meta.registers()) == 1


async def test_a_404_in_the_heartbeat_registers_again(node):
    agent, meta, _, _ = node
    await agent.beat_once()
    meta.heartbeats = [lease_expired()]
    await agent.beat_once()
    assert [path for path, _ in meta.calls] == [REGISTER, HEARTBEAT, REGISTER]
    await agent.beat_once()
    assert meta.beats()[-1]["lease_id"] == "lease-2"


async def test_done_orders_and_bad_blocks_survive_a_failed_heartbeat(node):
    agent, meta, _, store = node
    await agent.beat_once()
    agent.done_orders.append("orden-1")
    with pytest.raises(Exception):
        store.read("ana", MISSING)  # el almacén lo anota como bloque que falta
    meta.down = True
    await agent.beat_once()  # falla
    meta.down = False
    await agent.beat_once()
    assert meta.beats()[-1]["done_orders"] == ["orden-1"]
    assert meta.beats()[-1]["bad_blocks"] == [{"user": "ana", "hash": MISSING}]
    # El latido respondió 200: ya no se vuelven a enviar.
    await agent.beat_once()
    assert (meta.beats()[-1]["done_orders"], meta.beats()[-1]["bad_blocks"]) == ([], [])


async def test_a_heartbeat_answering_an_error_also_keeps_them(node):
    agent, meta, _, _ = node
    await agent.beat_once()
    agent.done_orders.append("orden-1")
    meta.heartbeats = [httpx.Response(500, json={"error": "E_INTERNAL", "message": "x", "retryable": False})]
    await agent.beat_once()
    await agent.beat_once()
    assert [beat["done_orders"] for beat in meta.beats()] == [["orden-1"], ["orden-1"]]


async def test_what_arrives_during_a_heartbeat_is_not_lost(node):
    """Solo se vacía lo que se envió: lo que se cumplió mientras el latido iba en camino se queda."""
    agent, meta, _, _ = node
    await agent.beat_once()
    agent.done_orders.append("orden-1")
    meta.on_heartbeat = lambda: agent.done_orders.append("orden-2")  # se cumple mientras el latido va en camino
    await agent.beat_once()
    assert agent.done_orders == ["orden-2"]


# --- Órdenes ---


async def test_orders_from_the_heartbeat_go_to_the_queue(node):
    agent, meta, _, _ = node
    await agent.beat_once()
    meta.heartbeats = [orders(copy_order(), delete_order("borrar-1", 0))]
    await agent.beat_once()
    assert [agent.queue.get_nowait().order_id for _ in range(agent.queue.qsize())] == ["copia-1", "borrar-1"]


async def test_copy_sends_the_block_to_the_internal_api_of_the_target(node):
    agent, _, peers, _ = node
    await agent.run_order(parse_order(copy_order()))
    assert peers.received == [("10.88.0.31:9444", f"/api/v1/internal/blocks/ana/{HASH}", BLOCK)]
    assert agent.done_orders == ["copia-1"]


async def test_a_failed_copy_is_not_reported_and_runs_again_when_the_order_comes_back(node):
    agent, meta, peers, _ = node
    await agent.beat_once()
    peers.status = 500
    meta.heartbeats = [orders(copy_order())]
    await agent.beat_once()
    await agent.run_order(agent.queue.get_nowait())
    assert agent.done_orders == []
    # El MetaServer la reenvía en el siguiente latido, y esta vez el destino responde 201.
    peers.status = 201
    meta.heartbeats = [orders(copy_order())]
    await agent.beat_once()
    await agent.run_order(agent.queue.get_nowait())
    assert agent.done_orders == ["copia-1"]
    assert len(peers.received) == 2


async def test_copy_of_a_block_this_node_does_not_have_is_not_reported_and_goes_to_bad_blocks(node):
    agent, meta, peers, _ = node
    await agent.beat_once()
    await agent.run_order(parse_order(copy_order("copia-falta", MISSING)))
    assert agent.done_orders == []
    assert peers.received == []
    await agent.beat_once()
    assert meta.beats()[-1]["bad_blocks"] == [{"user": "ana", "hash": MISSING}]


async def test_delete_of_an_older_copy_deletes_it_and_is_reported(node):
    agent, _, _, store = node
    await agent.run_order(parse_order(delete_order("borrar-1", int(time.time() * 1000) + 60_000)))
    assert agent.done_orders == ["borrar-1"]
    assert not (store.data_dir / "ana" / HASH).exists()


async def test_delete_of_a_newer_copy_keeps_it_and_is_also_reported(node):
    """La copia se escribió después de T: conservarla es justamente la regla (DA-02, Figura 1)."""
    agent, _, _, store = node
    await agent.run_order(parse_order(delete_order("borrar-1", 1)))
    assert agent.done_orders == ["borrar-1"]
    assert store.read("ana", HASH) == BLOCK


async def test_the_same_order_received_twice_runs_once(node):
    agent, meta, peers, _ = node
    await agent.beat_once()
    meta.heartbeats = [orders(copy_order()), orders(copy_order())]
    await agent.beat_once()
    await agent.beat_once()  # llega otra vez antes de que el trabajador la cumpla
    assert agent.queue.qsize() == 1
    order = agent.queue.get_nowait()
    # Se cumple mientras el siguiente latido va en camino: ese latido no la reporta y el
    # MetaServer la vuelve a enviar. Cumplida pero sin reportar: tampoco se repite.
    meta.on_heartbeat = lambda: asyncio.get_running_loop().create_task(agent.run_order(order))
    meta.heartbeats = [orders(copy_order())]
    await agent.beat_once()
    meta.on_heartbeat = None
    await asyncio.sleep(0.05)  # deja terminar la copia
    assert agent.done_orders == ["copia-1"]
    assert agent.queue.qsize() == 0
    assert len(peers.received) == 1
    # Un latido que la reporta y aun así la recibe (no debería pasar) tampoco la repite.
    meta.heartbeats = [orders(copy_order())]
    await agent.beat_once()
    assert agent.queue.qsize() == 0


async def test_once_reported_the_order_is_forgotten(node):
    agent, meta, _, _ = node
    await agent.beat_once()
    await agent.run_order(parse_order(copy_order()))
    await agent.beat_once()  # la reporta y el MetaServer responde 200
    assert meta.beats()[-1]["done_orders"] == ["copia-1"]
    assert "copia-1" not in agent.seen


async def test_an_unknown_order_type_is_logged_and_ignored(node, caplog):
    agent, meta, _, _ = node
    await agent.beat_once()
    rare = {"order_id": "rara-1", "type": "compress", "user": "ana", "hash": HASH}
    meta.heartbeats = [orders(rare, copy_order())]
    await agent.beat_once()
    assert agent.queue.qsize() == 1
    assert agent.queue.get_nowait().order_id == "copia-1"
    assert any("rara-1" in record.getMessage() for record in caplog.records)


async def test_the_worker_runs_the_queue_and_survives_an_unexpected_error(node, caplog):
    agent, _, peers, _ = node
    calls = []
    original = agent.run_order

    async def run_order(order):
        calls.append(order.order_id)
        if order.order_id == "falla":
            raise RuntimeError("error inesperado")
        await original(order)

    agent.run_order = run_order
    agent.seen.update({"falla", "copia-1"})
    agent.queue.put_nowait(parse_order(copy_order("falla")))
    agent.queue.put_nowait(parse_order(copy_order()))
    worker = asyncio.create_task(agent.order_worker())
    for _ in range(100):
        if agent.done_orders:
            break
        await asyncio.sleep(0.01)
    worker.cancel()
    assert calls == ["falla", "copia-1"]
    assert agent.done_orders == ["copia-1"]
    assert "falla" not in agent.seen  # se reintenta si la orden vuelve


# --- Arranque: disk_id y DFS_NODE_IP ---


async def test_disk_id_is_created_once_in_an_empty_folder(tmp_path):
    disk_id = load_disk_id(tmp_path)
    assert disk_id
    assert (tmp_path / ".disk_id").read_text(encoding="utf-8") == disk_id
    assert load_disk_id(tmp_path) == disk_id  # al volver a arrancar, el mismo


async def test_a_folder_with_blocks_but_without_disk_id_does_not_start(tmp_path):
    (tmp_path / "ana").mkdir()
    (tmp_path / "ana" / HASH).write_bytes(b"x")
    with pytest.raises(RuntimeError, match=".disk_id"):
        load_disk_id(tmp_path)
    assert not (tmp_path / ".disk_id").exists()


async def test_empty_user_folders_do_not_count_as_blocks(tmp_path):
    (tmp_path / "ana").mkdir()
    assert load_disk_id(tmp_path)


async def test_the_agent_needs_dfs_node_ip(tmp_path):
    settings = Settings(_env_file=None, node_id="dn2", zone="us-east-1b", meta_seeds=["10.88.0.21"])
    with pytest.raises(ValueError, match="DFS_NODE_IP"):
        NodeAgent(settings, None, None, None, disk_id="d")


async def test_python_m_datanode_does_not_start_without_dfs_node_ip(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)  # sin un .env que aporte valores
    for name in list(os.environ):
        if name.startswith("DFS_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("DFS_NODE_ID", "dn2")
    monkeypatch.setenv("DFS_ZONE", "us-east-1b")
    monkeypatch.setenv("DFS_META_SEEDS", "10.88.0.21")
    with pytest.raises(SystemExit, match="DFS_NODE_IP"):
        datanode_main.main()
