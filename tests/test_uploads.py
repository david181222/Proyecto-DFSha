"""Pruebas de la tarea 12: POST /uploads, realloc y commit (docs/contratos.md, secciones 2, 8 y 13).

Usan FakeEtcd con los 4 DataNodes de DA-13 y start = 1, el inicio de los ejemplos del contrato:
así los primarios y secundarios son los de esos ejemplos. Para el commit, las claves de los
bloques se escriben directo (add_block), como si el primario ya los hubiera confirmado; la
confirmación de verdad está en tests/test_blocks.py y en la prueba de punta a punta.
"""

import time
import uuid

import pytest

from dfs_common.errors import DFSError, ErrorCode
from dfs_common.security import verify_pass
from metaserver import uploads
from metaserver.etcd import lease_from_hex
from metaserver.uploads import check_commit, expected_sizes

from fakes import DA13, PASS_KEY, FakeEtcd, add_block, add_dir, add_node, bearer, internal_client, public_client

MIB = 1024 * 1024
BLOCK = 8 * MIB
H0 = "6ce5a55b446b43e4e935c44695a9a09f161e8e87d1ff2359adebc5f9ee5ee5cf"
H1 = "b64bb9046f61749097db060781bcbbba317c08ff0ac378e9aed9a6d18217e449"
H2 = "8de70b36bbfaaf4bd8fecc81370e32e388e0fa57cabc32a1fbd46777ac7ac937"
H_OLD = "29124d08ab194c99b65e04622016f3306c6c39ce331241c0e089b10e66411607"
PATH = "/docs/informe.pdf"
FILE_ID = "3f0c9a7e-1b2d-4c5e-8f9a-0b1c2d3e4f50"
UPLOADS = "/api/v1/uploads"
ALL_ZONES = ["dn1", "dn2", "dn3"]


def spec(index: int, hash: str, size: int = BLOCK) -> dict:
    return {"index": index, "hash": hash, "size": size}


THREE = [spec(0, H0), spec(1, H1), spec(2, H2, 4 * MIB)]  # el ejemplo: 20 MiB en tres bloques
SIZE = 20 * MIB


@pytest.fixture(autouse=True)
def start_of_the_examples(monkeypatch):
    monkeypatch.setattr(uploads, "new_start", lambda: 1)


class RaceBeforeTxn(FakeEtcd):
    """Corre race(etcd) justo antes de la próxima transacción: la del commit, si se arma antes de llamarlo."""

    race = None

    async def txn(self, compare, success, failure):
        race, self.race = self.race, None
        if race:
            race(self)
        return await super().txn(compare, success, failure)


def cluster() -> RaceBeforeTxn:
    etcd = RaceBeforeTxn()
    for node_id in DA13:
        add_node(etcd, node_id)
    add_dir(etcd, "ana", "/docs")
    return etcd


def publish(etcd, *, hashes, size, version=3, path=PATH, block_size=BLOCK, stored_on=ALL_ZONES, user="ana"):
    """Un archivo ya publicado: su entrada, el manifiesto de su versión y las claves de sus bloques."""
    etcd.seed(f"/ns/{user}{path}", {"type": "file", "file_id": FILE_ID, "version": version})
    manifest = {
        "user": user,
        "size": size,
        "block_size": block_size,
        "hashes": hashes,
        "file_sha256": None,
        "created_at": 1793628000000,
        "replaced_at": None,
    }
    etcd.seed(f"/files/{FILE_ID}/v/{version}", manifest)
    for hash, block_size_i in zip(hashes, expected_sizes(size, block_size)):
        add_block(etcd, user, hash, stored_on=list(stored_on), size=block_size_i)


def confirmed(etcd, blocks, *, stored_on=ALL_ZONES, user="ana"):
    """Las claves de los bloques, como si el primario los hubiera confirmado en 2 zonas."""
    for block in blocks:
        add_block(etcd, user, block["hash"], stored_on=list(stored_on), size=block["size"])


