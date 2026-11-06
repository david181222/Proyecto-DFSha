"""Pruebas de la tarea 18: open, read, write y close del cliente (src/dfs_client/files.py).

Usan FakeDfsApi (tests/fakes_client.py) con DFS_BLOCK_MIB=1: un bloque mide 1 MiB.
"""

import hashlib
import os

import pytest
from fakes_client import FakeDfsApi

from dfs_client.files import LOST_MESSAGE, DfsFile
from dfs_common.errors import DFSError, ErrorCode

MIB = 1024 * 1024


@pytest.fixture
def api():
    fake = FakeDfsApi()
    fake.dirs.add("/docs")
    return fake


def blocks_of(data: bytes) -> list[bytes]:
    return [data[start : start + MIB] for start in range(0, len(data), MIB)] or []


def content(api, path: str) -> bytes:
    """El contenido publicado de path, leído con un DfsFile en modo r."""
    with DfsFile.open(api, path, "r") as file:
        return file.read(0, file.size)


def error_of(call) -> DFSError:
    with pytest.raises(DFSError) as info:
        call()
    return info.value


def reserved(api) -> list[int]:
    """Los índices de la última reserva (POST /uploads)."""
    return [index for index, _, _ in api.calls_to("uploads")[-1][5]]


# --- w: crear o reemplazar ---


def test_w_creates_a_file(api):
    data = os.urandom(MIB + 100)
    with DfsFile.open(api, "/docs/nuevo.bin", "w") as file:
        assert (file.version, file.size) == (0, 0)
        file.write(0, data)
    assert content(api, "/docs/nuevo.bin") == data
    [commit] = api.calls_to("commit")
    assert commit[1] == 0  # base_version de un archivo nuevo
    assert commit[5] == hashlib.sha256(data).hexdigest()  # en w se calcula el SHA-256 del archivo


def test_w_replaces_an_existing_file_starting_empty(api):
    api.add_file("/docs/a.bin", [b"viejo" * 1000])
    with DfsFile.open(api, "/docs/a.bin", "w") as file:
        assert (file.version, file.size, file.read(0, 100)) == (1, 0, b"")
        file.write(0, b"nuevo")
    assert content(api, "/docs/a.bin") == b"nuevo"
    assert api.calls_to("commit")[0][1] == 1  # la versión actual es la base


def test_w_closed_without_writes_publishes_an_empty_file(api):
    api.add_file("/docs/a.bin", [b"algo"])
    answer = DfsFile.open(api, "/docs/a.bin", "w").close()
    assert answer.version == 2
    assert content(api, "/docs/a.bin") == b""


# --- a: anexar ---


