"""Pruebas de la tarea 08: ls, stat, mkdir, rmdir y rm (docs/contratos.md, secciones 2 y 8).

Usan FakeEtcd (tests/fakes.py). Los archivos se crean escribiendo directo su entrada en
/ns y su manifiesto (add_file), porque las subidas son de la tarea 12.
"""

import time

import pytest

from dfs_common.errors import DFSError, ErrorCode
from metaserver.etcd import EtcdError

from fakes import FakeEtcd, add_dir, add_file, bearer, public_client

LS, STAT, MKDIR, RMDIR, RM = (f"/api/v1/fs/{name}" for name in ("ls", "stat", "mkdir", "rmdir", "rm"))
FILE_ID = "3f0c9a7e-1b2d-4c5e-8f9a-0b1c2d3e4f50"


class RaceAroundTxn(FakeEtcd):
    """FakeEtcd en el que otro cliente actúa justo antes o justo después de la primera transacción.

    before prueba lo que pasa si algo cambia entre la lectura de la ruta y su transacción;
    after, lo que pasa si cambia otra vez antes de que se vuelva a leer.
    """

    def __init__(self, before=None, after=None) -> None:
        super().__init__()
        self.before, self.after, self.raced = before, after, False

    async def txn(self, compare, success, failure):
        if self.raced:
            return await super().txn(compare, success, failure)
        self.raced = True
        if self.before:
            self.before(self)
        result = await super().txn(compare, success, failure)
        if self.after:
            self.after(self)
        return result


def recreate_dir(etcd: FakeEtcd, path: str) -> None:
    """Otro cliente hace rmdir y mkdir de la misma ruta: queda igual, pero con otra mod_revision."""
    etcd.kvs.pop(f"/ns/ana{path}")
    add_dir(etcd, "ana", path)


def call(etcd: FakeEtcd, method: str, route: str, path: str, *, user: str = "ana", **settings):
    """Llama a una ruta /fs como user, con la ruta en la query o, en mkdir, en el cuerpo."""
    request = {"json": {"path": path}} if route == MKDIR else {"params": {"path": path}}
    with public_client(etcd, **settings) as client:
        return client.request(method, route, headers=bearer(user), **request)


def assert_error(response, status: int, code: str) -> None:
    assert response.status_code == status, response.json()
    assert response.json()["error"] == code


@pytest.mark.parametrize(
    ("method", "route"), [("GET", LS), ("GET", STAT), ("POST", MKDIR), ("DELETE", RMDIR), ("DELETE", RM)]
)
def test_every_fs_route_requires_a_jwt(method, route):
    request = {"json": {"path": "/docs"}} if route == MKDIR else {"params": {"path": "/docs"}}
    with public_client() as client:
        assert_error(client.request(method, route, **request), 401, "E_AUTH")


# --- ls y stat ---


def test_ls_shows_only_direct_children_sorted_by_name():
    etcd = FakeEtcd()
    add_dir(etcd, "ana", "/docs", created_at=1793178900000)
    add_dir(etcd, "ana", "/docs/fotos", created_at=1793178960000)
    add_dir(etcd, "ana", "/docs/fotos/2026")  # nieto: no aparece en ls /docs
    add_file(etcd, "ana", "/docs/informe.pdf", file_id=FILE_ID, version=4, size=20971520, created_at=1793628090000)
    add_file(etcd, "ana", "/docs/fotos/playa.jpg", file_id="otro-archivo", version=1)
    add_dir(etcd, "ana", "/zeta")
    add_dir(etcd, "ana", "/arte")

    docs = call(etcd, "GET", LS, "/docs")
    root = call(etcd, "GET", LS, "/")

    assert docs.status_code == 200
    assert docs.json() == {
        "entries": [
            {"name": "fotos", "type": "dir", "size": 0, "version": 0, "modified_at": 1793178960000},
            {"name": "informe.pdf", "type": "file", "size": 20971520, "version": 4, "modified_at": 1793628090000},
        ]
    }
    assert [entry["name"] for entry in root.json()["entries"]] == ["arte", "docs", "zeta"]


def test_ls_only_sees_the_users_own_space():
    etcd = FakeEtcd()
    add_dir(etcd, "ana", "/docs")
    add_dir(etcd, "anabel", "/secreto")  # su prefijo /ns/anabel/ empieza como /ns/ana
    add_dir(etcd, "bruno", "/docs/privado")

    assert [entry["name"] for entry in call(etcd, "GET", LS, "/").json()["entries"]] == ["docs"]
    assert call(etcd, "GET", LS, "/docs").json() == {"entries": []}
    assert_error(call(etcd, "GET", LS, "/secreto"), 404, "E_NOT_FOUND")