def take(client, path=PATH, *, user="ana") -> str:
    return client.post("/api/v1/locks", json={"path": path, "wait_s": 0}, headers=bearer(user)).json()["lock_id"]


def reserve(client, lock_id, *, blocks=THREE, base_version=0, mode="w", block_size=BLOCK, path=PATH, user="ana"):
    body = {
        "path": path,
        "mode": mode,
        "base_version": base_version,
        "block_size": block_size,
        "lock_id": lock_id,
        "blocks": blocks,
    }
    return client.post(UPLOADS, json=body, headers=bearer(user))


def realloc(client, upload_id, indices, exclude=(), *, user="ana"):
    body = {"indices": indices, "exclude_nodes": list(exclude)}
    return client.post(f"{UPLOADS}/{upload_id}/realloc", json=body, headers=bearer(user))


def commit(client, upload_id, lock_id, *, hashes, size, base_version=0, file_sha256=None, user="ana"):
    body = {"base_version": base_version, "lock_id": lock_id, "hashes": hashes, "size": size, "file_sha256": file_sha256}
    return client.post(f"{UPLOADS}/{upload_id}/commit", json=body, headers=bearer(user))


def assert_error(response, status: int, code: str) -> None:
    assert response.status_code == status, response.json()
    assert response.json()["error"] == code


def secondaries(assignment: dict) -> list[str]:
    return verify_pass(assignment["pass"], PASS_KEY, hash=assignment["hash"], op="w").secondaries


# --- Reserva ---


def test_reserve_answers_like_the_contract_example():
    etcd = cluster()
    publish(etcd, hashes=[H_OLD], size=4 * MIB, version=3)
    add_block(etcd, "ana", H0, stored_on=["dn2", "dn3", "dn4"], touched_at=1)  # el bloque 0 no cambió
    with public_client(etcd) as client:
        lock_id = take(client)
        response = reserve(client, lock_id, base_version=3)

    assert response.status_code == 200, response.json()
    body = response.json()
    assert body["present"] == [0]
    # Los mismos primarios y secundarios de los pases del ejemplo: los secundarios con el puerto interno.
    assert [(item["index"], item["primary"]) for item in body["missing"]] == [
        (1, {"node_id": "dn3", "address": "172.31.32.10:9443"}),
        (2, {"node_id": "dn4", "address": "172.31.16.20:9443"}),
    ]
    assert [secondaries(item) for item in body["missing"]] == [
        ["172.31.16.20:9444", "172.31.0.10:9444"],
        ["172.31.0.10:9444", "172.31.16.10:9444"],
    ]
    assert etcd.value(f"/blocks/ana/{H0}")["touched_at"] > 1  # la limpieza no lo borra (DA-02)

    upload = etcd.kvs[f"/uploads/{body['upload_id']}"]
    assert upload.lease == lease_from_hex(lock_id)  # desaparece sola si el lock vence
    assert upload.json() == {
        "user": "ana",
        "path": PATH,
        "mode": "w",
        "base_version": 3,
        "block_size": BLOCK,
        "lock_key": f"/locks/ana{PATH}/{lock_id}",
        "blocks": THREE,
        "created_at": upload.json()["created_at"],
    }
    assert uuid.UUID(body["upload_id"]).version == 4


def test_a_write_pass_is_signed_for_its_block_and_lasts_dfs_pass_ttl_s():
    etcd = cluster()
    with public_client(etcd, pass_ttl_s=123) as client:
        body = reserve(client, take(client)).json()
    for item in body["missing"]:
        claims = verify_pass(item["pass"], PASS_KEY, hash=item["hash"], op="w")
        assert claims.user == "ana"
        assert abs(claims.exp - (time.time() + 123)) <= 2
        assert item["primary"]["address"].endswith(":9443")
        assert all(address.endswith(":9444") for address in claims.secondaries)