def test_a_appends_across_a_block_boundary_and_reserves_only_the_touched_blocks(api):
    base = os.urandom(MIB + MIB // 2)
    api.add_file("/docs/log.bin", blocks_of(base))
    extra = os.urandom(MIB)
    with DfsFile.open(api, "/docs/log.bin", "a") as file:
        file.write(0, extra)  # en a el offset no importa: va al final
    assert content(api, "/docs/log.bin") == base + extra
    assert reserved(api) == [1, 2]  # el bloque 0 no cambia
    assert api.calls_to("commit")[0][5] is None  # en a, file_sha256 null (tarea 12)


def test_a_on_a_missing_file_creates_it(api):
    with DfsFile.open(api, "/docs/nuevo.log", "a") as file:
        file.write(5, b"primera linea")
    assert content(api, "/docs/nuevo.log") == b"primera linea"


# --- rw: modificar partes ---


def test_rw_changes_the_middle_of_a_block_and_reserves_only_that_block(api):
    base = os.urandom(3 * MIB)
    api.add_file("/docs/a.bin", blocks_of(base))
    offset = MIB + MIB // 2
    with DfsFile.open(api, "/docs/a.bin", "rw") as file:
        file.write(offset, b"CAMBIO")
    expected = base[:offset] + b"CAMBIO" + base[offset + 6 :]
    assert content(api, "/docs/a.bin") == expected
    assert reserved(api) == [1]
    [commit] = api.calls_to("commit")
    assert commit[3] == [hashlib.sha256(block).hexdigest() for block in blocks_of(expected)]


def test_rw_can_grow_the_file_from_its_end(api):
    api.add_file("/docs/a.bin", [b"0123456789"])
    with DfsFile.open(api, "/docs/a.bin", "rw") as file:
        file.write(8, b"ABCD")
    assert content(api, "/docs/a.bin") == b"01234567ABCD"


def test_rw_without_changes_publishes_nothing_and_releases_the_lock(api):
    api.add_file("/docs/a.bin", [b"datos"])
    file = DfsFile.open(api, "/docs/a.bin", "rw")
    assert file.read(0, 5) == b"datos"
    assert file.close() is None
    assert "uploads" not in api.names()
    assert api.names()[-1] == "unlock"


def test_write_beyond_the_size_is_an_error(api):
    api.add_file("/docs/a.bin", [b"0123456789"])
    with DfsFile.open(api, "/docs/a.bin", "rw") as file:
        error = error_of(lambda: file.write(11, b"x"))
    assert error.code == ErrorCode.E_BAD_REQUEST
    assert "huecos" in error.message


def test_write_in_r_mode_is_an_error(api):
    api.add_file("/docs/a.bin", [b"datos"])
    with DfsFile.open(api, "/docs/a.bin", "r") as file:
        assert error_of(lambda: file.write(0, b"x")).code == ErrorCode.E_BAD_REQUEST


# --- read ---


def test_read_sees_its_own_unpublished_changes(api):
    api.add_file("/docs/a.bin", [b"0123456789"])
    with DfsFile.open(api, "/docs/a.bin", "rw") as file:
        file.write(2, b"XY")
        assert file.read(0, 10) == b"01XY456789"
        assert content(api, "/docs/a.bin") == b"0123456789"  # otro lector todavía ve lo publicado


def test_read_crosses_blocks_and_stops_at_the_end(api):
    data = os.urandom(2 * MIB + 10)
    api.add_file("/docs/a.bin", blocks_of(data))
    with DfsFile.open(api, "/docs/a.bin", "r") as file:
        assert file.read(MIB - 5, 10) == data[MIB - 5 : MIB + 5]
        assert file.read(2 * MIB, 100) == data[2 * MIB :]
        assert file.read(len(data), 10) == b""


def test_atomicity_and_close_to_open(api):
    old, new = os.urandom(2 * MIB), os.urandom(2 * MIB)
    api.add_file("/docs/a.bin", blocks_of(old))
    before = DfsFile.open(api, "/docs/a.bin", "r")  # abierto antes del close
    writer = DfsFile.open(api, "/docs/a.bin", "w")
    writer.write(0, new)
    assert before.read(0, 2 * MIB) == old  # nunca una mezcla (G2)
    writer.close()
    after = DfsFile.open(api, "/docs/a.bin", "r")  # abierto después del close
    assert before.read(0, 2 * MIB) == old  # sigue en su versión
    assert after.read(0, 2 * MIB) == new  # close-to-open (G3)
    assert (before.version, after.version) == (1, 2)


def test_r_keeps_its_version_while_another_client_publishes(api):
    old = os.urandom(2 * MIB)
    api.add_file("/docs/a.bin", blocks_of(old))
    reader = DfsFile.open(api, "/docs/a.bin", "r")
    assert reader.read(0, 10) == old[:10]  # solo bajó el bloque 0
    with DfsFile.open(api, "/docs/a.bin", "w") as writer:
        writer.write(0, b"otra cosa")
    assert reader.read(MIB, MIB) == old[MIB:]  # el bloque 1 sale de la versión fija


def test_a_slow_reader_whose_version_is_no_longer_retained_must_reopen(api):
    api.add_file("/docs/a.bin", [b"datos"])
    reader = DfsFile.open(api, "/docs/a.bin", "r")
    # El pase venció (401) y GET /files/blocks con la versión fija ya responde 404 (DA-14).
    api.on_get = lambda node_id, hash: DFSError(ErrorCode.E_AUTH, "pase vencido")
    api.fail_call["files_blocks"] = [DFSError(ErrorCode.E_NOT_FOUND, "esa versión ya no existe")]
    error = error_of(lambda: reader.read(0, 5))
    assert error.code == ErrorCode.E_NOT_FOUND
    assert "reabre el archivo" in error.message


# --- Lock ---


def test_writers_take_the_lock_and_read_the_base_after_it(api):
    api.add_file("/docs/a.bin", [b"datos"])
    DfsFile.open(api, "/docs/a.bin", "rw", wait_s=7).close()
    assert api.names()[:3] == ["lock", "stat", "files_blocks"]  # stat con el lock tomado
    assert api.calls_to("lock") == [("/docs/a.bin", 7)]


def test_a_second_writer_gets_423_while_the_first_one_has_the_file_open(api):
    first = DfsFile.open(api, "/docs/a.bin", "w")
    assert error_of(lambda: DfsFile.open(api, "/docs/a.bin", "w", wait_s=0)).code == ErrorCode.E_LOCKED
    first.close()
    DfsFile.open(api, "/docs/a.bin", "w", wait_s=0).close()  # ya se soltó


def test_opening_a_directory_for_writing_fails_and_releases_the_lock(api, monkeypatch):
    # FakeDfsApi responde 400 al pedir el lock de un directorio; aquí el directorio aparece en el
    # stat que va después del lock, como cuando otro cliente lo crea entre las dos llamadas.
    monkeypatch.setattr(api, "lock", lambda path, wait_s=30: FakeDfsApi.lock(api, "/docs/otro", wait_s))
    error = error_of(lambda: DfsFile.open(api, "/docs", "w"))
    assert error.code == ErrorCode.E_BAD_REQUEST
    assert api.names()[-1] == "unlock"


def test_lost_lock_fails_the_close_and_unlock_is_still_called(api):
    file = DfsFile.open(api, "/docs/a.bin", "w")
    file.write(0, b"datos")
    api.locks.clear()  # el lease venció en el MetaServer
    assert file.keeper.renew_once() is False  # la renovación responde 409
    error = error_of(file.close)
    assert (error.code, error.message) == (ErrorCode.E_LOCK_LOST, LOST_MESSAGE)
    assert "commit" not in api.names()
    assert api.names()[-1] == "unlock"


def test_without_renewal_a_late_commit_gets_409(api):
    file = DfsFile.open(api, "/docs/a.bin", "w", renew=False)
    file.write(0, b"datos")
    api.locks.clear()  # nadie lo renovó y venció (--no-renew)
    error = error_of(file.close)
    assert (error.code, error.message) == (ErrorCode.E_LOCK_LOST, LOST_MESSAGE)
    assert api.names()[-1] == "unlock"
    assert "/docs/a.bin" not in api.files  # no se publicó nada


def test_a_commit_against_a_newer_version_gets_409(api, monkeypatch):
    api.add_file("/docs/a.bin", [b"v1"])
    file = DfsFile.open(api, "/docs/a.bin", "rw")
    file.write(0, b"v2")
    api.add_file("/docs/a.bin", [b"otra"])  # alguien publicó sin respetar el lock
    error = error_of(file.close)
    assert (error.code, error.message) == (ErrorCode.E_VERSION_CONFLICT, LOST_MESSAGE)


# --- Context manager y archivo cerrado ---


def test_an_error_inside_with_discards_the_changes_and_releases_the_lock(api):
    with pytest.raises(RuntimeError):
        with DfsFile.open(api, "/docs/a.bin", "w") as file:
            file.write(0, b"a medias")
            raise RuntimeError("el programa falló")
    assert "commit" not in api.names()
    assert api.names()[-1] == "unlock"
    assert api.locks == {}


def test_a_closed_file_cannot_be_used(api):
    file = DfsFile.open(api, "/docs/a.bin", "w")
    file.close()
    assert error_of(lambda: file.read(0, 1)).code == ErrorCode.E_BAD_REQUEST
    assert error_of(file.close).code == ErrorCode.E_BAD_REQUEST


def test_an_invalid_mode_is_an_error(api):
    assert error_of(lambda: DfsFile.open(api, "/docs/a.bin", "x")).code == ErrorCode.E_BAD_REQUEST
    assert api.calls == []