def test_ls_on_a_file_returns_only_that_file():
    etcd = FakeEtcd()
    add_file(etcd, "ana", "/docs/informe.pdf", version=4, size=20971520, created_at=1793628090000)
    response = call(etcd, "GET", LS, "/docs/informe.pdf")
    assert response.json() == {
        "entries": [
            {"name": "informe.pdf", "type": "file", "size": 20971520, "version": 4, "modified_at": 1793628090000}
        ]
    }


@pytest.mark.parametrize("route", [LS, STAT])
@pytest.mark.parametrize("path", ["/nada", "/docs/informe.pdf/dentro"], ids=["no-existe", "bajo-un-archivo"])
def test_ls_and_stat_of_a_missing_path_are_404(route, path):
    etcd = FakeEtcd()
    add_file(etcd, "ana", "/docs/informe.pdf")
    assert_error(call(etcd, "GET", route, path), 404, "E_NOT_FOUND")


def test_stat_of_a_file_and_of_a_directory_has_the_same_values_as_ls():
    etcd = FakeEtcd()
    add_dir(etcd, "ana", "/docs", created_at=1793178900000)
    add_file(etcd, "ana", "/docs/informe.pdf", version=4, size=20971520, created_at=1793628090000)

    assert call(etcd, "GET", STAT, "/docs/informe.pdf").json() == {
        "path": "/docs/informe.pdf",
        "type": "file",
        "size": 20971520,
        "version": 4,
        "modified_at": 1793628090000,
    }
    assert call(etcd, "GET", STAT, "/docs").json() == {
        "path": "/docs",
        "type": "dir",
        "size": 0,
        "version": 0,
        "modified_at": 1793178900000,
    }


# --- mkdir ---


def test_mkdir_creates_the_directory_with_its_creation_time():
    etcd = FakeEtcd()
    before = time.time_ns() // 1_000_000
    root_child = call(etcd, "POST", MKDIR, "/docs")
    nested = call(etcd, "POST", MKDIR, "/docs/fotos")
    after = time.time_ns() // 1_000_000

    assert root_child.json() == nested.json() == {"ok": True}
    for key in ("/ns/ana/docs", "/ns/ana/docs/fotos"):
        stored = etcd.value(key)
        assert stored["type"] == "dir"
        assert before <= stored["created_at"] <= after  # milisegundos desde epoch
    assert [entry["name"] for entry in call(etcd, "GET", LS, "/docs").json()["entries"]] == ["fotos"]


def test_mkdir_errors():
    etcd = FakeEtcd()
    add_dir(etcd, "ana", "/docs")
    add_file(etcd, "ana", "/docs/informe.pdf")

    assert_error(call(etcd, "POST", MKDIR, "/nada/fotos"), 404, "E_NOT_FOUND")  # sin padre
    assert_error(call(etcd, "POST", MKDIR, "/docs/informe.pdf/fotos"), 404, "E_NOT_FOUND")  # padre archivo
    assert_error(call(etcd, "POST", MKDIR, "/docs"), 409, "E_EXISTS")  # repetido
    assert_error(call(etcd, "POST", MKDIR, "/docs/informe.pdf"), 409, "E_EXISTS")  # ya es un archivo
    assert_error(call(etcd, "POST", MKDIR, "/"), 409, "E_EXISTS")  # la raíz existe siempre
    assert etcd.keys("/ns/") == ["/ns/ana/docs", "/ns/ana/docs/informe.pdf"]  # no creó nada


def test_mkdir_when_the_parent_is_removed_before_the_transaction_is_404():
    etcd = RaceAroundTxn(before=lambda etcd: etcd.kvs.pop("/ns/ana/docs"))  # otro cliente hace rmdir /docs
    add_dir(etcd, "ana", "/docs")
    assert_error(call(etcd, "POST", MKDIR, "/docs/fotos"), 404, "E_NOT_FOUND")
    assert etcd.keys("/ns/") == []


def test_mkdir_when_another_client_creates_it_first_is_409():
    etcd = RaceAroundTxn(before=lambda etcd: add_dir(etcd, "ana", "/docs/fotos"))
    add_dir(etcd, "ana", "/docs")
    assert_error(call(etcd, "POST", MKDIR, "/docs/fotos"), 409, "E_EXISTS")


def test_mkdir_when_it_existed_at_the_transaction_but_not_at_the_reread_is_409():
    # Otro cliente crea /docs/fotos antes de la transacción y lo borra antes de la relectura:
    # el padre no cambió, así que lo que falló fue "la ruta no existe".
    etcd = RaceAroundTxn(
        before=lambda etcd: add_dir(etcd, "ana", "/docs/fotos"),
        after=lambda etcd: etcd.kvs.pop("/ns/ana/docs/fotos"),
    )
    add_dir(etcd, "ana", "/docs")
    assert_error(call(etcd, "POST", MKDIR, "/docs/fotos"), 409, "E_EXISTS")