def test_a_stored_block_without_two_live_zones_goes_to_missing():
    etcd = cluster()
    add_block(etcd, "ana", H0, stored_on=["dn2", "dn4"], touched_at=1)  # las dos copias en la zona b
    with public_client(etcd) as client:
        body = reserve(client, take(client)).json()
    assert body["present"] == []
    assert [item["index"] for item in body["missing"]] == [0, 1, 2]


def test_an_empty_file_has_nothing_to_send():
    etcd = cluster()
    with public_client(etcd) as client:
        body = reserve(client, take(client), blocks=[]).json()
    assert (body["present"], body["missing"]) == ([], [])


def test_reserve_errors():
    etcd = cluster()
    publish(etcd, hashes=[H_OLD], size=4 * MIB, version=3)
    etcd.seed("/ns/ana/notas.txt", {"type": "file", "file_id": "otro", "version": 1})
    with public_client(etcd) as client:
        lock_id = take(client)
        other_file = take(client, "/docs/otro.txt")
        brunos = take(client, user="bruno")
        assert_error(reserve(client, lock_id, base_version=2), 409, "E_VERSION_CONFLICT")
        assert_error(reserve(client, brunos, base_version=3), 403, "E_FORBIDDEN")
        assert_error(reserve(client, other_file, base_version=3), 409, "E_LOCK_LOST")
        assert_error(reserve(client, "7a00000000000001", base_version=3), 409, "E_LOCK_LOST")  # no existe
        assert_error(reserve(client, lock_id, base_version=3, block_size=4 * MIB, blocks=[]), 400, "E_BAD_REQUEST")
        assert_error(reserve(client, lock_id, base_version=3, blocks=[spec(0, H0), spec(0, H1)]), 400, "E_BAD_REQUEST")
        assert_error(reserve(client, lock_id, path="/docs"), 400, "E_BAD_REQUEST")  # directorio
        assert_error(reserve(client, lock_id, path="/"), 400, "E_BAD_REQUEST")
        assert_error(reserve(client, take(client, "/nada/x.txt"), path="/nada/x.txt"), 404, "E_NOT_FOUND")
        assert_error(reserve(client, take(client, "/notas.txt/x"), path="/notas.txt/x"), 404, "E_NOT_FOUND")


def test_append_and_rw_use_the_block_size_of_the_current_version():
    etcd = cluster()
    publish(etcd, hashes=[H_OLD], size=4 * MIB, version=3, block_size=4 * MIB)
    with public_client(etcd) as client:
        lock_id = take(client)
        for mode in ("a", "rw"):
            assert_error(reserve(client, lock_id, mode=mode, base_version=3, blocks=[]), 400, "E_BAD_REQUEST")
            ok = reserve(client, lock_id, mode=mode, base_version=3, block_size=4 * MIB, blocks=[])
            assert ok.status_code == 200, ok.json()
        # Sobre un archivo que no existe, a y rw lo crean: DFS_BLOCK_MIB, igual que w.
        new = take(client, "/docs/nuevo.txt")
        assert reserve(client, new, mode="a", path="/docs/nuevo.txt", blocks=[]).status_code == 200


def test_reserve_without_enough_datanodes_is_503_and_without_space_is_507():
    etcd = cluster()
    for node_id in ("dn1", "dn3"):
        etcd.kvs.pop(f"/nodes/dn/{node_id}")  # solo quedan dn2 y dn4, los dos en la zona b
    with public_client(etcd) as client:
        assert_error(reserve(client, take(client)), 503, "E_UNAVAILABLE")

    etcd = cluster()
    for node_id in DA13:
        add_node(etcd, node_id, free_bytes=MIB)
    with public_client(etcd) as client:
        assert_error(reserve(client, take(client)), 507, "E_NO_SPACE")


