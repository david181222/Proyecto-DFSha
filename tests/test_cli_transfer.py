"""Pruebas de dfs put y dfs get (tarea 17), con el CliRunner de typer y FakeDfsApi.

DFS_BLOCK_MIB=1 en la API falsa: un archivo de 3,5 MiB son 4 bloques.
"""

import hashlib
import os

import pytest
from fakes_client import FakeDfsApi
from typer.testing import CliRunner

from dfs_client import cli, session
from dfs_client.cli import app
from dfs_client.session import Session
from dfs_common.errors import DFSError, ErrorCode

MIB = 1024 * 1024


@pytest.fixture
def api(monkeypatch, tmp_path):
    fake = FakeDfsApi()
    fake.dirs.add("/docs")
    monkeypatch.setattr(session, "SESSION_FILE", tmp_path / ".dfs" / "session.json")
    monkeypatch.setattr(cli, "make_api", lambda settings, token: fake)
    session.save(Session(user="ana", token="jwt", cwd="/docs"))
    return fake


def run(*args):
    return CliRunner().invoke(app, [str(arg) for arg in args])


def local_file(tmp_path, data: bytes, name: str = "local.bin"):
    path = tmp_path / name
    path.write_bytes(data)
    return path


# --- put ---


def test_put_and_get_a_file_of_three_and_a_half_blocks(api, tmp_path):
    data = os.urandom(3 * MIB + MIB // 2)
    result = run("put", local_file(tmp_path, data), "informe.bin")
    assert result.exit_code == 0, result.output
    assert "/docs/informe.bin" in result.output and "versión 1" in result.output
    assert "4 enviados, 0 ya estaban" in result.output
    assert "MiB/s" in result.output
    [commit] = api.calls_to("commit")
    assert commit[3:] == ([hashlib.sha256(data[i : i + MIB]).hexdigest() for i in range(0, len(data), MIB)],
                          len(data), hashlib.sha256(data).hexdigest())
    assert api.names()[:2] == ["lock", "stat"]  # stat después de tener el lock
    assert api.names()[-1] == "unlock"

    target = tmp_path / "bajado.bin"
    result = run("get", "informe.bin", target)
    assert result.exit_code == 0, result.output
    assert target.read_bytes() == data
    assert "versión 1" in result.output and "MiB/s" in result.output


def test_put_and_get_an_empty_file(api, tmp_path):
    result = run("put", local_file(tmp_path, b""), "vacio")
    assert result.exit_code == 0, result.output
    [commit] = api.calls_to("commit")
    assert commit[3:5] == ([], 0)
    result = run("get", "vacio", tmp_path / "vacio-bajado")
    assert result.exit_code == 0, result.output
    assert (tmp_path / "vacio-bajado").read_bytes() == b""


def test_put_over_an_existing_file_uses_its_version_as_base(api, tmp_path):
    api.add_file("/docs/informe.bin", [b"version 1"])
    result = run("put", local_file(tmp_path, b"version 2"), "informe.bin")
    assert result.exit_code == 0, result.output
    assert api.calls_to("commit")[0][1] == 1  # base_version
    assert "versión 2" in result.output


def test_a_block_already_stored_is_counted_as_present(api, tmp_path):
    api.add_file("/docs/otro.bin", [b"igual"])
    result = run("put", local_file(tmp_path, b"igual"), "copia.bin")
    assert "0 enviados, 1 ya estaban" in result.output


def test_put_sends_wait_to_the_lock(api, tmp_path):
    run("put", local_file(tmp_path, b"x"), "a.bin", "--wait", 5)
    assert api.calls_to("lock") == [("/docs/a.bin", 5)]


def test_put_to_a_directory_fails(api, tmp_path):
    api.dirs.add("/docs/fotos")
    result = run("put", local_file(tmp_path, b"x"), "fotos")
    assert result.exit_code == 1
    assert "error E_BAD_REQUEST: /docs/fotos es un directorio" in result.output  # lo responde POST /locks
    assert api.calls_to("uploads") == []


def test_a_directory_seen_by_stat_after_the_lock_fails_and_releases_the_lock(api, tmp_path):
    def directory_appears():
        api.dirs.add("/docs/nuevo")  # stat, ya con el lock tomado, ve un directorio
        return None

    api.fail_call["stat"] = [directory_appears]
    result = run("put", local_file(tmp_path, b"x"), "nuevo")
    assert result.exit_code == 1
    assert "error E_BAD_REQUEST: /docs/nuevo es un directorio" in result.output
    assert api.calls_to("uploads") == []
    assert len(api.calls_to("unlock")) == 1


def test_three_failures_fail_the_put_and_the_lock_is_still_released(api, tmp_path):
    api.on_put = lambda node_id, hash, data: DFSError(ErrorCode.E_UNAVAILABLE, "el DataNode no respondió")
    result = run("put", local_file(tmp_path, b"datos"), "a.bin")
    assert result.exit_code == 1
    assert "error E_UNAVAILABLE: el DataNode no respondió" in result.output
    assert len(api.calls_to("put_block")) == 3
    assert api.names()[-1] == "unlock"


def test_a_lost_lock_fails_the_put_and_unlock_is_still_called(api, tmp_path):
    def expire_lock(node_id, hash, data):
        api.locks.clear()  # el lease venció mientras se enviaba
        return None

    api.on_put = expire_lock
    result = run("put", local_file(tmp_path, b"datos"), "a.bin")
    assert result.exit_code == 1
    assert "E_LOCK_LOST" in result.output
    assert api.names()[-1] == "unlock"


def test_put_of_a_locked_file_says_so(api, tmp_path):
    api.fail_call["lock"] = [DFSError(ErrorCode.E_LOCKED, "lock ocupado")]
    result = run("put", local_file(tmp_path, b"x"), "a.bin")
    assert result.exit_code == 1
    assert "El archivo está bloqueado por otro cliente; intenta más tarde" in result.output
    assert api.calls_to("unlock") == []


def test_put_of_a_local_file_that_does_not_exist(api, tmp_path):
    result = run("put", tmp_path / "no-existe", "a.bin")
    assert result.exit_code == 1
    assert "no es un archivo" in result.output
    assert api.calls == []


def test_an_unlock_that_fails_does_not_hide_the_result(api, tmp_path):
    api.fail_call["unlock"] = [DFSError(ErrorCode.E_UNAVAILABLE, "etcd sin mayoría")]
    result = run("put", local_file(tmp_path, b"x"), "a.bin")
    assert result.exit_code == 0, result.output
    assert "versión 1" in result.output


# --- get ---


def test_get_of_an_old_version(api, tmp_path):
    api.add_file("/docs/a.bin", [b"version 1"])
    api.add_file("/docs/a.bin", [b"version 2"])
    result = run("get", "a.bin", tmp_path / "a.bin", "--version", 1)
    assert result.exit_code == 0, result.output
    assert api.calls_to("files_blocks") == [("/docs/a.bin", 1)]
    assert (tmp_path / "a.bin").read_bytes() == b"version 1"


def test_get_into_a_directory_keeps_the_remote_name(api, tmp_path):
    api.add_file("/docs/a.bin", [b"contenido"])
    result = run("get", "a.bin", tmp_path)
    assert result.exit_code == 0, result.output
    assert (tmp_path / "a.bin").read_bytes() == b"contenido"


def test_get_of_a_file_that_does_not_exist(api, tmp_path):
    result = run("get", "nada.bin", tmp_path / "x")
    assert result.exit_code == 1
    assert "error E_NOT_FOUND" in result.output
    assert not (tmp_path / "x").exists()


def test_get_and_put_need_a_session(api, tmp_path):
    session.clear()
    assert "Tu sesión no existe o venció" in run("get", "a.bin", tmp_path / "x").output
    assert "Tu sesión no existe o venció" in run("put", local_file(tmp_path, b"x"), "a.bin").output
    assert api.calls == []
