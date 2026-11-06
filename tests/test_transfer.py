"""Pruebas de transfer.py (tarea 17): subir una versión, bajar bloques y archivos, con reintentos.

Usan FakeDfsApi (tests/fakes_client.py) y bloques de 16 bytes, para que cada prueba sea rápida;
el archivo de 3,5 bloques con DFS_BLOCK_MIB=1 está en tests/test_cli_transfer.py.
"""

import hashlib
import threading
import time

import pytest
from fakes_client import FakeDfsApi

from dfs_client.locks import LockKeeper
from dfs_client.transfer import UploadStats, download_file, fetch_block, hash_file, read_block, upload_version
from dfs_common.errors import DFSError, ErrorCode

BS = 16  # bytes por bloque en estas pruebas
DATA = bytes(i % 251 for i in range(3 * BS + BS // 2))  # 3,5 bloques, todos distintos
PARTS = [DATA[i : i + BS] for i in range(0, len(DATA), BS)]
HASHES = [hashlib.sha256(part).hexdigest() for part in PARTS]


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def api():
    return FakeDfsApi()


@pytest.fixture
def put(api, tmp_path):
    """put(data, ...): toma el lock y sube data a /a.bin con upload_version, como dfs put."""

    def put(data: bytes = DATA, *, path="/a.bin", base_version=0, keeper=None, stats=None, parallel=4):
        source = tmp_path / "local.bin"
        source.write_bytes(data)
        blocks, hashes, file_sha256, size = hash_file(source, BS)
        lock_id = keeper.lock_id if keeper else api.lock(path).lock_id
        return upload_version(
            api,
            path=path,
            mode="w",
            base_version=base_version,
            lock_id=lock_id,
            block_size=BS,
            blocks=blocks,
            hashes=hashes,
            size=size,
            file_sha256=file_sha256,
            get_block_bytes=lambda index: read_block(source, BS, index),
            parallel=parallel,
            keeper=keeper,
            stats=stats,
        )

    return put


def fail_first(code: ErrorCode, times: int = 1, *, hash: str | None = None):
    """on_put que falla las primeras `times` veces (solo para ese hash, si se da) y anota en qué nodo."""
    failed: list[str] = []

    def on_put(node_id, block_hash, data):
        if (hash is None or block_hash == hash) and len(failed) < times:
            failed.append(node_id)
            return DFSError(code, "falló el envío")
        return None

    return on_put, failed


# --- hash_file y read_block ---


def test_hash_file_reads_block_by_block(tmp_path):
    source = tmp_path / "x"
    source.write_bytes(DATA)
    blocks, hashes, file_sha256, size = hash_file(source, BS)
    assert [(b.index, b.hash, b.size) for b in blocks] == [(0, HASHES[0], BS), (1, HASHES[1], BS), (2, HASHES[2], BS), (3, HASHES[3], BS // 2)]
    assert hashes == HASHES
    assert (file_sha256, size) == (sha(DATA), len(DATA))
    assert [read_block(source, BS, i) for i in range(4)] == PARTS


def test_hash_file_of_an_empty_file(tmp_path):
    source = tmp_path / "vacio"
    source.write_bytes(b"")
    assert hash_file(source, BS) == ([], [], sha(b""), 0)


# --- upload_version ---


def test_happy_path_sends_every_block_and_commits_the_hashes_in_order(api, put):
    stats = UploadStats()
    answer = put(stats=stats)
    assert (answer.path, answer.version) == ("/a.bin", 1)
    [reservation] = api.calls_to("uploads")
    assert reservation[-1] == [(0, HASHES[0], BS), (1, HASHES[1], BS), (2, HASHES[2], BS), (3, HASHES[3], BS // 2)]
    assert sorted(hash for _, hash, _ in api.calls_to("put_block")) == sorted(HASHES)
    [commit] = api.calls_to("commit")
    assert commit[1:] == (0, commit[2], HASHES, len(DATA), sha(DATA))
    assert (stats.sent, stats.present) == (4, 0)


def test_a_block_already_stored_is_not_sent_again(api, put):
    api.add_file("/otro.bin", [PARTS[1]], block_size=BS)
    stats = UploadStats()
    put(stats=stats)
    assert HASHES[1] not in [hash for _, hash, _ in api.calls_to("put_block")]
    assert len(api.calls_to("put_block")) == 3
    assert (stats.sent, stats.present) == (3, 1)
    assert api.calls_to("commit")[0][3] == HASHES  # el commit lleva la lista completa


@pytest.mark.parametrize("code", [ErrorCode.E_UNAVAILABLE, ErrorCode.E_CHECKSUM, ErrorCode.E_REPLICAS])
def test_a_primary_that_fails_leads_to_realloc_without_that_node(api, put, code):
    api.on_put, failed = fail_first(code, hash=HASHES[2])
    put()
    [realloc] = api.calls_to("realloc")
    assert realloc[1:] == ([2], failed)
    assert api.stored_on[HASHES[2]]
    assert api.calls_to("commit")


def test_the_excluded_nodes_of_a_block_accumulate(api, put):
    api.on_put, failed = fail_first(ErrorCode.E_UNAVAILABLE, times=2, hash=HASHES[0])
    put()
    assert [args[2] for args in api.calls_to("realloc")] == [failed[:1], failed[:2]]


def test_an_expired_pass_leads_to_realloc_with_no_exclusions(api, put):
    api.on_put, _ = fail_first(ErrorCode.E_AUTH, hash=HASHES[0])
    put()
    [realloc] = api.calls_to("realloc")
    assert realloc[1:] == ([0], [])


def test_three_failures_of_a_block_fail_the_upload(api, put):
    api.on_put, failed = fail_first(ErrorCode.E_UNAVAILABLE, times=99, hash=HASHES[0])
    with pytest.raises(DFSError) as caught:
        put(parallel=1)
    assert caught.value.code == ErrorCode.E_UNAVAILABLE
    assert len(failed) == 3
    assert len(api.calls_to("realloc")) == 2
    assert api.calls_to("commit") == []


def test_a_pass_that_keeps_expiring_is_not_reported_as_an_expired_session(api, put):
    """E_AUTH haría que la CLI pida iniciar sesión, pero lo vencido es el pase del bloque, no el JWT."""
    api.on_put, _ = fail_first(ErrorCode.E_AUTH, times=99, hash=HASHES[0])
    with pytest.raises(DFSError) as caught:
        put(parallel=1)
    assert caught.value.code == ErrorCode.E_UNAVAILABLE
    assert "pase" in caught.value.message


@pytest.mark.parametrize("code", [ErrorCode.E_FORBIDDEN, ErrorCode.E_NO_SPACE, ErrorCode.E_BAD_REQUEST])
def test_an_error_that_is_not_retryable_fails_at_once(api, put, code):
    api.on_put, failed = fail_first(code, hash=HASHES[0])
    with pytest.raises(DFSError) as caught:
        put()
    assert caught.value.code == code
    assert api.calls_to("realloc") == []
    assert api.calls_to("commit") == []


def test_a_commit_with_e_replicas_reserves_again_and_resends(api, put):
    def lose_copies():
        api.forget_block(HASHES[3])  # la copia de un bloque se perdió antes del commit
        return DFSError(ErrorCode.E_REPLICAS, "faltan copias")

    api.fail_call["commit"] = [lose_copies]
    answer = put()
    assert answer.version == 1
    reservations = api.calls_to("uploads")
    assert len(reservations) == 2 and reservations[0][-1] == reservations[1][-1]  # la misma lista
    assert [hash for _, hash, _ in api.calls_to("put_block")].count(HASHES[3]) == 2
    assert len(api.calls_to("commit")) == 2


def test_the_commit_is_tried_three_rounds_at_most(api, put):
    api.fail_call["commit"] = [DFSError(ErrorCode.E_REPLICAS)] * 5
    with pytest.raises(DFSError) as caught:
        put()
    assert caught.value.code == ErrorCode.E_REPLICAS
    assert len(api.calls_to("commit")) == 3
    assert len(api.calls_to("uploads")) == 3


def test_a_409_in_the_commit_is_raised(api, put):
    api.add_file("/a.bin", [b"version 1"], block_size=BS)
    with pytest.raises(DFSError) as caught:
        put(base_version=0)  # otro cliente ya publicó la versión 1
    assert caught.value.code == ErrorCode.E_VERSION_CONFLICT
    assert len(api.calls_to("commit")) == 1


def test_a_lost_lock_stops_the_upload(api, put):
    keeper = LockKeeper(api, api.lock("/a.bin").lock_id, renew=False)

    def lose_lock(node_id, hash, data):
        keeper.lost = True  # una renovación respondió 409 mientras se enviaba
        return None

    api.on_put = lose_lock
    with pytest.raises(DFSError) as caught:
        put(keeper=keeper, parallel=1)
    assert caught.value.code == ErrorCode.E_LOCK_LOST
    assert len(api.calls_to("put_block")) == 1
    assert api.calls_to("commit") == []


def test_only_parallel_blocks_are_in_memory_at_once(api, tmp_path):
    source = tmp_path / "local.bin"
    data = bytes(i % 251 for i in range(10 * BS))
    source.write_bytes(data)
    blocks, hashes, file_sha256, size = hash_file(source, BS)
    live, most = [0], [0]
    mutex = threading.Lock()

    def get_block_bytes(index):
        with mutex:
            live[0] += 1
            most[0] = max(most[0], live[0])
        return read_block(source, BS, index)

    def on_put(node_id, hash, data):
        time.sleep(0.01)
        with mutex:
            live[0] -= 1  # el envío terminó: el bloque ya no se necesita
        return None

    api.on_put = on_put
    upload_version(
        api, path="/a.bin", mode="w", base_version=0, lock_id=api.lock("/a.bin").lock_id, block_size=BS,
        blocks=blocks, hashes=hashes, size=size, file_sha256=file_sha256, get_block_bytes=get_block_bytes, parallel=2,
    )
    assert most[0] == 2
    assert len(api.calls_to("put_block")) == 10


def test_an_empty_file_is_committed_without_blocks(api, put):
    answer = put(b"")
    assert answer.version == 1
    assert api.calls_to("put_block") == []
    assert api.calls_to("commit")[0][3:] == ([], 0, sha(b""))


# --- fetch_block ---


@pytest.fixture
def manifest(api):
    api.add_file("/a.bin", PARTS, block_size=BS)
    return api.files_blocks("/a.bin")


@pytest.mark.parametrize(
    "error",
    [
        DFSError(ErrorCode.E_CHECKSUM, "la copia guardada está dañada"),
        DFSError(ErrorCode.E_NOT_FOUND, "el bloque no está en este DataNode"),
        DFSError(ErrorCode.E_UNAVAILABLE, "el DataNode no respondió"),
    ],
)
def test_it_passes_to_the_next_replica(api, manifest, error):
    api.on_get = lambda node_id, hash: error if node_id == "dn1" else None
    assert fetch_block(api, manifest.blocks[0], path="/a.bin", version=1) == PARTS[0]
    assert [args[0] for args in api.calls_to("get_block")] == ["dn1", "dn2"]


def test_a_wrong_hash_passes_to_the_next_replica(api, manifest):
    api.on_get = lambda node_id, hash: b"contenido equivocado" if node_id == "dn1" else None
    assert fetch_block(api, manifest.blocks[0], path="/a.bin", version=1) == PARTS[0]
    assert [args[0] for args in api.calls_to("get_block")] == ["dn1", "dn2"]


def test_an_expired_pass_refreshes_the_manifest_of_the_same_version(api, manifest):
    api.add_file("/a.bin", [b"version nueva"], block_size=BS)  # ya hay una versión 2
    expired = [True]

    def on_get(node_id, hash):
        if expired[0]:
            expired[0] = False
            return DFSError(ErrorCode.E_AUTH, "pase vencido")
        return None

    api.on_get = on_get
    assert fetch_block(api, manifest.blocks[1], path="/a.bin", version=1) == PARTS[1]
    assert api.calls_to("files_blocks")[-1] == ("/a.bin", 1)
    first, second = api.calls_to("get_block")
    assert first[2] != second[2]  # con el pase nuevo


def test_a_pass_that_expires_again_is_not_refreshed_twice(api, manifest):
    api.on_get = lambda node_id, hash: DFSError(ErrorCode.E_AUTH, "pase vencido")
    with pytest.raises(DFSError) as caught:
        fetch_block(api, manifest.blocks[0], path="/a.bin", version=1)
    assert caught.value.code == ErrorCode.E_UNAVAILABLE
    assert len(api.calls_to("files_blocks")) == 2  # el del fixture y una renovación


def test_if_no_replica_works_it_is_unavailable_with_the_block_index(api, manifest):
    api.on_get = lambda node_id, hash: DFSError(ErrorCode.E_CHECKSUM, "dañada")
    with pytest.raises(DFSError) as caught:
        fetch_block(api, manifest.blocks[2], path="/a.bin", version=1)
    assert caught.value.code == ErrorCode.E_UNAVAILABLE
    assert "2" in caught.value.message
    assert len(api.calls_to("get_block")) == 3


def test_a_block_without_live_replicas_is_unavailable(api):
    api.add_file("/a.bin", PARTS, block_size=BS)
    api.forget_block(HASHES[0])
    block = api.files_blocks("/a.bin").blocks[0]
    with pytest.raises(DFSError) as caught:
        fetch_block(api, block, path="/a.bin", version=1)
    assert caught.value.code == ErrorCode.E_UNAVAILABLE
    assert "0" in caught.value.message


def test_another_error_is_raised_as_is(api, manifest):
    api.on_get = lambda node_id, hash: DFSError(ErrorCode.E_FORBIDDEN, "pase inválido")
    with pytest.raises(DFSError) as caught:
        fetch_block(api, manifest.blocks[0], path="/a.bin", version=1)
    assert caught.value.code == ErrorCode.E_FORBIDDEN


# --- download_file ---


def test_download_writes_each_block_in_its_place(api, manifest, tmp_path):
    target = tmp_path / "bajado.bin"
    download_file(api, manifest, target, parallel=3)
    assert target.read_bytes() == DATA
    assert not (tmp_path / "bajado.bin.part").exists()


def test_a_file_hash_that_does_not_match_leaves_neither_file_nor_part(api, manifest, tmp_path):
    manifest.file_sha256 = "0" * 64
    target = tmp_path / "bajado.bin"
    with pytest.raises(DFSError) as caught:
        download_file(api, manifest, target, parallel=2)
    assert caught.value.code == ErrorCode.E_CHECKSUM
    assert list(tmp_path.iterdir()) == []


def test_without_file_sha256_the_file_hash_is_not_compared(api, manifest, tmp_path):
    manifest.file_sha256 = None  # versión publicada con a o rw
    download_file(api, manifest, tmp_path / "bajado.bin", parallel=2)
    assert (tmp_path / "bajado.bin").read_bytes() == DATA


def test_a_failed_block_removes_the_part_and_keeps_the_old_file(api, manifest, tmp_path):
    target = tmp_path / "bajado.bin"
    target.write_bytes(b"lo que habia antes")
    api.on_get = lambda node_id, hash: DFSError(ErrorCode.E_NOT_FOUND, "no está") if hash == HASHES[3] else None
    with pytest.raises(DFSError):
        download_file(api, manifest, target, parallel=2)
    assert target.read_bytes() == b"lo que habia antes"
    assert not (tmp_path / "bajado.bin.part").exists()


def test_a_node_that_does_not_answer_goes_last_for_the_next_blocks(api, manifest, tmp_path):
    """Un nodo caído cuesta un timeout una vez, no uno por bloque: los siguientes lo prueban al final."""
    api.on_get = lambda node_id, hash: DFSError(ErrorCode.E_UNAVAILABLE, "no respondió") if node_id == "dn1" else None
    download_file(api, manifest, tmp_path / "bajado.bin", parallel=1)
    assert (tmp_path / "bajado.bin").read_bytes() == DATA
    assert [args[0] for args in api.calls_to("get_block")].count("dn1") == 1


def test_a_suspect_node_is_still_tried_when_the_others_fail(api, manifest, tmp_path):
    down_once = [True]

    def on_get(node_id, hash):
        if node_id == "dn1" and down_once[0]:
            down_once[0] = False
            return DFSError(ErrorCode.E_UNAVAILABLE, "no respondió")  # dn1 queda como sospechoso
        if hash == HASHES[1] and node_id != "dn1":
            return DFSError(ErrorCode.E_NOT_FOUND, "no está")
        return None

    api.on_get = on_get
    download_file(api, manifest, tmp_path / "bajado.bin", parallel=1)
    assert (tmp_path / "bajado.bin").read_bytes() == DATA
    tried_for_block_1 = [args[0] for args in api.calls_to("get_block") if args[1] == HASHES[1]]
    assert tried_for_block_1 == ["dn2", "dn3", "dn1"]  # el sospechoso, al final


@pytest.mark.parametrize("code", [ErrorCode.E_CHECKSUM, ErrorCode.E_NOT_FOUND])
def test_a_damaged_or_missing_copy_does_not_make_the_node_suspect(api, manifest, tmp_path, code):
    """El nodo respondió: el problema es de esa copia, no del nodo."""
    api.on_get = lambda node_id, hash: DFSError(code, "x") if (node_id, hash) == ("dn1", HASHES[0]) else None
    download_file(api, manifest, tmp_path / "bajado.bin", parallel=1)
    assert [args[0] for args in api.calls_to("get_block")].count("dn1") == len(PARTS)


def test_fetch_block_without_suspects_keeps_the_order(api, manifest):
    api.on_get = lambda node_id, hash: DFSError(ErrorCode.E_UNAVAILABLE, "no respondió") if node_id == "dn1" else None
    fetch_block(api, manifest.blocks[0], path="/a.bin", version=1)
    fetch_block(api, manifest.blocks[1], path="/a.bin", version=1)
    assert [args[0] for args in api.calls_to("get_block")] == ["dn1", "dn2", "dn1", "dn2"]


def test_an_empty_file_is_downloaded_as_an_empty_file(api, tmp_path):
    api.add_file("/vacio", [], block_size=BS)
    download_file(api, api.files_blocks("/vacio"), tmp_path / "vacio", parallel=4)
    assert (tmp_path / "vacio").read_bytes() == b""
