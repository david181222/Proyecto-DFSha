"""Pruebas de la tarea 13: autorregistro del MetaServer y GET /api/v1/cluster/metaservers (DA-11).

La renovación se prueba llamando directo a keep_registered con un intervalo de 0,05 s
(DFS_HEARTBEAT_S no baja de 1 s); el arranque, corriendo el lifespan de verdad con FakeEtcd.
"""

import asyncio
import os

import pytest
from fastapi.testclient import TestClient

from dfs_common.config import Settings
from dfs_common.errors import DFSError, ErrorCode
from dfs_common.models import MetaServerListResponse
from metaserver import deps, metaservers
from metaserver.public_api import app as metaserver_public

from fakes import JWT_SECRET, PASS_KEY, FakeEtcd, public_client

EVERY_S = 0.05


def settings(**changes) -> Settings:
    return Settings(_env_file=None, node_id="meta1", node_ip="172.31.0.10", **changes)


@pytest.mark.anyio
async def test_register_self_writes_its_key_tied_to_a_lease_of_dfs_node_lease_s():
    etcd = FakeEtcd()
    lease_id = await metaservers.register_self(etcd, settings(node_lease_s=17))
    assert etcd.kvs["/nodes/meta/meta1"].lease == lease_id
    assert etcd.ttls[lease_id] == 17
    assert etcd.value("/nodes/meta/meta1") == {"ip": "172.31.0.10", "public_port": 8443, "internal_port": 8444}


@pytest.mark.anyio
async def test_keep_registered_renews_the_lease_every_round():
    etcd = FakeEtcd()
    config = settings()
    lease_id = await metaservers.register_self(etcd, config)
    task = asyncio.create_task(metaservers.keep_registered(etcd, config, lease_id, EVERY_S))
    await asyncio.sleep(0.3)
    task.cancel()
    assert etcd.keepalives.count(lease_id) >= 3


@pytest.mark.anyio
async def test_keep_registered_registers_again_when_the_lease_is_lost():
    etcd = FakeEtcd()
    config = settings()
    lease_id = await metaservers.register_self(etcd, config)
    task = asyncio.create_task(metaservers.keep_registered(etcd, config, lease_id, EVERY_S))
    await etcd.lease_revoke(lease_id)  # venció: su clave desapareció con él
    await asyncio.sleep(0.3)
    task.cancel()
    kv = etcd.kvs["/nodes/meta/meta1"]
    assert kv.lease != lease_id and kv.lease in etcd.leases
    assert etcd.keepalives.count(kv.lease) >= 2  # y sigue renovando el lease nuevo


class KeepaliveFailsOnce(FakeEtcd):
    async def lease_keepalive(self, lease_id):
        if not self.keepalives:
            self.keepalives.append(lease_id)
            raise DFSError(ErrorCode.E_UNAVAILABLE, "ningún nodo de etcd respondió")
        return await super().lease_keepalive(lease_id)


@pytest.mark.anyio
async def test_keep_registered_logs_and_tries_again_if_etcd_does_not_answer(caplog):
    etcd = KeepaliveFailsOnce()
    config = settings()
    lease_id = await metaservers.register_self(etcd, config)
    task = asyncio.create_task(metaservers.keep_registered(etcd, config, lease_id, EVERY_S))
    await asyncio.sleep(0.3)
    task.cancel()
    assert etcd.keepalives.count(lease_id) >= 3
    assert "se intenta en la siguiente vuelta" in caplog.text


def test_list_metaservers_is_sorted_by_meta_id_and_needs_no_authentication():
    etcd = FakeEtcd()
    for meta_id, ip in [("meta3", "172.31.32.10"), ("meta1", "172.31.0.10"), ("meta2", "172.31.16.10")]:
        etcd.seed(f"/nodes/meta/{meta_id}", {"ip": ip, "public_port": 8443, "internal_port": 8444})
    with public_client(etcd) as client:
        response = client.get("/api/v1/cluster/metaservers")
    assert response.json() == MetaServerListResponse.model_config["json_schema_extra"]["examples"][0]


# --- Arranque (lifespan) ---


@pytest.fixture
def startup(monkeypatch, tmp_path):
    """Lo que necesita el lifespan de la API pública, con un FakeEtcd en lugar de etcd."""
    for key in list(os.environ):
        if key.upper().startswith("DFS_"):
            monkeypatch.delenv(key)
    monkeypatch.chdir(tmp_path)  # sin .env: solo cuentan las variables de abajo
    (tmp_path / "jwt_secret").write_bytes(JWT_SECRET)
    (tmp_path / "pass_key").write_bytes(PASS_KEY)
    monkeypatch.setenv("DFS_JWT_SECRET_FILE", str(tmp_path / "jwt_secret"))
    monkeypatch.setenv("DFS_PASS_KEY_FILE", str(tmp_path / "pass_key"))
    monkeypatch.setenv("DFS_NODE_ID", "meta2")
    monkeypatch.setenv("DFS_NODE_IP", "172.31.16.10")
    etcd = FakeEtcd()
    monkeypatch.setattr(deps, "connect_etcd", lambda settings: etcd)
    return etcd


def test_the_metaserver_registers_itself_when_it_starts(startup):
    with TestClient(metaserver_public) as client:
        listed = client.get("/api/v1/cluster/metaservers").json()
    assert listed == {"metaservers": [{"meta_id": "meta2", "ip": "172.31.16.10", "public_port": 8443, "internal_port": 8444}]}
    assert startup.closed  # al apagar, la renovación se detuvo y se cerró la conexión


def test_without_dfs_node_ip_the_metaserver_does_not_start(startup, monkeypatch):
    monkeypatch.delenv("DFS_NODE_IP")
    with pytest.raises(ValueError, match="DFS_NODE_IP"):
        with TestClient(metaserver_public):
            pass
    assert startup.keys("/nodes/") == []


class EtcdDown(FakeEtcd):
    async def lease_grant(self, ttl_s):
        raise DFSError(ErrorCode.E_UNAVAILABLE, "ningún nodo de etcd respondió")


def test_if_etcd_does_not_answer_at_startup_the_metaserver_does_not_start(startup, monkeypatch):
    """El proceso termina y Docker lo reinicia, como en la tarea 07."""
    monkeypatch.setattr(deps, "connect_etcd", lambda settings: EtcdDown())
    with pytest.raises(DFSError, match="ningún nodo de etcd respondió"):
        with TestClient(metaserver_public):
            pass
