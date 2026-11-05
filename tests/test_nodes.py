"""Pruebas de la tarea 13: POST /internal/register y POST /internal/heartbeat (docs/contratos.md, sección 3)."""

import pytest

from metaserver.etcd import lease_from_hex, lease_hex
from metaserver.nodes import same_ip

from fakes import DA13, FakeEtcd, add_block, internal_client

REGISTER = "/api/v1/internal/register"
HEARTBEAT = "/api/v1/internal/heartbeat"
DISK = "5f0c7e2a-9d41-4b8e-a3c6-1e2f3a4b5c6d"
H0 = "6ce5a55b446b43e4e935c44695a9a09f161e8e87d1ff2359adebc5f9ee5ee5cf"
H1 = "b64bb9046f61749097db060781bcbbba317c08ff0ac378e9aed9a6d18217e449"
H2 = "8de70b36bbfaaf4bd8fecc81370e32e388e0fa57cabc32a1fbd46777ac7ac937"


def registration(node_id: str = "dn1", **changes) -> dict:
    ip, zone = DA13[node_id]
    body = {
        "node_id": node_id,
        "disk_id": DISK,
        "ip": ip,
        "public_port": 9443,
        "internal_port": 9444,
        "zone": zone,
        "free_bytes": 15032385536,
    }
    return {**body, **changes}


def register(etcd, body=None, *, ip=None, **settings):
    body = body or registration()
    with internal_client(etcd, ip=ip or body["ip"], **settings) as client:
        return client.post(REGISTER, json=body)


def registered(etcd, node_id: str = "dn1") -> int:
    """Registra el nodo y devuelve su lease."""
    return lease_from_hex(register(etcd, registration(node_id)).json()["lease_id"])


def beat(etcd, lease_id, *, node_id="dn1", ip=None, free_bytes=1, done=(), bad=()):
    body = {
        "node_id": node_id,
        "lease_id": lease_id if isinstance(lease_id, str) else lease_hex(lease_id),
        "free_bytes": free_bytes,
        "done_orders": list(done),
        "bad_blocks": list(bad),
    }
    with internal_client(etcd, ip=ip or DA13[node_id][0]) as client:
        return client.post(HEARTBEAT, json=body)


def copy_order(order_id: str, hash: str, target: str) -> dict:
    address = f"{DA13[target][0]}:9444"
    return {"order_id": order_id, "type": "copy", "user": "ana", "hash": hash, "target": {"node_id": target, "address": address}}


def delete_order(order_id: str, hash: str) -> dict:
    return {"order_id": order_id, "type": "delete", "user": "ana", "hash": hash, "written_before_ms": 1793628599000}


def assert_error(response, status: int, code: str) -> None:
    assert response.status_code == status, response.json()
    assert response.json()["error"] == code


# --- Registro ---


def test_register_writes_the_node_tied_to_a_lease_and_answers_heartbeat_s():
    etcd = FakeEtcd()
    response = register(etcd, node_lease_s=17, heartbeat_s=4)
    lease_id = lease_from_hex(response.json()["lease_id"])
    assert response.json() == {"lease_id": lease_hex(lease_id), "heartbeat_s": 4}
    assert etcd.kvs["/nodes/dn/dn1"].lease == lease_id
    assert etcd.ttls[lease_id] == 17  # DFS_NODE_LEASE_S
    assert etcd.value("/nodes/dn/dn1") == {
        "ip": "172.31.0.10",
        "public_port": 9443,
        "internal_port": 9444,
        "zone": "us-east-1a",
        "free_bytes": 15032385536,
    }
    assert etcd.value("/nodes/disk/dn1") == {"disk_id": DISK}


def test_a_new_registration_overwrites_the_previous_one():
    etcd = FakeEtcd()
    first = registered(etcd)
    second = registered(etcd)
    assert second != first
    assert etcd.kvs["/nodes/dn/dn1"].lease == second
    assert first in etcd.leases  # el lease viejo vence solo


def test_register_from_an_ip_that_is_not_the_nodes_is_403():
    etcd = FakeEtcd()
    # La IP de origen no es la del cuerpo.
    assert_error(register(etcd, ip="10.9.9.9"), 403, "E_FORBIDDEN")
    assert etcd.keys("/nodes/") == []
    # dn1 está vivo con su IP: otro nodo con el mismo DFS_NODE_ID no pisa su registro.
    registered(etcd)
    impostor = registration(ip="10.9.9.9")
    assert_error(register(etcd, impostor, ip="10.9.9.9"), 403, "E_FORBIDDEN")
    assert etcd.value("/nodes/dn/dn1")["ip"] == "172.31.0.10"


def test_a_new_disk_removes_the_node_from_every_stored_on_before_saving_it():
    etcd = FakeEtcd()
    etcd.seed("/nodes/disk/dn1", {"disk_id": "disco-viejo"})
    add_block(etcd, "ana", H0, stored_on=["dn1", "dn2"])
    add_block(etcd, "bruno", H1, stored_on=["dn3", "dn1"])
    add_block(etcd, "ana", H2, stored_on=["dn2", "dn3"])
    assert register(etcd).status_code == 200
    assert etcd.value(f"/blocks/ana/{H0}")["stored_on"] == ["dn2"]
    assert etcd.value(f"/blocks/bruno/{H1}")["stored_on"] == ["dn3"]
    assert etcd.value(f"/blocks/ana/{H2}")["stored_on"] == ["dn2", "dn3"]
    assert etcd.value("/nodes/disk/dn1") == {"disk_id": DISK}


