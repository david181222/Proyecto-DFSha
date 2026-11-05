"""Pruebas de LockKeeper (tarea 17): renueva el lock mientras se escribe y avisa si se perdió (DA-08)."""

import time

import pytest
from fakes_client import FakeDfsApi

from dfs_client.locks import LockKeeper
from dfs_common.config import Settings
from dfs_common.errors import DFSError, ErrorCode


@pytest.fixture
def api():
    return FakeDfsApi(Settings(_env_file=None, lock_renew_s=1))


def test_a_409_marks_the_lock_as_lost(api):
    keeper = LockKeeper(api, "1f")  # el fake no tiene ese lock: renovarlo responde 409
    assert keeper.renew_once() is False
    assert keeper.lost
    with pytest.raises(DFSError) as caught:
        keeper.check()
    assert caught.value.code == ErrorCode.E_LOCK_LOST


@pytest.mark.parametrize("code", [ErrorCode.E_UNAVAILABLE, ErrorCode.E_AUTH])
def test_any_error_other_than_409_does_not_stop_it(api, code):
    """503 o error de conexión (E_UNAVAILABLE) y también un 401: solo el 409 dice que se perdió."""
    lock_id = api.lock("/a").lock_id
    api.fail_call["renew_lock"] = [DFSError(code)]
    keeper = LockKeeper(api, lock_id)
    assert keeper.renew_once() is True  # sigue en la siguiente vuelta: el lease puede seguir vivo
    assert not keeper.lost
    keeper.check()
    assert keeper.renew_once() is True
    assert api.calls_to("renew_lock") == [(lock_id,), (lock_id,)]


def test_the_thread_renews_every_dfs_lock_renew_s_and_stops(api):
    lock_id = api.lock("/a").lock_id
    with LockKeeper(api, lock_id):
        time.sleep(1.3)
    renewals = len(api.calls_to("renew_lock"))
    assert renewals == 1
    time.sleep(1.1)
    assert len(api.calls_to("renew_lock")) == renewals  # al salir del with, dejó de renovar


def test_the_thread_stops_by_itself_when_the_lock_is_lost(api):
    keeper = LockKeeper(api, "1f")
    with keeper:
        time.sleep(1.3)
        assert keeper.lost
        assert not keeper._thread.is_alive()


def test_without_renew_it_never_renews(api):
    lock_id = api.lock("/a").lock_id
    with LockKeeper(api, lock_id, renew=False) as keeper:
        time.sleep(1.2)
        keeper.check()
    assert api.calls_to("renew_lock") == []
