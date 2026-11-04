"""Pruebas de la tarea 09: almacén de bloques del DataNode (src/datanode/storage.py)."""

import errno
import hashlib
import os
import shutil

import pytest

from datanode import storage
from datanode.storage import BlockStore
from dfs_common.errors import DFSError, ErrorCode

MIB = 1024 * 1024
KEY = bytes(range(32))
OTHER_KEY = bytes(32)
# Un bloque con un patrón reconocible, para buscarlo en el disco.
PATTERN = b"DFSHA-TEXTO-EN-CLARO-"
SMALL = PATTERN * 10


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


async def chunks_of(data: bytes, size: int = 64 * 1024):
    for start in range(0, len(data), size):
        yield data[start : start + size]


def files_under(folder) -> list:
    return sorted(path for path in folder.rglob("*") if path.is_file())


def error_code(call) -> ErrorCode:
    with pytest.raises(DFSError) as info:
        call()
    return info.value.code


async def async_error_code(awaitable) -> ErrorCode:
    with pytest.raises(DFSError) as info:
        await awaitable
    return info.value.code


@pytest.fixture
def store(tmp_path):
    return BlockStore(tmp_path, KEY, 8 * MIB)


# --- Escribir y leer ---


@pytest.mark.anyio
@pytest.mark.parametrize("size", [8 * MIB, 10], ids=["8MiB", "10B"])
async def test_write_and_read_back(store, size):
    data = os.urandom(size)
    await store.write("ana", sha(data), chunks_of(data))
    assert store.read("ana", sha(data)) == data


@pytest.mark.anyio
async def test_block_lives_in_the_user_folder_with_nonce_and_tag(store, tmp_path):
    await store.write("ana", sha(SMALL), chunks_of(SMALL))
    assert files_under(tmp_path) == [tmp_path / "ana" / sha(SMALL)]
    assert (tmp_path / "ana" / sha(SMALL)).stat().st_size == 12 + len(SMALL) + 16


@pytest.mark.anyio
async def test_wrong_hash_is_422_and_leaves_no_file(store, tmp_path):
    code = await async_error_code(store.write("ana", sha(b"otro contenido"), chunks_of(SMALL)))
    assert code == ErrorCode.E_CHECKSUM
    assert files_under(tmp_path) == []


@pytest.mark.anyio
async def test_oversized_body_is_400_cut_while_arriving_and_leaves_no_file(tmp_path):
    store = BlockStore(tmp_path, KEY, 1000)
    data = b"x" * 5000
    sent = []

    async def counted_chunks():
        async for chunk in chunks_of(data, 100):
            sent.append(chunk)
            yield chunk

    code = await async_error_code(store.write("ana", sha(data), counted_chunks()))

    assert code == ErrorCode.E_BAD_REQUEST
    assert len(sent) == 11  # se corta al pasar de 1000 bytes, sin leer el resto
    assert files_under(tmp_path) == []


@pytest.mark.anyio
async def test_disk_full_is_507_and_leaves_no_file(store, tmp_path, monkeypatch):
    def disk_full(fd):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(storage.os, "fsync", disk_full)
    code = await async_error_code(store.write("ana", sha(SMALL), chunks_of(SMALL)))
    assert code == ErrorCode.E_NO_SPACE
    assert files_under(tmp_path) == []


# --- Cifrado en disco (DA-09) ---


@pytest.mark.anyio
async def test_disk_holds_no_plaintext(store, tmp_path):
    data = PATTERN * 50_000
    await store.write("ana", sha(data), chunks_of(data))
    on_disk = (tmp_path / "ana" / sha(data)).read_bytes()
    assert PATTERN not in on_disk
    assert sha(on_disk) != sha(data)


@pytest.mark.anyio
async def test_same_block_written_twice_uses_a_new_nonce(store, tmp_path):
    path = tmp_path / "ana" / sha(SMALL)
    await store.write("ana", sha(SMALL), chunks_of(SMALL))
    first = path.read_bytes()
    await store.write("ana", sha(SMALL), chunks_of(SMALL))
    assert path.read_bytes() != first
    assert store.read("ana", sha(SMALL)) == SMALL


@pytest.mark.anyio
async def test_another_node_key_cannot_read_the_block(store, tmp_path):
    await store.write("ana", sha(SMALL), chunks_of(SMALL))
    other_node = BlockStore(tmp_path, OTHER_KEY, 8 * MIB)
    assert error_code(lambda: other_node.read("ana", sha(SMALL))) == ErrorCode.E_CHECKSUM