def test_reserve_with_w_3_needs_three_datanodes():
    etcd = cluster()
    for node_id in ("dn2", "dn4"):
        etcd.kvs.pop(f"/nodes/dn/{node_id}")  # la zona b caída: quedan dn1 y dn3
    with public_client(etcd, w=3) as client:
        assert_error(reserve(client, take(client)), 503, "E_UNAVAILABLE")
    with public_client(etcd, w=2) as client:
        body = reserve(client, take(client, "/docs/otro.txt"), path="/docs/otro.txt").json()
    assert all(len(secondaries(item)) == 1 for item in body["missing"])  # un primario y un secundario (DA-05)


# --- realloc ---


def test_realloc_respects_exclude_nodes():
    etcd = cluster()
    with public_client(etcd) as client:
        upload_id = reserve(client, take(client)).json()["upload_id"]
        response = realloc(client, upload_id, [2], exclude=["dn4"])
    # El ejemplo de realloc del contrato: sin dn4, primario dn1 y secundarios dn2 y dn3.
    [item] = response.json()["missing"]
    assert (item["index"], item["hash"], item["primary"]["node_id"]) == (2, H2, "dn1")
    assert secondaries(item) == ["172.31.16.10:9444", "172.31.32.10:9444"]


def test_realloc_with_no_exclusions_renews_the_passes():
    etcd = cluster()
    with public_client(etcd, pass_ttl_s=60) as client:
        upload_id = reserve(client, take(client)).json()["upload_id"]
    with public_client(etcd, pass_ttl_s=900) as client:
        missing = realloc(client, upload_id, [0, 2]).json()["missing"]
    assert [(item["index"], item["hash"]) for item in missing] == [(0, H0), (2, H2)]
    for item in missing:
        assert verify_pass(item["pass"], PASS_KEY, hash=item["hash"], op="w").exp >= time.time() + 800


def test_realloc_errors():
    etcd = cluster()
    with public_client(etcd) as client:
        upload_id = reserve(client, take(client)).json()["upload_id"]
        assert_error(realloc(client, upload_id, [3]), 400, "E_BAD_REQUEST")  # el índice 3 no es de la subida
        assert_error(realloc(client, upload_id, [0], user="bruno"), 403, "E_FORBIDDEN")
        assert_error(realloc(client, str(uuid.uuid4()), [0]), 404, "E_NOT_FOUND")
        assert_error(realloc(client, upload_id, [0], exclude=["dn1", "dn3"]), 503, "E_UNAVAILABLE")


# --- Commit ---


def prepared(client, etcd, *, blocks=THREE, base_version=0, mode="w", block_size=BLOCK, path=PATH):
    """Lock, reserva y bloques confirmados: lo que hay justo antes del commit. Devuelve (lock_id, upload_id)."""
    lock_id = take(client, path)
    response = reserve(client, lock_id, blocks=blocks, base_version=base_version, mode=mode, block_size=block_size, path=path)
    assert response.status_code == 200, response.json()
    confirmed(etcd, blocks)
    return lock_id, response.json()["upload_id"]


def test_commit_publishes_the_new_version_and_retains_the_previous_one():
    etcd = cluster()
    publish(etcd, hashes=[H_OLD], size=4 * MIB, version=3)
    with public_client(etcd) as client:
        lock_id, upload_id = prepared(client, etcd, base_version=3)
        before = time.time_ns() // 1_000_000
        response = commit(client, upload_id, lock_id, hashes=[H0, H1, H2], size=SIZE, base_version=3, file_sha256="f" * 64)

    assert response.json() == {"path": PATH, "version": 4}
    assert etcd.value(f"/ns/ana{PATH}") == {"type": "file", "file_id": FILE_ID, "version": 4}
    manifest = etcd.value(f"/files/{FILE_ID}/v/4")
    assert manifest == {
        "user": "ana",
        "size": SIZE,
        "block_size": BLOCK,
        "hashes": [H0, H1, H2],
        "file_sha256": "f" * 64,
        "created_at": manifest["created_at"],
        "replaced_at": None,
    }
    assert manifest["created_at"] >= before
    assert etcd.value(f"/files/{FILE_ID}/v/3")["replaced_at"] == manifest["created_at"]  # queda retenida (DA-14)
    assert etcd.keys("/uploads/") == []
    assert etcd.keys("/locks/") == [f"/locks/ana{PATH}/{lock_id}"]  # el commit no suelta el lock


