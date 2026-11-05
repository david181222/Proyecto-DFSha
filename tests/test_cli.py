"""Pruebas de los comandos de directorio de dfs (tarea 16), con el CliRunner de typer y una DfsApi falsa.

La API falsa responde los ejemplos del contrato y anota cada llamada; errors[metodo] hace
que ese método lance un DFSError. La sesión vive en tmp_path (monkeypatch de SESSION_FILE).
"""

import pytest
from typer.testing import CliRunner

from dfs_client import cli, session
from dfs_client.cli import app, format_time
from dfs_client.session import Session
from dfs_common.errors import DFSError, ErrorCode
from dfs_common.models import AdminStatusResponse, FileBlocksResponse, LoginResponse, LsResponse, OkResponse, StatResponse

SESSION_MESSAGE = "Tu sesión no existe o venció: corre dfs login"


def example(model, n: int = 0):
    return model.model_validate(model.model_config["json_schema_extra"]["examples"][n])


class FakeApi:
    """Lo mínimo de DfsApi que usan estos comandos."""

    def __init__(self):
        self.calls: list[tuple] = []
        self.errors: dict[str, DFSError] = {}
        self.dirs = {"/", "/docs", "/docs/fotos"}

    def _call(self, name, *args):
        self.calls.append((name, *args))
        if name in self.errors:
            raise self.errors[name]

    def login(self, user, password):
        self._call("login", user, password)
        return LoginResponse(token=f"jwt-de-{user}", expires_in=3600)

    def ls(self, path):
        self._call("ls", path)
        return example(LsResponse)

    def stat(self, path):
        self._call("stat", path)
        if path in self.dirs:
            return StatResponse(path=path, type="dir", size=0, version=0, modified_at=0 if path == "/" else 1793178900000)
        return StatResponse(path=path, type="file", size=20971520, version=4, modified_at=1793628090000)

    def files_blocks(self, path, version=None):
        self._call("files_blocks", path, version)
        return example(FileBlocksResponse)

    def mkdir(self, path):
        self._call("mkdir", path)
        return OkResponse(ok=True)

    def rmdir(self, path):
        self._call("rmdir", path)
        return OkResponse(ok=True)

    def rm(self, path):
        self._call("rm", path)
        return OkResponse(ok=True)

    def add_user(self, user, password):
        self._call("add_user", user, password)
        return OkResponse(ok=True)

    def admin_status(self):
        self._call("admin_status")
        return example(AdminStatusResponse)


@pytest.fixture
def api(monkeypatch, tmp_path):
    fake = FakeApi()
    fake.tokens = []  # el token con que se creó cada DfsApi
    monkeypatch.setattr(session, "SESSION_FILE", tmp_path / ".dfs" / "session.json")

    def make_api(settings, token):
        fake.tokens.append(token)
        return fake

    monkeypatch.setattr(cli, "make_api", make_api)
    return fake


@pytest.fixture
def logged_in(api):
    session.save(Session(user="ana", token="jwt-de-ana", cwd="/docs"))
    return api


def run(*args, input=None):
    return CliRunner().invoke(app, list(args), input=input)


# --- login, logout y la sesión ---


def test_login_asks_the_password_without_showing_it_and_saves_the_session(api):
    result = run("login", "--user", "ana", input="clave-secreta\n")
    assert result.exit_code == 0, result.output
    assert api.calls == [("login", "ana", "clave-secreta")]
    assert api.tokens == [None]
    assert "clave-secreta" not in result.output
    assert session.load() == Session(user="ana", token="jwt-de-ana", cwd="/")


def test_login_without_user_asks_for_it(api):
    result = run("login", input="ana\nclave\n")
    assert result.exit_code == 0, result.output
    assert api.calls == [("login", "ana", "clave")]


def test_login_resets_the_directory(logged_in):
    run("login", "--user", "ana", input="clave\n")
    assert session.load().cwd == "/"


def test_wrong_password_shows_the_server_error_and_saves_nothing(api):
    api.errors["login"] = DFSError(ErrorCode.E_AUTH, "usuario o contraseña incorrectos")
    result = run("login", "--user", "ana", input="mala\n")
    assert result.exit_code == 1
    assert "error E_AUTH: usuario o contraseña incorrectos" in result.output
    assert SESSION_MESSAGE not in result.output
    assert session.load() is None


def test_logout_deletes_the_session(logged_in):
    result = run("logout")
    assert result.exit_code == 0
    assert session.load() is None


def test_commands_use_the_saved_token(logged_in):
    run("ls")
    assert logged_in.tokens == ["jwt-de-ana"]


@pytest.mark.parametrize("args", [("ls",), ("pwd",), ("cd", "/docs"), ("mkdir", "a"), ("status",), ("stat", "a")])
def test_without_session_it_asks_to_log_in(api, args):
    result = run(*args)
    assert result.exit_code == 1
    assert SESSION_MESSAGE in result.output
    assert api.calls == []


@pytest.mark.parametrize("method, args", [("ls", ("ls",)), ("mkdir", ("mkdir", "a")), ("stat", ("cd", "fotos"))])
def test_a_401_asks_to_log_in_again(logged_in, method, args):
    logged_in.errors[method] = DFSError(ErrorCode.E_AUTH, "JWT ausente, inválido o vencido")
    result = run(*args)
    assert result.exit_code == 1
    assert SESSION_MESSAGE in result.output


# --- pwd y cd ---


def test_pwd_shows_the_directory(logged_in):
    result = run("pwd")
    assert (result.exit_code, result.output) == (0, "/docs\n")


def test_cd_to_a_directory_changes_it(logged_in):
    result = run("cd", "fotos")
    assert result.exit_code == 0, result.output
    assert logged_in.calls == [("stat", "/docs/fotos")]
    assert session.load().cwd == "/docs/fotos"