def test_the_same_disk_keeps_its_copies():
    etcd = FakeEtcd()
    etcd.seed("/nodes/disk/dn1", {"disk_id": DISK})
    add_block(etcd, "ana", H0, stored_on=["dn1", "dn2"])
    assert register(etcd).status_code == 200
    assert etcd.value(f"/blocks/ana/{H0}")["stored_on"] == ["dn1", "dn2"]


# --- Latidos ---


def test_heartbeat_renews_the_lease_rewrites_free_bytes_and_answers_the_pending_orders():
    etcd = FakeEtcd()
    lease_id = registered(etcd)
    etcd.seed("/orders/dn1/o1", copy_order("o1", H0, "dn3"))
    etcd.seed("/orders/dn1/o2", delete_order("o2", H1))
    etcd.seed("/orders/dn2/o3", copy_order("o3", H2, "dn1"))  # de otro nodo

    response = beat(etcd, lease_id, free_bytes=123)

    assert response.json() == {"orders": [copy_order("o1", H0, "dn3"), delete_order("o2", H1)]}
    assert etcd.keepalives == [lease_id]
    assert etcd.value("/nodes/dn/dn1")["free_bytes"] == 123
    assert etcd.kvs["/nodes/dn/dn1"].lease == lease_id  # en el mismo lease


def test_a_fulfilled_copy_adds_its_target_and_every_reported_order_is_deleted():
    etcd = FakeEtcd()
    lease_id = registered(etcd, "dn2")
    add_block(etcd, "ana", H0, stored_on=["dn2", "dn3"])
    etcd.seed("/orders/dn2/o1", copy_order("o1", H0, "dn1"))
    etcd.seed("/orders/dn2/o2", copy_order("o2", H1, "dn4"))  # su bloque ya no existe
    etcd.seed("/orders/dn2/o3", delete_order("o3", H2))
    etcd.seed("/orders/dn2/o4", copy_order("o4", H2, "dn1"))  # sigue pendiente

    response = beat(etcd, lease_id, node_id="dn2", done=["o1", "o2", "o3", "ya-no-existe"])

    assert response.json() == {"orders": [copy_order("o4", H2, "dn1")]}
    assert etcd.value(f"/blocks/ana/{H0}")["stored_on"] == ["dn2", "dn3", "dn1"]
    assert etcd.value(f"/blocks/ana/{H1}") is None  # no se crea
    assert etcd.keys("/orders/") == ["/orders/dn2/o4"]


def test_bad_blocks_remove_this_node_from_stored_on():
    etcd = FakeEtcd()
    lease_id = registered(etcd)
    add_block(etcd, "ana", H2, stored_on=["dn1", "dn2", "dn3"])
    bad = [{"user": "ana", "hash": H2}, {"user": "ana", "hash": H0}]  # H0 no tiene clave: no pasa nada
    assert beat(etcd, lease_id, bad=bad).status_code == 200
    assert etcd.value(f"/blocks/ana/{H2}")["stored_on"] == ["dn2", "dn3"]
    assert etcd.value(f"/blocks/ana/{H0}") is None


def test_heartbeat_with_a_lease_that_is_missing_or_not_the_nodes_is_404():
    etcd = FakeEtcd()
    dn1 = registered(etcd)
    dn2 = registered(etcd, "dn2")
    # El lease de otro nodo: 404, y ese lease no se renueva.
    assert_error(beat(etcd, dn2), 404, "E_NOT_FOUND")
    assert_error(beat(etcd, "no-es-hex"), 404, "E_NOT_FOUND")
    assert etcd.keepalives == []
    # Lease vencido: su clave desapareció con él.
    etcd.leases.discard(dn1)
    etcd.kvs.pop("/nodes/dn/dn1")
    assert_error(beat(etcd, dn1), 404, "E_NOT_FOUND")


class LeaseEndsBeforeKeepalive(FakeEtcd):
    async def lease_keepalive(self, lease_id):
        await self.lease_revoke(lease_id)
        return await super().lease_keepalive(lease_id)


def test_heartbeat_when_the_lease_expires_just_before_the_keepalive_is_404():
    etcd = LeaseEndsBeforeKeepalive()
    assert_error(beat(etcd, registered(etcd)), 404, "E_NOT_FOUND")


def test_heartbeat_from_an_ip_that_is_not_the_nodes_is_403():
    etcd = FakeEtcd()
    lease_id = registered(etcd)
    assert_error(beat(etcd, lease_id, ip="10.9.9.9"), 403, "E_FORBIDDEN")
    assert etcd.keepalives == []


@pytest.mark.parametrize("route", [REGISTER, HEARTBEAT])
def test_an_invalid_body_is_400(route):
    with internal_client() as client:
        response = client.post(route, json={"node_id": "dn1"})
    assert_error(response, 400, "E_BAD_REQUEST")


# --- La IP de origen (función pura) ---


@pytest.mark.parametrize(
    ("first", "second", "same"),
    [
        ("10.88.0.31", "10.88.0.31", True),
        ("::ffff:10.88.0.31", "10.88.0.31", True),  # IPv6 mapeada: un servidor que escucha en ::
        ("10.88.0.31", "10.88.0.32", False),
        ("no-es-una-ip", "10.88.0.31", False),
        ("10.88.0.31", None, False),  # el nodo no está vivo
    ],
)
def test_same_ip(first, second, same):
    assert same_ip(first, second) is same


def test_register_from_an_ipv6_mapped_address_is_the_same_node():
    etcd = FakeEtcd()
    assert register(etcd, ip="::ffff:172.31.0.10").status_code == 200