@pytest.mark.parametrize("path", ["/docs/nuevo.pdf", "/nuevo.pdf"], ids=["en-un-directorio", "en-la-raiz"])
def test_commit_with_base_version_0_creates_the_file_with_a_new_file_id(path):
    etcd = cluster()
    with public_client(etcd) as client:
        lock_id, upload_id = prepared(client, etcd, path=path)
        response = commit(client, upload_id, lock_id, hashes=[H0, H1, H2], size=SIZE)
    assert response.json() == {"path": path, "version": 1}
    entry = etcd.value(f"/ns/ana{path}")
    assert entry["version"] == 1 and uuid.UUID(entry["file_id"]).version == 4
    assert etcd.value(f"/files/{entry['file_id']}/v/1")["file_sha256"] is None  # a y rw lo mandan null


def test_commit_of_an_empty_file():
    etcd = cluster()
    with public_client(etcd) as client:
        lock_id, upload_id = prepared(client, etcd, blocks=[])
        assert commit(client, upload_id, lock_id, hashes=[], size=0).json() == {"path": PATH, "version": 1}


def test_commit_without_a_confirmed_block_is_422():
    etcd = cluster()
    with public_client(etcd) as client:
        lock_id = take(client)
        upload_id = reserve(client, lock_id).json()["upload_id"]
        confirmed(etcd, THREE[:2])  # el bloque 2 no se confirmó
        assert_error(commit(client, upload_id, lock_id, hashes=[H0, H1, H2], size=SIZE), 422, "E_REPLICAS")
    assert etcd.value(f"/ns/ana{PATH}") is None


def test_commit_of_a_new_block_without_two_live_zones_is_422():
    etcd = cluster()
    with public_client(etcd) as client:
        lock_id, upload_id = prepared(client, etcd)
        add_block(etcd, "ana", H2, stored_on=["dn2", "dn4"], size=4 * MIB)  # solo la zona b
        assert_error(commit(client, upload_id, lock_id, hashes=[H0, H1, H2], size=SIZE), 422, "E_REPLICAS")


def test_commit_with_a_shape_that_does_not_match_the_size_is_400():
    etcd = cluster()
    with public_client(etcd) as client:
        lock_id, upload_id = prepared(client, etcd)
        assert_error(commit(client, upload_id, lock_id, hashes=[H0, H1], size=SIZE), 400, "E_BAD_REQUEST")
        assert_error(commit(client, upload_id, lock_id, hashes=[H0, H2, H1], size=SIZE), 400, "E_BAD_REQUEST")


def test_append_keeps_the_old_blocks_and_only_needs_their_keys():
    """Sección 13: los bloques de la versión base que a no cambia solo necesitan su clave."""
    etcd = cluster()
    publish(etcd, hashes=[H0, H_OLD], size=12 * MIB, version=3, stored_on=["dn2", "dn4"])  # H0, solo en la zona b
    new = [spec(1, H1), spec(2, H2, 4 * MIB)]  # anexar 8 MiB reescribe el último bloque y agrega otro
    with public_client(etcd) as client:
        lock_id, upload_id = prepared(client, etcd, blocks=new, base_version=3, mode="a")
        # Cambiar lo que ya estaba o truncar: 400.
        assert_error(commit(client, upload_id, lock_id, hashes=[H1, H1, H2], size=SIZE, base_version=3), 400, "E_BAD_REQUEST")
        response = commit(client, upload_id, lock_id, hashes=[H0, H1, H2], size=SIZE, base_version=3)
    assert response.json() == {"path": PATH, "version": 4}


