"""Pruebas de la sesión del cliente dfs (tarea 16): resolve y el archivo ~/.dfs/session.json."""

import json
import os
import stat

import pytest

from dfs_client import session
from dfs_client.session import Session


@pytest.fixture
def session_file(tmp_path, monkeypatch):
    path = tmp_path / "home" / ".dfs" / "session.json"
    monkeypatch.setattr(session, "SESSION_FILE", path)
    return path


# --- resolve ---


@pytest.mark.parametrize(
    "cwd, path, expected",
    [
        ("/docs", "informe.pdf", "/docs/informe.pdf"),
        ("/", "docs", "/docs"),
        ("/docs/fotos", "../informe.pdf", "/docs/informe.pdf"),
        ("/docs", "/otra/ruta", "/otra/ruta"),
        ("/docs", ".", "/docs"),
        ("/docs", "./a/./b", "/docs/a/b"),
        ("/docs", "..", "/"),
        ("/docs/fotos", "../..", "/"),
        # Más allá de la raíz se queda en la raíz, como cd .. en / de Linux.
        ("/docs", "../../../..", "/"),
        ("/", "..", "/"),
        ("/docs", "../../x", "/x"),
        ("/docs", "/a/../b", "/b"),
        # // se junta y la / final se quita.
        ("/docs", "a//b/", "/docs/a/b"),
        ("/", "/", "/"),
    ],
)
def test_resolve(cwd, path, expected):
    assert Session(user="ana", token="t", cwd=cwd).resolve(path) == expected


# --- Archivo de la sesión ---


def test_save_load_and_clear(session_file):
    assert session.load() is None
    session.save(Session(user="ana", token="jwt", cwd="/docs"))
    assert json.loads(session_file.read_text(encoding="utf-8")) == {"user": "ana", "token": "jwt", "cwd": "/docs"}
    assert session.load() == Session(user="ana", token="jwt", cwd="/docs")
    session.clear()
    assert not session_file.exists()
    assert session.load() is None


def test_clear_without_session_does_nothing(session_file):
    session.clear()
    assert not session_file.exists()


def test_a_damaged_session_counts_as_no_session(session_file):
    session_file.parent.mkdir(parents=True)
    session_file.write_text("{no es json", encoding="utf-8")
    assert session.load() is None


@pytest.mark.skipif(os.name != "posix", reason="los permisos 600 solo existen en POSIX")
def test_the_session_file_is_private(session_file):
    session.save(Session(user="ana", token="jwt", cwd="/"))
    assert stat.S_IMODE(session_file.stat().st_mode) == 0o600
    # Si ya existía con otros permisos, también queda en 600.
    session_file.chmod(0o644)
    session.save(Session(user="ana", token="otro", cwd="/"))
    assert stat.S_IMODE(session_file.stat().st_mode) == 0o600
