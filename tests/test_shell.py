"""Pruebas de la tarea 18: dfs shell (src/dfs_client/shell.py), llamando a los comandos del Cmd.

Usan FakeDfsApi (tests/fakes_client.py). El último bloque prueba el comando dfs shell de la CLI.
"""

import io

import pytest
from fakes_client import FakeDfsApi
from typer.testing import CliRunner

from dfs_client import cli, session
from dfs_client.cli import app
from dfs_client.session import Session
from dfs_client.shell import DfsShell


@pytest.fixture
def api():
    fake = FakeDfsApi()
    fake.dirs.add("/docs")
    return fake


def shell_for(api, cwd: str = "/docs") -> DfsShell:
    return DfsShell(api, Session(user="ana", token="jwt", cwd=cwd), stdout=io.StringIO())


def run(shell: DfsShell, *lines: str) -> list[str]:
    """Corre los comandos y devuelve las líneas que mostró el shell."""
    start = shell.stdout.tell()
    for line in lines:
        shell.onecmd(line)
    shell.stdout.seek(start)
    return shell.stdout.read().splitlines()


def test_full_round_open_write_read_close(api):
    shell = shell_for(api)
    assert run(shell, "open a.txt w") == ["fd 3: /docs/a.txt en modo w, versión 0, 0 bytes"]
    assert run(shell, "write 3 0 hola mundo") == ["10 bytes escritos"]
    assert run(shell, "read 3 0 100") == ["hola mundo"]
    assert run(shell, "files") == ["fd 3: /docs/a.txt modo w versión 0 10 bytes, 1 bloques sin publicar"]
    assert run(shell, "close 3") == ["fd 3 cerrado: /docs/a.txt versión 1"]
    assert run(shell, "files") == ["no hay archivos abiertos"]

    # Acceso RF3: otro shell modifica una parte y el primero ve la versión nueva al reabrir.
    other = shell_for(api)
    run(other, "open /docs/a.txt rw", "write 3 5 MUNDO", "close 3")
    assert run(shell, "open a.txt r", "read 4 0 100") == [
        "fd 4: /docs/a.txt en modo r, versión 2, 10 bytes",
        "hola MUNDO",
    ]


def test_writefile_and_read_to_a_local_file(api, tmp_path):
    local = tmp_path / "origen.bin"
    local.write_bytes(b"\x00\x01contenido binario")
    target = tmp_path / "copia.bin"
    shell = shell_for(api)
    run(shell, "open b.bin w", f"writefile 3 0 {local}", "close 3", "open b.bin r")
    assert run(shell, f"read 4 0 1000 --to {target}") == [f"19 bytes en {target}"]
    assert target.read_bytes() == local.read_bytes()


def test_wait_and_no_renew_options_reach_the_file(api):
    shell = shell_for(api)
    run(shell, "open a.txt w --wait 0 --no-renew")
    assert api.calls_to("lock") == [("/docs/a.txt", 0)]
    assert shell.files[3].keeper.renew is False


def test_a_second_writer_gets_the_locked_error_and_the_shell_goes_on(api):
    first, second = shell_for(api), shell_for(api)
    run(first, "open a.txt w")
    [line] = run(second, "open a.txt w --wait 0")
    assert line.startswith("error E_LOCKED:")
    assert run(second, "files") == ["no hay archivos abiertos"]


def test_errors_are_shown_and_the_shell_goes_on(api):
    shell = shell_for(api)
    assert run(shell, "read 9 0 10") == ["error E_BAD_REQUEST: fd 9 no está abierto"]
    assert run(shell, "open a.txt") == ["error: argumentos inválidos; mira help open"]
    run(shell, "open a.txt w")
    assert run(shell, "write 3 5 hueco")[0].startswith("error E_BAD_REQUEST: offset 5 fuera de 0..0")


def test_close_reports_when_nothing_was_published(api):
    api.add_file("/docs/a.txt", [b"datos"])
    shell = shell_for(api)
    run(shell, "open a.txt rw")
    assert run(shell, "close 3") == ["fd 3 cerrado sin publicar (no hubo cambios)"]


def test_quit_discards_open_files_and_releases_their_locks(api):
    shell = shell_for(api)
    run(shell, "open a.txt w", "write 3 0 sin publicar")
    assert shell.onecmd("quit") is True
    assert "commit" not in api.names()
    assert api.locks == {}
    assert shell.files == {}


def test_help_of_open_explains_no_renew():
    assert "solo para la prueba de concurrencia" in DfsShell.do_open.__doc__


# --- dfs shell en la CLI ---


def test_dfs_shell_runs_the_commands_from_its_input(api, monkeypatch, tmp_path):
    monkeypatch.setattr(session, "SESSION_FILE", tmp_path / ".dfs" / "session.json")
    monkeypatch.setattr(cli, "make_api", lambda settings, token: api)
    session.save(Session(user="ana", token="jwt", cwd="/docs"))
    commands = "open a.txt w\nwrite 3 0 desde la cli\nclose 3\nquit\n"
    result = CliRunner().invoke(app, ["shell"], input=commands)
    assert result.exit_code == 0, result.output
    assert "fd 3 cerrado: /docs/a.txt versión 1" in result.output
    assert api.files["/docs/a.txt"][-1]["size"] == len("desde la cli")


def test_dfs_shell_without_a_session_asks_to_log_in(monkeypatch, tmp_path):
    monkeypatch.setattr(session, "SESSION_FILE", tmp_path / ".dfs" / "session.json")
    result = CliRunner().invoke(app, ["shell"])
    assert result.exit_code == 1
    assert cli.SESSION_MESSAGE in result.output