def test_rw_cannot_truncate():
    etcd = cluster()
    publish(etcd, hashes=[H0, H1, H2], size=SIZE, version=3)
    with public_client(etcd) as client:
        lock_id, upload_id = prepared(client, etcd, blocks=[spec(0, H1)], base_version=3, mode="rw")
        assert_error(commit(client, upload_id, lock_id, hashes=[H1], size=BLOCK, base_version=3), 400, "E_BAD_REQUEST")
        response = commit(client, upload_id, lock_id, hashes=[H1, H1, H2], size=SIZE, base_version=3)
    assert response.json()["version"] == 4


def test_commit_lock_and_upload_errors():
    etcd = cluster()
    with public_client(etcd) as client:
        lock_id, upload_id = prepared(client, etcd)
        other_lock = take(client, "/docs/otro.txt")
        brunos_lock = take(client, "/b.txt", user="bruno")
        brunos_upload = reserve(client, brunos_lock, user="bruno", path="/b.txt", blocks=[]).json()["upload_id"]
        hashes = {"hashes": [H0, H1, H2], "size": SIZE}
        assert_error(commit(client, upload_id, other_lock, **hashes), 409, "E_LOCK_LOST")  # lock de otro archivo
        assert_error(commit(client, upload_id, brunos_lock, **hashes), 403, "E_FORBIDDEN")  # lock de otro usuario
        assert_error(commit(client, brunos_upload, lock_id, **hashes), 403, "E_FORBIDDEN")  # subida de otro usuario
        assert_error(commit(client, str(uuid.uuid4()), lock_id, **hashes), 404, "E_NOT_FOUND")
        assert_error(commit(client, upload_id, lock_id, base_version=1, **hashes), 409, "E_VERSION_CONFLICT")
        client.delete(f"/api/v1/locks/{lock_id}", headers=bearer())  # el lock se suelta (o vence)
        assert_error(commit(client, upload_id, lock_id, **hashes), 409, "E_LOCK_LOST")
    assert etcd.value(f"/ns/ana{PATH}") is None


def test_commit_with_an_old_base_version_is_409():
    etcd = cluster()
    publish(etcd, hashes=[H_OLD], size=4 * MIB, version=3)
    with public_client(etcd) as client:
        lock_id, upload_id = prepared(client, etcd, base_version=3)
        publish(etcd, hashes=[H_OLD], size=4 * MIB, version=4)  # alguien publicó la 4 (con un lock vencido)
        response = commit(client, upload_id, lock_id, hashes=[H0, H1, H2], size=SIZE, base_version=3)
    assert_error(response, 409, "E_VERSION_CONFLICT")


@pytest.mark.parametrize(
    ("race", "status", "code"),
    [
        (lambda etcd: etcd.kvs.pop("/ns/ana/docs"), 404, "E_NOT_FOUND"),  # rmdir del padre
        (lambda etcd: add_dir(etcd, "ana", "/docs"), 404, "E_NOT_FOUND"),  # el padre se borró y se creó de nuevo
        (lambda etcd: etcd.seed(f"/ns/ana{PATH}", {"type": "dir", "created_at": 1}), 409, "E_VERSION_CONFLICT"),
        (lambda etcd: [etcd.kvs.pop(key) for key in etcd.keys("/locks/")], 409, "E_LOCK_LOST"),
    ],
    ids=["padre-borrado", "padre-recreado", "ruta-creada", "lock-perdido"],
)
def test_a_commit_whose_transaction_fails_answers_what_changed(race, status, code):
    etcd = cluster()
    with public_client(etcd) as client:
        lock_id, upload_id = prepared(client, etcd)
        etcd.race = race  # justo antes de la transacción del commit
        response = commit(client, upload_id, lock_id, hashes=[H0, H1, H2], size=SIZE)
    assert_error(response, status, code)
    assert etcd.keys("/files/") == []  # no escribió nada