def test_mkdir_when_the_parent_is_recreated_before_the_transaction_is_404():
    etcd = RaceAroundTxn(before=lambda etcd: recreate_dir(etcd, "/docs"))
    add_dir(etcd, "ana", "/docs")
    assert_error(call(etcd, "POST", MKDIR, "/docs/fotos"), 404, "E_NOT_FOUND")
    assert etcd.keys("/ns/") == ["/ns/ana/docs"]


# --- rmdir ---


def test_rmdir_removes_an_empty_directory():
    etcd = FakeEtcd()
    add_dir(etcd, "ana", "/docs")
    add_dir(etcd, "ana", "/docs-viejos")  # nombre parecido: no cuenta como entrada de /docs
    assert call(etcd, "DELETE", RMDIR, "/docs").json() == {"ok": True}
    assert etcd.keys("/ns/") == ["/ns/ana/docs-viejos"]


def test_rmdir_errors():
    etcd = FakeEtcd()
    add_dir(etcd, "ana", "/docs")
    add_dir(etcd, "ana", "/docs/fotos")
    add_file(etcd, "ana", "/informe.pdf")

    assert_error(call(etcd, "DELETE", RMDIR, "/docs"), 409, "E_NOT_EMPTY")
    assert_error(call(etcd, "DELETE", RMDIR, "/informe.pdf"), 400, "E_BAD_REQUEST")  # es un archivo
    assert_error(call(etcd, "DELETE", RMDIR, "/"), 400, "E_BAD_REQUEST")  # la raíz
    assert_error(call(etcd, "DELETE", RMDIR, "/nada"), 404, "E_NOT_FOUND")
    assert etcd.keys("/ns/ana/docs") == ["/ns/ana/docs", "/ns/ana/docs/fotos"]  # no borró nada


def test_rmdir_when_the_directory_gets_an_entry_before_the_transaction_is_409():
    # Otro cliente crea /docs/fotos entre la lectura de /docs y la transacción del rmdir.
    etcd = RaceAroundTxn(before=lambda etcd: add_dir(etcd, "ana", "/docs/fotos"))
    add_dir(etcd, "ana", "/docs")
    assert_error(call(etcd, "DELETE", RMDIR, "/docs"), 409, "E_NOT_EMPTY")
    assert etcd.keys("/ns/") == ["/ns/ana/docs", "/ns/ana/docs/fotos"]


def test_rmdir_when_an_entry_appears_and_disappears_around_the_transaction_is_409():
    # Caso de la auditoría (H3): al hacer la transacción tenía una entrada; al releer, ya no.
    # El directorio no cambió, así que lo que falló fue "no tiene entradas": 409, no 404.
    etcd = RaceAroundTxn(
        before=lambda etcd: add_dir(etcd, "ana", "/docs/fotos"),
        after=lambda etcd: etcd.kvs.pop("/ns/ana/docs/fotos"),
    )
    add_dir(etcd, "ana", "/docs")
    assert_error(call(etcd, "DELETE", RMDIR, "/docs"), 409, "E_NOT_EMPTY")
    assert etcd.keys("/ns/") == ["/ns/ana/docs"]  # un nuevo rmdir sí lo borraría


def test_rmdir_when_the_directory_is_removed_before_the_transaction_is_404():
    etcd = RaceAroundTxn(before=lambda etcd: etcd.kvs.pop("/ns/ana/docs"))
    add_dir(etcd, "ana", "/docs")
    assert_error(call(etcd, "DELETE", RMDIR, "/docs"), 404, "E_NOT_FOUND")


def test_rmdir_when_the_directory_is_recreated_before_the_transaction_is_404():
    # El directorio que se leyó ya no existe: el que hay ahora es otro, con otra mod_revision.
    etcd = RaceAroundTxn(before=lambda etcd: recreate_dir(etcd, "/docs"))
    add_dir(etcd, "ana", "/docs")
    assert_error(call(etcd, "DELETE", RMDIR, "/docs"), 404, "E_NOT_FOUND")
    assert etcd.keys("/ns/") == ["/ns/ana/docs"]


# --- rm ---