def test_cd_with_dot_dot(logged_in):
    run("cd", "..")
    assert session.load().cwd == "/"


def test_cd_without_path_goes_to_the_root(logged_in):
    result = run("cd")
    assert result.exit_code == 0
    assert session.load().cwd == "/"


def test_cd_to_a_file_fails_and_keeps_the_directory(logged_in):
    result = run("cd", "informe.pdf")
    assert result.exit_code == 1
    assert "error E_BAD_REQUEST: /docs/informe.pdf no es un directorio" in result.output
    assert session.load().cwd == "/docs"


def test_cd_to_a_path_that_does_not_exist(logged_in):
    logged_in.errors["stat"] = DFSError(ErrorCode.E_NOT_FOUND, "la ruta no existe")
    result = run("cd", "nada")
    assert result.exit_code == 1
    assert "error E_NOT_FOUND: la ruta no existe" in result.output
    assert session.load().cwd == "/docs"


# --- ls y stat ---


def test_ls_shows_type_size_version_date_and_name(logged_in):
    result = run("ls")
    assert result.exit_code == 0, result.output
    assert logged_in.calls == [("ls", "/docs")]
    lines = result.output.splitlines()
    assert lines[0].split() == ["d", "0", "0", *format_time(1793178900000).split(), "fotos"]
    assert lines[1].split() == ["-", "20971520", "4", *format_time(1793628090000).split(), "informe.pdf"]


def test_ls_with_a_relative_path(logged_in):
    run("ls", "../otra")
    assert logged_in.calls == [("ls", "/otra")]


def test_stat_shows_the_file(logged_in):
    result = run("stat", "informe.pdf")
    assert result.exit_code == 0, result.output
    assert logged_in.calls == [("stat", "/docs/informe.pdf")]
    assert "/docs/informe.pdf" in result.output
    assert "archivo" in result.output
    assert "20971520" in result.output
    assert format_time(1793628090000) in result.output


def test_stat_of_the_root_has_no_date(logged_in):
    result = run("stat", "/")
    assert "directorio" in result.output
    assert format_time(0) == "-"


def test_stat_blocks_shows_each_block_with_its_nodes(logged_in):
    result = run("stat", "informe.pdf", "--blocks")
    assert result.exit_code == 0, result.output
    assert logged_in.calls == [("files_blocks", "/docs/informe.pdf", None)]
    rows = [line.split() for line in result.output.splitlines()]
    assert ["0", "6ce5a55b446b", "8388608", "dn2,", "dn3,", "dn4"] in rows
    assert ["2", "8de70b36bbfa", "4194304", "dn1,", "dn2,", "dn3"] in rows
    assert "versión 4" in result.output


def test_stat_blocks_marks_the_nodes_that_are_down(logged_in, monkeypatch):
    blocks = example(FileBlocksResponse)
    blocks.blocks[0].replicas = [r for r in blocks.blocks[0].replicas if r.node_id != "dn4"]
    monkeypatch.setattr(logged_in, "files_blocks", lambda path, version=None: blocks)
    result = run("stat", "informe.pdf", "--blocks")
    assert "dn2, dn3, dn4 (caído)" in result.output


# --- mkdir, rmdir y rm ---


@pytest.mark.parametrize("command", ["mkdir", "rmdir", "rm"])
def test_mkdir_rmdir_and_rm_work_on_the_resolved_path(logged_in, command):
    result = run(command, "fotos/viejas")
    assert result.exit_code == 0, result.output
    assert logged_in.calls == [(command, "/docs/fotos/viejas")]


def test_errors_show_code_and_message(logged_in):
    logged_in.errors["mkdir"] = DFSError(ErrorCode.E_EXISTS, "la ruta ya existe")
    result = run("mkdir", "fotos")
    assert result.exit_code == 1
    assert "error E_EXISTS: la ruta ya existe" in result.output


def test_rm_of_a_locked_file_says_so(logged_in):
    logged_in.errors["rm"] = DFSError(ErrorCode.E_LOCKED, "lock ocupado y la espera se agotó o era 0")
    result = run("rm", "informe.pdf")
    assert result.exit_code == 1
    assert "El archivo está bloqueado por otro cliente; intenta más tarde" in result.output


# --- useradd y status (admin) ---


def test_useradd_asks_the_new_password_twice(logged_in):
    result = run("useradd", "bruno", input="clave-nueva\nclave-nueva\n")
    assert result.exit_code == 0, result.output
    assert logged_in.calls == [("add_user", "bruno", "clave-nueva")]
    assert "clave-nueva" not in result.output


def test_useradd_without_admin_role(logged_in):
    logged_in.errors["add_user"] = DFSError(ErrorCode.E_FORBIDDEN, "hace falta el rol de administrador")
    result = run("useradd", "bruno", input="x\nx\n")
    assert result.exit_code == 1
    assert "error E_FORBIDDEN: hace falta el rol de administrador" in result.output


def test_status_shows_leader_nodes_copies_and_orders(logged_in):
    result = run("status")
    assert result.exit_code == 0, result.output
    out = result.output
    assert "meta2" in out.splitlines()[0]
    lines = {line.split()[0]: line.split() for line in out.splitlines() if line.strip()}
    assert lines["dn1"][:3] == ["dn1", "vivo", "us-east-1a"]
    assert lines["dn4"][:2] == ["dn4", "caído"]
    assert "6ce5a55b446b" in out and "dn2, dn3" in out  # bloque con 2 copias vivas
    assert "copy" in out and "dn1" in out  # orden pendiente
    assert "172.31.16.10" in out  # un MetaServer