# --- check_commit y expected_sizes (funciones puras) ---


def test_expected_sizes():
    assert expected_sizes(0, BLOCK) == []
    assert expected_sizes(SIZE, BLOCK) == [BLOCK, BLOCK, 4 * MIB]
    assert expected_sizes(2 * BLOCK, BLOCK) == [BLOCK, BLOCK]
    assert expected_sizes(1, BLOCK) == [1]


BASE = {"size": 12 * MIB, "hashes": [H0, H_OLD]}


@pytest.mark.parametrize(
    ("hashes", "sizes", "size", "mode", "base"),
    [
        ([H0, H1, H2], [BLOCK, BLOCK, 4 * MIB], SIZE, "w", None),
        ([], [], 0, "w", None),
        ([H0, H1, H2], [BLOCK, BLOCK, 4 * MIB], SIZE, "a", BASE),
        ([H1, H_OLD], [BLOCK, 4 * MIB], 12 * MIB, "rw", BASE),  # rw puede cambiar cualquier bloque
        ([H0], [BLOCK], BLOCK, "w", BASE),  # w reemplaza: puede achicar el archivo
    ],
)
def test_check_commit_accepts(hashes, sizes, size, mode, base):
    check_commit(hashes, sizes, size, BLOCK, mode, base)


@pytest.mark.parametrize(
    ("hashes", "sizes", "size", "mode", "base", "message"),
    [
        ([H0, H1], [BLOCK, BLOCK], SIZE, "w", None, "no forman"),  # falta un bloque: un hueco
        ([H0, H1, H2], [BLOCK, 4 * MIB, BLOCK], SIZE, "w", None, "no forman"),  # un bloque corto en medio
        ([H0], [BLOCK], BLOCK, "a", BASE, "truncar"),
        ([H0], [BLOCK], BLOCK, "rw", BASE, "truncar"),
        ([H1, H1, H2], [BLOCK, BLOCK, 4 * MIB], SIZE, "a", BASE, "anexar"),
    ],
)
def test_check_commit_rejects(hashes, sizes, size, mode, base, message):
    with pytest.raises(DFSError, match=message) as caught:
        check_commit(hashes, sizes, size, BLOCK, mode, base)
    assert caught.value.code == ErrorCode.E_BAD_REQUEST


# --- De punta a punta, en memoria ---


def test_lock_reserve_confirm_commit_read_and_release():
    etcd = cluster()
    with public_client(etcd) as client:
        lock_id = take(client)
        reservation = reserve(client, lock_id).json()
        # Cada primario confirma su bloque en la API interna, desde su propia IP.
        for item in reservation["missing"]:
            stored_on = [item["primary"]["node_id"], "dn1" if item["primary"]["node_id"] != "dn1" else "dn2"]
            size = next(block["size"] for block in THREE if block["index"] == item["index"])
            with internal_client(etcd, ip=item["primary"]["address"].split(":")[0]) as internal:
                confirm = {"user": "ana", "hash": item["hash"], "size": size, "stored_on": stored_on}
                assert internal.post("/api/v1/internal/blocks/confirm", json=confirm).json() == {"ok": True}
        published = commit(client, reservation["upload_id"], lock_id, hashes=[H0, H1, H2], size=SIZE)
        assert published.json() == {"path": PATH, "version": 1}

        read = client.get("/api/v1/files/blocks", params={"path": PATH}, headers=bearer())
        assert [(block["index"], block["hash"], block["size"]) for block in read.json()["blocks"]] == [
            (0, H0, BLOCK),
            (1, H1, BLOCK),
            (2, H2, 4 * MIB),
        ]
        assert all(block["replicas"] for block in read.json()["blocks"])
        assert client.delete(f"/api/v1/locks/{lock_id}", headers=bearer()).json() == {"ok": True}
    assert etcd.keys("/locks/") == [] and etcd.keys("/uploads/") == []