def test_rm_removes_the_entry_retains_the_version_and_releases_the_lock():
    etcd = FakeEtcd()
    add_file(etcd, "ana", "/docs/informe.pdf", file_id=FILE_ID, version=4)
    manifest_before = etcd.value(f"/files/{FILE_ID}/v/4")
    before = time.time_ns() // 1_000_000

    response = call(etcd, "DELETE", RM, "/docs/informe.pdf", lock_lease_s=17)

    assert response.json() == {"ok": True}
    assert etcd.value("/ns/ana/docs/informe.pdf") is None
    manifest = etcd.value(f"/files/{FILE_ID}/v/4")
    assert before <= manifest["replaced_at"] <= time.time_ns() // 1_000_000
    assert {**manifest, "replaced_at": None} == manifest_before  # solo cambió replaced_at; user sigue ahí
    assert etcd.keys("/locks/") == []
    assert etcd.leases == set()  # el lease del intento se revocó


def test_rm_when_another_client_has_the_lock_is_423():
    etcd = FakeEtcd()
    add_file(etcd, "ana", "/docs/informe.pdf", file_id=FILE_ID, version=4)
    writer = 0x7A00000000000001  # un escritor que tomó el lock antes (POST /locks)
    etcd.leases.add(writer)
    etcd.seed(f"/locks/ana/docs/informe.pdf/{writer:x}", b"", lease=writer)

    assert_error(call(etcd, "DELETE", RM, "/docs/informe.pdf"), 423, "E_LOCKED")
    assert etcd.value("/ns/ana/docs/informe.pdf") is not None
    assert etcd.value(f"/files/{FILE_ID}/v/4")["replaced_at"] is None
    assert etcd.keys("/locks/") == [f"/locks/ana/docs/informe.pdf/{writer:x}"]  # el intento de rm salió de la fila
    assert etcd.leases == {writer}


def test_rm_when_the_file_changes_before_the_transaction_is_423():
    # Un escritor publica la versión 5 entre la lectura de rm y su transacción.
    def publish_version_5(etcd):
        etcd.seed("/ns/ana/docs/informe.pdf", {"type": "file", "file_id": FILE_ID, "version": 5})

    etcd = RaceAroundTxn(before=publish_version_5)
    add_file(etcd, "ana", "/docs/informe.pdf", file_id=FILE_ID, version=4)
    assert_error(call(etcd, "DELETE", RM, "/docs/informe.pdf"), 423, "E_LOCKED")
    assert etcd.value("/ns/ana/docs/informe.pdf")["version"] == 5
    assert etcd.value(f"/files/{FILE_ID}/v/4")["replaced_at"] is None
    assert etcd.keys("/locks/") == []
    assert etcd.leases == set()


REVOKE_FAILURES = [
    DFSError(ErrorCode.E_UNAVAILABLE, "ningún nodo de etcd respondió"),
    EtcdError("etcdserver: request timed out"),
]


class RevokeFails(FakeEtcd):
    """FakeEtcd en el que revocar un lease falla, como si etcd dejara de responder justo en ese momento."""

    def __init__(self, failure: Exception) -> None:
        super().__init__()
        self.failure = failure

    async def lease_revoke(self, lease_id):
        raise self.failure


@pytest.mark.parametrize("failure", REVOKE_FAILURES, ids=["503", "error-de-etcd"])
def test_rm_that_already_deleted_answers_200_even_if_releasing_the_lock_fails(failure, caplog):
    etcd = RevokeFails(failure)
    add_file(etcd, "ana", "/docs/informe.pdf", file_id=FILE_ID, version=4)

    assert call(etcd, "DELETE", RM, "/docs/informe.pdf").json() == {"ok": True}
    assert etcd.value("/ns/ana/docs/informe.pdf") is None
    assert etcd.value(f"/files/{FILE_ID}/v/4")["replaced_at"] is not None
    assert "vencerá solo" in caplog.text  # el lease no se revocó: vence en DFS_LOCK_LEASE_S


@pytest.mark.parametrize("failure", REVOKE_FAILURES, ids=["503", "error-de-etcd"])
def test_rm_keeps_its_423_even_if_releasing_the_lock_fails(failure):
    etcd = RevokeFails(failure)
    add_file(etcd, "ana", "/docs/informe.pdf")
    writer = 0x7A00000000000001
    etcd.leases.add(writer)
    etcd.seed(f"/locks/ana/docs/informe.pdf/{writer:x}", b"", lease=writer)
    assert_error(call(etcd, "DELETE", RM, "/docs/informe.pdf"), 423, "E_LOCKED")


def test_rm_errors():
    etcd = FakeEtcd()
    add_dir(etcd, "ana", "/docs")
    assert_error(call(etcd, "DELETE", RM, "/docs"), 400, "E_BAD_REQUEST")  # es un directorio
    assert_error(call(etcd, "DELETE", RM, "/"), 400, "E_BAD_REQUEST")  # la raíz
    assert_error(call(etcd, "DELETE", RM, "/nada.txt"), 404, "E_NOT_FOUND")
    assert etcd.leases == set()  # ninguno de estos pidió lease