@pytest.mark.anyio
@pytest.mark.parametrize("target", ["other_hash", "other_user"])
async def test_file_copied_under_another_name_is_422_and_reported(store, tmp_path, target):
    other = PATTERN * 3
    await store.write("ana", sha(SMALL), chunks_of(SMALL))
    await store.write("ana", sha(other), chunks_of(other))
    user, hash = ("ana", sha(other)) if target == "other_hash" else ("bob", sha(SMALL))
    (tmp_path / user).mkdir(exist_ok=True)
    shutil.copyfile(tmp_path / "ana" / sha(SMALL), tmp_path / user / hash)

    assert error_code(lambda: store.read(user, hash)) == ErrorCode.E_CHECKSUM
    assert store.pop_bad_blocks() == [(user, hash)]


@pytest.mark.anyio
async def test_changed_byte_is_422_deletes_the_copy_and_is_reported_once(store, tmp_path):
    await store.write("ana", sha(SMALL), chunks_of(SMALL))
    path = tmp_path / "ana" / sha(SMALL)
    damaged = bytearray(path.read_bytes())
    damaged[20] ^= 0x01
    path.write_bytes(bytes(damaged))

    assert error_code(lambda: store.read("ana", sha(SMALL))) == ErrorCode.E_CHECKSUM
    assert not path.exists()
    # La segunda lectura ya no lo encuentra (404), pero el bloque no se repite en la lista.
    assert error_code(lambda: store.read("ana", sha(SMALL))) == ErrorCode.E_NOT_FOUND
    assert store.pop_bad_blocks() == [("ana", sha(SMALL))]
    assert store.pop_bad_blocks() == []


def test_missing_block_is_404_and_reported(store):
    # docs/contratos.md, sección 4: el MetaServer creía que la copia estaba aquí.
    assert error_code(lambda: store.read("ana", sha(SMALL))) == ErrorCode.E_NOT_FOUND
    assert store.pop_bad_blocks() == [("ana", sha(SMALL))]


@pytest.mark.parametrize(("user", "hash"), [("../ana", sha(SMALL)), ("ana", "../../etc/passwd"), ("Ana", sha(SMALL))])
def test_names_that_are_not_user_and_hash_are_rejected(store, user, hash):
    assert error_code(lambda: store.read(user, hash)) == ErrorCode.E_BAD_REQUEST


# --- Borrado por fecha (DA-02, regla del instante T) ---


@pytest.mark.anyio
async def test_delete_if_older_on_both_sides_of_the_instant(store, tmp_path):
    await store.write("ana", sha(SMALL), chunks_of(SMALL))
    written_ms = 1_793_628_000_000
    os.utime(tmp_path / "ana" / sha(SMALL), ns=(written_ms * 1_000_000, written_ms * 1_000_000))

    assert store.delete_if_older("ana", sha(SMALL), written_ms) is False  # escrito justo en T: se queda
    assert (tmp_path / "ana" / sha(SMALL)).exists()
    assert store.delete_if_older("ana", sha(SMALL), written_ms + 1) is True
    assert not (tmp_path / "ana" / sha(SMALL)).exists()
    assert store.delete_if_older("ana", sha(SMALL), written_ms + 1) is False  # ya no está


@pytest.mark.anyio
async def test_rewritten_block_survives_a_late_delete_order(store, tmp_path):
    path = tmp_path / "ana" / sha(SMALL)
    await store.write("ana", sha(SMALL), chunks_of(SMALL))
    old_ms = 1_793_628_000_000
    os.utime(path, ns=(old_ms * 1_000_000, old_ms * 1_000_000))

    await store.write("ana", sha(SMALL), chunks_of(SMALL))  # el cliente lo vuelve a subir

    assert store.delete_if_older("ana", sha(SMALL), old_ms + 1000) is False
    assert store.read("ana", sha(SMALL)) == SMALL


# --- Arranque y espacio libre ---


@pytest.mark.anyio
async def test_leftover_temporaries_are_removed_at_start(store, tmp_path):
    await store.write("ana", sha(SMALL), chunks_of(SMALL))
    leftover = tmp_path / "ana" / f"{sha(PATTERN)}.tmp-0123abcd"
    leftover.write_bytes(b"medio bloque cifrado")

    BlockStore(tmp_path, KEY, 8 * MIB)

    assert files_under(tmp_path) == [tmp_path / "ana" / sha(SMALL)]


def test_free_bytes_reports_the_data_folder(store, tmp_path):
    assert 0 < store.free_bytes() <= shutil.disk_usage(tmp_path).total
