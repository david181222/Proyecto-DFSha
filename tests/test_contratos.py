"""Pruebas del contrato: rutas, ejemplos, errores, stubs, OpenAPI exportado y configuración.

Las últimas secciones prueban las decisiones de las tareas 02, 02b y 02c, que están
explicadas en docs/contratos.md; cada prueba dice qué decisión cubre.
"""

import base64
import importlib
import inspect
import json
import os
import re
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel, ValidationError

from datanode import deps as datanode_deps
from datanode.internal_api import app as datanode_internal
from datanode.public_api import app as datanode_public
from dfs_common import errors, models
from dfs_common.config import Settings
from dfs_common.errors import ERROR_TABLE, ErrorCode, ErrorResponse
from dfs_common.models import DirEntry, HeartbeatRequest, LockRequest, MetaServerListResponse, check_dfs_path
from dfs_common.security import sign_pass, verify_pass
from metaserver.etcd import EtcdError, LeaseNotFound, ResponseLost
from metaserver.internal_api import app as metaserver_internal
from metaserver.public_api import app as metaserver_public

from fakes import JWT_SECRET, PASS_KEY, FakeEtcd, bearer, internal_client, public_client  # tests/fakes.py
from metaserver import deps as metaserver_deps

ROOT = Path(__file__).resolve().parents[1]
CONTRATOS_MD = (ROOT / "docs" / "contratos.md").read_text(encoding="utf-8")
ENV_EXAMPLE = ROOT / ".env.example"

# Mismos nombres que usa scripts/export_openapi.py para docs/api/{nombre}.json.
APPS: dict[str, FastAPI] = {
    "metaserver-public": metaserver_public,
    "metaserver-internal": metaserver_internal,
    "datanode-public": datanode_public,
    "datanode-internal": datanode_internal,
}

# La lista de rutas de docs/contratos.md.
EXPECTED_ROUTES: dict[str, set[tuple[str, str]]] = {
    "metaserver-public": {
        ("POST", "/api/v1/auth/login"),
        ("GET", "/api/v1/cluster/metaservers"),
        ("GET", "/api/v1/fs/ls"),
        ("GET", "/api/v1/fs/stat"),
        ("POST", "/api/v1/fs/mkdir"),
        ("DELETE", "/api/v1/fs/rmdir"),
        ("DELETE", "/api/v1/fs/rm"),
        ("POST", "/api/v1/locks"),
        ("PUT", "/api/v1/locks/{lock_id}"),
        ("DELETE", "/api/v1/locks/{lock_id}"),
        ("GET", "/api/v1/files/blocks"),
        ("POST", "/api/v1/uploads"),
        ("POST", "/api/v1/uploads/{upload_id}/realloc"),
        ("POST", "/api/v1/uploads/{upload_id}/commit"),
        ("POST", "/api/v1/admin/users"),
        ("GET", "/api/v1/admin/status"),
        ("GET", "/api/v1/health"),
    },
    "metaserver-internal": {
        ("POST", "/api/v1/internal/register"),
        ("POST", "/api/v1/internal/heartbeat"),
        ("POST", "/api/v1/internal/blocks/confirm"),
    },
    "datanode-public": {
        ("PUT", "/api/v1/blocks/{hash}"),
        ("GET", "/api/v1/blocks/{hash}"),
        ("GET", "/api/v1/health"),
    },
    "datanode-internal": {
        ("PUT", "/api/v1/internal/blocks/{user}/{hash}"),
    },
}

HASH = "b64bb9046f61749097db060781bcbbba317c08ff0ac378e9aed9a6d18217e449"
SAMPLE_PATH_PARAMS = {
    "lock_id": "694d8a1c2e3b4f05",
    "upload_id": "9b2f6d1e-4c3a-4e8b-9f1d-2a7c5e8b1d43",
    "hash": HASH,
    "user": "ana",
}
NOT_IMPLEMENTED = {"error": "E_NOT_IMPLEMENTED", "message": "ruta todavía sin implementar", "retryable": False}


def documented_routes(app: FastAPI) -> set[tuple[str, str]]:
    """(método, ruta) de cada operación del OpenAPI de la app."""
    return {(method.upper(), path) for path, operations in app.openapi()["paths"].items() for method in operations}


def operation(app: FastAPI, method: str, path: str) -> dict:
    return app.openapi()["paths"][path][method.lower()]


def documented_errors(app: FastAPI, method: str, path: str) -> set[str]:
    """Códigos de error que el OpenAPI documenta para una ruta (un ejemplo por código)."""
    responses = operation(app, method, path)["responses"].values()
    return {code for r in responses for code in r.get("content", {}).get("application/json", {}).get("examples", {})}


def models_with_examples() -> list[type[BaseModel]]:
    found = [ErrorResponse]
    for obj in vars(models).values():
        if isinstance(obj, type) and issubclass(obj, BaseModel) and obj.__module__ == models.__name__:
            if "examples" in (obj.model_config.get("json_schema_extra") or {}):
                found.append(obj)
    return found


def clear_dfs_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in list(os.environ):
        if key.upper().startswith("DFS_"):
            monkeypatch.delenv(key)


# =====================================================================
# Apps y rutas
# =====================================================================


@pytest.mark.parametrize("name", APPS)
def test_app_loads(name):
    spec = APPS[name].openapi()
    assert spec["info"]["title"].startswith("DFSha")


@pytest.mark.parametrize("name", APPS)
def test_routes_are_exactly_the_contract(name):
    assert documented_routes(APPS[name]) == EXPECTED_ROUTES[name]


@pytest.mark.parametrize("name", APPS)
def test_every_request_and_response_model_has_an_example(name):
    spec = APPS[name].openapi()
    schemas = spec["components"]["schemas"]
    for path, operations in spec["paths"].items():
        for method, op in operations.items():
            contents = [op.get("requestBody", {}).get("content", {})]
            contents += [r.get("content", {}) for status, r in op["responses"].items() if status.startswith("2")]
            for content in contents:
                for media in content.values():
                    ref = media.get("schema", {}).get("$ref")
                    if ref:
                        model_name = ref.rsplit("/", 1)[-1]
                        assert schemas[model_name].get("examples"), f"{method.upper()} {path}: {model_name} sin ejemplo"


# =====================================================================
# Ejemplos
# =====================================================================


@pytest.mark.parametrize("model", models_with_examples(), ids=lambda m: m.__name__)
def test_examples_validate_against_their_model(model):
    for example in model.model_config["json_schema_extra"]["examples"]:
        model.model_validate(example)


def test_contratos_md_shows_the_same_examples_as_the_models():
    blocks = [json.loads(block) for block in re.findall(r"```json\n(.*?)\n```", CONTRATOS_MD, flags=re.DOTALL)]
    for model in models_with_examples():
        for example in model.model_config["json_schema_extra"]["examples"]:
            assert example in blocks, f"el ejemplo de {model.__name__} no está en docs/contratos.md"


# =====================================================================
# Errores y stubs
# =====================================================================


def test_every_error_code_has_http_status_and_retryable():
    assert set(ERROR_TABLE) == set(ErrorCode)
    for code, spec in ERROR_TABLE.items():
        assert 400 <= spec.http_status <= 599, code
        assert isinstance(spec.retryable, bool), code
        assert spec.description, code


def test_contratos_md_has_the_same_error_table():
    for code, spec in ERROR_TABLE.items():
        row = f"| {spec.http_status} | `{code}` | {spec.description} | {str(spec.retryable).lower()} |"
        assert row in CONTRATOS_MD, f"falta o cambió la fila de {code} en docs/contratos.md"


# Rutas que ya no son stubs, además de health. Sus pruebas están en tests/test_auth.py (tarea 07),
# tests/test_fs.py (tarea 08), tests/test_datanode_internal.py (tarea 09), tests/test_datanode_public.py
# (tarea 10), tests/test_locks.py (tarea 11), tests/test_uploads.py, test_files.py y test_blocks.py
# (tarea 12), y tests/test_nodes.py, test_metaservers.py y test_status.py (tarea 13). PUT /blocks/{hash}
# sigue siendo stub, pero antes verifica el pase: su 501 se prueba con un pase válido en
# tests/test_datanode_public.py.
IMPLEMENTED_ROUTES = {
    ("metaserver-public", "POST", "/api/v1/auth/login"),
    ("metaserver-public", "POST", "/api/v1/admin/users"),
    ("metaserver-public", "GET", "/api/v1/fs/ls"),
    ("metaserver-public", "GET", "/api/v1/fs/stat"),
    ("metaserver-public", "POST", "/api/v1/fs/mkdir"),
    ("metaserver-public", "DELETE", "/api/v1/fs/rmdir"),
    ("metaserver-public", "DELETE", "/api/v1/fs/rm"),
    ("metaserver-public", "POST", "/api/v1/locks"),
    ("metaserver-public", "PUT", "/api/v1/locks/{lock_id}"),
    ("metaserver-public", "DELETE", "/api/v1/locks/{lock_id}"),
    ("metaserver-public", "GET", "/api/v1/files/blocks"),
    ("metaserver-public", "POST", "/api/v1/uploads"),
    ("metaserver-public", "POST", "/api/v1/uploads/{upload_id}/realloc"),
    ("metaserver-public", "POST", "/api/v1/uploads/{upload_id}/commit"),
    ("metaserver-internal", "POST", "/api/v1/internal/blocks/confirm"),
    ("metaserver-public", "GET", "/api/v1/cluster/metaservers"),
    ("metaserver-public", "GET", "/api/v1/admin/status"),
    ("metaserver-internal", "POST", "/api/v1/internal/register"),
    ("metaserver-internal", "POST", "/api/v1/internal/heartbeat"),
    ("datanode-internal", "PUT", "/api/v1/internal/blocks/{user}/{hash}"),
    ("datanode-public", "GET", "/api/v1/blocks/{hash}"),
    ("datanode-public", "PUT", "/api/v1/blocks/{hash}"),
}


def stub_operations() -> list:
    """Todas las rutas menos health y las ya implementadas."""
    return [
        pytest.param(name, method, path, id=f"{name} {method} {path}")
        for name, app in APPS.items()
        for method, path in sorted(documented_routes(app))
        if not path.endswith("/health") and (name, method, path) not in IMPLEMENTED_ROUTES
    ]


def test_implemented_routes_exist():
    """Si una ruta de IMPLEMENTED_ROUTES cambia de nombre, la prueba de los stubs no debe saltársela en silencio."""
    for name, method, path in IMPLEMENTED_ROUTES:
        assert (method, path) in documented_routes(APPS[name])


@pytest.mark.parametrize(("name", "method", "path"), stub_operations())
def test_stub_responds_501_with_error_shape(name, method, path):
    app = APPS[name]
    spec = app.openapi()
    op = spec["paths"][path][method.lower()]
    # Petición válida armada con los ejemplos del contrato.
    params = {p["name"]: "/docs/informe.pdf" for p in op.get("parameters", []) if p["in"] == "query"}
    params.pop("version", None)
    request = {}
    content = op.get("requestBody", {}).get("content", {})
    if "application/json" in content:
        model_name = content["application/json"]["schema"]["$ref"].rsplit("/", 1)[-1]
        request["json"] = spec["components"]["schemas"][model_name]["examples"][0]
    elif "application/octet-stream" in content:
        request["content"] = b"contenido del bloque"

    response = TestClient(app).request(method, path.format(**SAMPLE_PATH_PARAMS), params=params, **request)

    assert response.status_code == 501
    assert response.json() == NOT_IMPLEMENTED


@pytest.mark.parametrize("app", [metaserver_public, datanode_public], ids=["metaserver", "datanode"])
def test_health_works(app):
    response = TestClient(app).get("/api/v1/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_invalid_parameters_respond_400_bad_request():
    # Sin lifespan no hay almacén ni clave de pases, y FastAPI resuelve las dependencias antes de
    # validar la ruta. Con ellas reemplazadas, el hash inválido se rechaza antes de mirar el pase.
    datanode_public.dependency_overrides[datanode_deps.get_pass_key] = lambda: b"k" * 32
    datanode_public.dependency_overrides[datanode_deps.get_store] = lambda: None
    try:
        response = TestClient(datanode_public).get("/api/v1/blocks/no-es-un-hash")
    finally:
        datanode_public.dependency_overrides.clear()
    assert response.status_code == 400
    body = response.json()
    assert body["error"] == "E_BAD_REQUEST"
    assert body["retryable"] is False


def test_unknown_route_responds_404_not_found():
    response = TestClient(datanode_public).get("/api/v1/no-existe")
    assert response.status_code == 404
    assert response.json()["error"] == "E_NOT_FOUND"


def test_method_not_allowed_responds_400_bad_request():
    """Decisión 19."""
    response = TestClient(metaserver_public).patch("/api/v1/fs/mkdir")
    assert response.status_code == 400
    assert response.json()["error"] == "E_BAD_REQUEST"


# =====================================================================
# OpenAPI exportado
# =====================================================================


@pytest.mark.parametrize("name", APPS)
def test_exported_openapi_matches_code(name):
    exported = json.loads((ROOT / "docs" / "api" / f"{name}.json").read_text(encoding="utf-8"))
    generated = json.loads(json.dumps(APPS[name].openapi()))
    assert exported == generated, "corre: python scripts/export_openapi.py"


# Sección de docs/contratos.md con la tabla de rutas de cada API.
MD_SECTIONS = {
    "metaserver-public": "## 2.",
    "metaserver-internal": "## 3.",
    "datanode-public": "## 4.",
    "datanode-internal": "## 5.",
}


@pytest.mark.parametrize("name", APPS)
def test_contratos_md_route_tables_match_openapi(name):
    """Cada fila de las tablas de rutas dice los mismos estados HTTP que el OpenAPI."""
    start = CONTRATOS_MD.index(MD_SECTIONS[name])
    section = CONTRATOS_MD[start : CONTRATOS_MD.index("\n## ", start + 4)]
    paths = APPS[name].openapi()["paths"]
    rows = re.findall(r"^\| (GET|POST|PUT|DELETE) \| `([^`?]+)[^`]*` \|(.*)\|$", section, flags=re.MULTILINE)
    assert {(method, path) for method, path, _ in rows} == EXPECTED_ROUTES[name]
    for method, path, rest in rows:
        columns = [c.strip() for c in rest.split("|")]
        ok_column, errors_column = columns[-3], columns[-2]
        statuses = paths[path][method.lower()]["responses"]
        assert re.findall(r"\b2\d\d\b", ok_column) == [s for s in statuses if s.startswith("2")], f"{method} {path}"
        documented = sorted(s for s in statuses if not s.startswith("2"))
        assert sorted(set(re.findall(r"\b[45]\d\d\b", errors_column))) == documented, f"{method} {path}"
        assert f"### {method} {path}" in CONTRATOS_MD, f"falta la sección de {method} {path}"


@pytest.mark.parametrize("name", ["metaserver-internal", "datanode-internal"])
def test_internal_apis_declare_mtls(name):
    spec = APPS[name].openapi()
    assert spec["components"]["securitySchemes"]["mTLS"]["type"] == "mutualTLS"
    for operations in spec["paths"].values():
        for op in operations.values():
            assert op["security"] == [{"mTLS": []}]


# =====================================================================
# Configuración (DA-16): config.py, .env.example y docs/contratos.md dicen lo mismo
# =====================================================================

# Valores que dependen del despliegue: en .env.example son de ejemplo.
DEPLOYMENT_FIELDS = {"meta_seeds", "etcd_endpoints", "zone", "node_id", "node_ip"}


def env_example_test_values() -> dict[str, str]:
    """Valor de pruebas de cada variable, tomado del comentario "Pruebas: X" de la línea anterior."""
    values, comment = {}, ""
    for line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
        if line.startswith("#"):
            comment = line
        elif line.startswith("DFS_"):
            if "Pruebas: " in comment:
                values[line.split("=", 1)[0]] = comment.split("Pruebas: ", 1)[1].strip()
            comment = ""
    return values


def test_settings_defaults_are_the_normal_values(monkeypatch):
    clear_dfs_env(monkeypatch)
    s = Settings(_env_file=None)
    table_19 = (
        s.block_mib, s.w, s.primary_wait_s, s.lock_lease_s, s.heartbeat_s, s.node_lease_s,
        s.rereplicate_after_s, s.leader_grace_s, s.retention_s, s.gc_interval_s, s.gc_min_age_s,
    )  # fmt: skip
    assert table_19 == (8, 2, 10, 30, 5, 15, 60, 300, 600, 600, 3600)
    assert (s.lock_renew_s, s.gc_clock_margin_s) == (10, 1)  # decisión 12
    assert s.leader_scan_s == 10  # tarea 02d
    assert (s.jwt_ttl_s, s.pass_ttl_s) == (3600, 600)
    assert (s.meta_public_port, s.meta_internal_port, s.dn_public_port, s.dn_internal_port) == (8443, 8444, 9443, 9444)
    assert s.admin_password_file is None  # decisión 17


def test_settings_reads_dfs_variables(monkeypatch):
    clear_dfs_env(monkeypatch)
    monkeypatch.setenv("DFS_LEADER_GRACE_S", "60")
    monkeypatch.setenv("DFS_ETCD_ENDPOINTS", "https://172.31.0.10:2379, https://172.31.16.10:2379")
    monkeypatch.setenv("DFS_ADMIN_PASSWORD_FILE", "")
    s = Settings(_env_file=None)
    assert s.leader_grace_s == 60
    assert s.etcd_endpoints == ["https://172.31.0.10:2379", "https://172.31.16.10:2379"]
    assert s.admin_password_file is None  # vacía = valor por defecto


def test_env_example_lists_every_setting_with_the_normal_values(monkeypatch):
    clear_dfs_env(monkeypatch)
    lines = ENV_EXAMPLE.read_text(encoding="utf-8").splitlines()
    keys = {line.split("=", 1)[0] for line in lines if line.startswith("DFS_")}
    assert keys == {f"DFS_{field.upper()}" for field in Settings.model_fields}
    from_example = Settings(_env_file=ENV_EXAMPLE).model_dump(exclude=DEPLOYMENT_FIELDS)
    assert from_example == Settings(_env_file=None).model_dump(exclude=DEPLOYMENT_FIELDS)


def test_contratos_md_lists_every_setting_with_the_same_values(monkeypatch):
    clear_dfs_env(monkeypatch)
    defaults = Settings(_env_file=None)
    test_values = env_example_test_values()
    for field in Settings.model_fields:
        var = f"DFS_{field.upper()}"
        value = getattr(defaults, field)
        row = f"| `{var}` |"
        if isinstance(value, int):
            row += f" {value} |"
            if var in test_values:
                row += f" {test_values[var]} |"
        assert row in CONTRATOS_MD, f"falta o cambió la fila de {var} en la sección 9 de docs/contratos.md"


# =====================================================================
# Decisiones de la tarea 02
# =====================================================================


# --- Decisión 2: respuestas sin datos y locks ---


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/api/v1/fs/mkdir"),
        ("DELETE", "/api/v1/fs/rmdir"),
        ("DELETE", "/api/v1/fs/rm"),
        ("DELETE", "/api/v1/locks/{lock_id}"),
        ("POST", "/api/v1/admin/users"),
    ],
)
def test_operations_without_data_answer_ok_true(method, path):
    ok = operation(metaserver_public, method, path)["responses"]["200"]
    assert ok["content"]["application/json"]["schema"]["$ref"].endswith("/OkResponse")


def test_renewing_an_expired_lock_is_lock_lost():
    errors = documented_errors(metaserver_public, "PUT", "/api/v1/locks/{lock_id}")
    assert "E_LOCK_LOST" in errors
    assert "E_NOT_FOUND" not in errors


def test_releasing_a_lock_is_idempotent():
    assert "E_NOT_FOUND" not in documented_errors(metaserver_public, "DELETE", "/api/v1/locks/{lock_id}")


# --- Decisión 3: rutas de archivo y 403 ---

VALID_PATHS = [
    "/",
    "/docs",
    "/docs/informe.pdf",
    "/mis cosas/ñandú.txt",
    "/" + "x" * 255,
    "/" + "/".join(["a" * 255] * 4),  # 1024 bytes justos
]
INVALID_PATHS = [
    "",
    "docs/informe.pdf",
    "/docs/",
    "//",
    "/docs//informe.pdf",
    "/./docs",
    "/docs/..",
    "/docs/a\0b",
    "/" + "x" * 256,
    "/" + "ñ" * 128,  # 128 caracteres pero 256 bytes en UTF-8
    "/" + "/".join(["a" * 200] * 6),  # componentes válidos, pero 1206 bytes
]


# Las rutas /fs exigen el JWT antes de validar los parámetros: sin token, una ruta inválida
# responde 401. Por eso estas pruebas mandan un token válido (tests/fakes.py).


@pytest.mark.parametrize("path", VALID_PATHS, ids=range(len(VALID_PATHS)))
def test_valid_paths_are_accepted(path):
    assert check_dfs_path(path) == path
    with public_client() as client:
        response = client.get("/api/v1/fs/stat", params={"path": path}, headers=bearer())
    # Válida: llega a la ruta. La raíz existe siempre; las demás no existen en un etcd vacío.
    assert response.status_code == (200 if path == "/" else 404)


@pytest.mark.parametrize("path", INVALID_PATHS, ids=range(len(INVALID_PATHS)))
def test_invalid_paths_respond_400(path):
    with pytest.raises(ValueError):
        check_dfs_path(path)
    with public_client() as client:
        in_query = client.get("/api/v1/fs/ls", params={"path": path}, headers=bearer())
        in_body = client.post("/api/v1/fs/mkdir", json={"path": path}, headers=bearer())
    for response in (in_query, in_body):
        assert response.status_code == 400
        assert response.json()["error"] == "E_BAD_REQUEST"
        assert "Value error" not in response.json()["message"]  # mensaje limpio, sin el prefijo de Pydantic


# Cuatro casos: pase en el DataNode, /admin sin rol, lock_id o upload_id de otro usuario,
# y una llamada a la API interna del MetaServer desde una IP que no es la de su node_id.
FORBIDDEN_ROUTES = {
    ("datanode-public", "PUT", "/api/v1/blocks/{hash}"),
    ("datanode-public", "GET", "/api/v1/blocks/{hash}"),
    ("metaserver-public", "POST", "/api/v1/admin/users"),
    ("metaserver-public", "GET", "/api/v1/admin/status"),
    ("metaserver-public", "PUT", "/api/v1/locks/{lock_id}"),
    ("metaserver-public", "DELETE", "/api/v1/locks/{lock_id}"),
    ("metaserver-public", "POST", "/api/v1/uploads"),
    ("metaserver-public", "POST", "/api/v1/uploads/{upload_id}/realloc"),
    ("metaserver-public", "POST", "/api/v1/uploads/{upload_id}/commit"),
    ("metaserver-internal", "POST", "/api/v1/internal/register"),
    ("metaserver-internal", "POST", "/api/v1/internal/heartbeat"),
    ("metaserver-internal", "POST", "/api/v1/internal/blocks/confirm"),
}


def test_forbidden_only_where_decided():
    documented = {
        (name, method, path)
        for name, app in APPS.items()
        for method, path in documented_routes(app)
        if "E_FORBIDDEN" in documented_errors(app, method, path)
    }
    assert documented == FORBIDDEN_ROUTES


# --- Decisión 5: directorios ---


def test_root_always_exists():
    # Con un etcd vacío: la raíz no se guarda, pero existe y es un directorio.
    with public_client() as client:
        ls_root = client.get("/api/v1/fs/ls", params={"path": "/"}, headers=bearer())
        stat_root = client.get("/api/v1/fs/stat", params={"path": "/"}, headers=bearer())
    assert ls_root.status_code == 200
    assert ls_root.json() == {"entries": []}
    assert stat_root.status_code == 200
    assert stat_root.json() == {"path": "/", "type": "dir", "size": 0, "version": 0, "modified_at": 0}


def test_mkdir_does_not_create_parents():
    errors = documented_errors(metaserver_public, "POST", "/api/v1/fs/mkdir")
    assert {"E_NOT_FOUND", "E_EXISTS"} <= errors


def test_directories_have_size_0_version_0_and_a_date():
    entry = {"name": "fotos", "type": "dir", "size": 0, "version": 0, "modified_at": 1793178900000}
    assert entry in models.LsResponse.model_config["json_schema_extra"]["examples"][0]["entries"]
    with pytest.raises(ValidationError):
        DirEntry.model_validate({**entry, "modified_at": None})


# --- Decisión 6: rm ---


def test_rm_takes_the_lock_internally():
    op = operation(metaserver_public, "DELETE", "/api/v1/fs/rm")
    assert [p["name"] for p in op["parameters"]] == ["path"]  # sin lock_id: el cliente no llama a /locks
    assert "requestBody" not in op
    assert "E_LOCKED" in documented_errors(metaserver_public, "DELETE", "/api/v1/fs/rm")


@pytest.mark.parametrize("route", ["/api/v1/fs/rm", "/api/v1/fs/rmdir"])
def test_rm_and_rmdir_on_the_wrong_type_are_bad_request(route):
    assert "E_BAD_REQUEST" in documented_errors(metaserver_public, "DELETE", route)


# --- Decisión 7: commit con bloques sin confirmar ---


def test_commit_without_confirmed_copies_is_retryable_replicas_error():
    assert "E_REPLICAS" in documented_errors(metaserver_public, "POST", "/api/v1/uploads/{upload_id}/commit")
    assert ERROR_TABLE[ErrorCode.E_REPLICAS].retryable is True
    upload = documented_errors(metaserver_public, "POST", "/api/v1/uploads")
    assert {"E_VERSION_CONFLICT", "E_LOCK_LOST"} <= upload


# --- Decisión 10: lista de MetaServers y semillas ---


def test_metaserver_list_has_both_ports():
    listed = MetaServerListResponse.model_validate(
        {"metaservers": [{"meta_id": "meta1", "ip": "172.31.0.10", "public_port": 8443, "internal_port": 8444}]}
    )
    assert listed.metaservers[0].internal_port == 8444


def test_meta_seeds_are_hosts_without_port(monkeypatch):
    clear_dfs_env(monkeypatch)
    monkeypatch.setenv("DFS_META_SEEDS", "172.31.0.10, 172.31.16.10,172.31.32.10")
    assert Settings(_env_file=None).meta_seeds == ["172.31.0.10", "172.31.16.10", "172.31.32.10"]
    # DA-11: si el laboratorio permite Route 53, la lista se cambia por un nombre sin tocar el código.
    monkeypatch.setenv("DFS_META_SEEDS", "meta.dfsha.internal")
    assert Settings(_env_file=None).meta_seeds == ["meta.dfsha.internal"]
    monkeypatch.setenv("DFS_META_SEEDS", "172.31.0.10:8443")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


# --- Decisión 15: copia dañada ---


def test_damaged_copy_is_retryable_checksum_error():
    assert "E_CHECKSUM" in documented_errors(datanode_public, "GET", "/api/v1/blocks/{hash}")
    assert ERROR_TABLE[ErrorCode.E_CHECKSUM].retryable is True


def test_heartbeat_reports_bad_blocks():
    beat = {"node_id": "dn2", "lease_id": "694d8a1c2e3b4f13", "free_bytes": 1}
    assert HeartbeatRequest.model_validate(beat).bad_blocks == []
    reported = HeartbeatRequest.model_validate({**beat, "bad_blocks": [{"user": "ana", "hash": HASH}]})
    assert reported.bad_blocks[0].hash == HASH


# --- Decisión 18: wait_s ---


def test_wait_s_defaults_to_30():
    assert LockRequest(path="/docs/informe.pdf").wait_s == 30


@pytest.mark.parametrize(("wait_s", "status"), [(-1, 400), (0, 200), (60, 200), (61, 400)])
def test_wait_s_goes_from_0_to_60(wait_s, status):
    # Con FakeEtcd el lock está libre: con un wait_s válido se obtiene sin esperar (tarea 11).
    with public_client() as client:
        response = client.post(
            "/api/v1/locks", json={"path": "/docs/informe.pdf", "wait_s": wait_s}, headers=bearer()
        )
    assert response.status_code == status


# =====================================================================
# Ajustes de la tarea 02b
# =====================================================================


# --- Punto 1: toda respuesta es un objeto ---


def test_metaserver_list_is_an_object():
    schemas = metaserver_public.openapi()["components"]["schemas"]
    listing = schemas["MetaServerListResponse"]
    assert listing["type"] == "object"
    assert listing["properties"]["metaservers"]["items"]["$ref"].endswith("/MetaServerInfo")


def test_admin_status_keeps_metaserver_status_with_the_same_shape():
    schemas = metaserver_public.openapi()["components"]["schemas"]
    status = schemas["AdminStatusResponse"]
    assert status["properties"]["metaservers"]["items"]["$ref"].endswith("/MetaServerStatus")
    assert schemas["MetaServerStatus"]["properties"] == schemas["MetaServerInfo"]["properties"]
    assert set(models.MetaServerStatus.model_fields) == {"meta_id", "ip", "public_port", "internal_port"}


@pytest.mark.parametrize("name", APPS)
def test_every_json_response_is_an_object(name):
    spec = APPS[name].openapi()
    schemas = spec["components"]["schemas"]
    for path, operations in spec["paths"].items():
        for method, op in operations.items():
            for status, response in op["responses"].items():
                schema = response.get("content", {}).get("application/json", {}).get("schema")
                if schema:
                    resolved = schemas[schema["$ref"].rsplit("/", 1)[-1]] if "$ref" in schema else schema
                    assert resolved.get("type") == "object", f"{method.upper()} {path} {status} no es un objeto"


# --- Punto 2: pendientes de la sección 10 ---


def test_stat_root_has_modified_at_0():
    root = {"path": "/", "type": "dir", "size": 0, "version": 0, "modified_at": 0}
    assert root in models.StatResponse.model_config["json_schema_extra"]["examples"]
    models.StatResponse.model_validate(root)


@pytest.mark.parametrize("route", ["/api/v1/fs/rmdir", "/api/v1/fs/rm"])
def test_removing_the_root_is_bad_request(route):
    with public_client() as client:
        root = client.delete(route, params={"path": "/"}, headers=bearer())
        other = client.delete(route, params={"path": "/docs"}, headers=bearer())
    assert root.status_code == 400
    assert root.json()["error"] == "E_BAD_REQUEST"
    assert other.json()["error"] == "E_NOT_FOUND"  # otra ruta sí llega a la ruta: no existe


def test_ls_on_a_file_returns_that_file_as_the_only_entry():
    examples = models.LsResponse.model_config["json_schema_extra"]["examples"]
    single = [e["entries"] for e in examples if len(e["entries"]) == 1]
    assert single and single[0][0]["type"] == "file"


@pytest.mark.parametrize("route", ["/api/v1/uploads", "/api/v1/uploads/{upload_id}/commit"])
def test_lock_id_of_another_file_is_lock_lost(route):
    assert "E_LOCK_LOST" in documented_errors(metaserver_public, "POST", route)
    assert "otro archivo" in ERROR_TABLE[ErrorCode.E_LOCK_LOST].description


def test_wait_s_limits_are_fixed_in_the_contract():
    schema = models.LockRequest.model_json_schema()["properties"]["wait_s"]
    assert (schema["minimum"], schema["maximum"], schema["default"]) == (0, 60, 30)
    assert not any("lock_wait" in field for field in Settings.model_fields)  # no es una variable de entorno


# =====================================================================
# Auditoría del contrato
# =====================================================================


@pytest.mark.parametrize(("secondaries", "valid"), [(["172.31.16.10:9444"], True), ([], False), (["a:1", "b:2", "c:3"], False)])
def test_write_pass_carries_one_or_two_secondaries(secondaries, valid):
    """DA-05: con solo 2 DataNodes vivos en zonas distintas, el trío es de 2 nodos y hay 1 secundario."""
    write_pass = {"user": "ana", "hash": HASH, "op": "w", "secondaries": secondaries, "exp": 1793628600}
    if valid:
        models.BlockPass.model_validate(write_pass)
    else:
        with pytest.raises(ValidationError):
            models.BlockPass.model_validate(write_pass)


def decode_pass(token: str) -> dict:
    """Contenido JSON de un pase base64url(json).base64url(hmac)."""
    part = token.split(".")[0]
    return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))


def first_example(model: type[BaseModel]) -> dict:
    return model.model_config["json_schema_extra"]["examples"][0]


def test_example_passes_match_their_blocks_and_timeline():
    """Los pases de los ejemplos dicen lo mismo que el bloque que acompañan y no se emiten antes del commit."""
    committed_at_s = first_example(models.LsResponse)["entries"][1]["modified_at"] // 1000
    for block in first_example(models.FileBlocksResponse)["blocks"]:
        read_pass = decode_pass(block["pass"])
        assert (read_pass["hash"], read_pass["op"]) == (block["hash"], "r")
        assert read_pass["exp"] - 600 >= committed_at_s  # emitido después de publicar la versión 4
    for block in first_example(models.UploadResponse)["missing"] + first_example(models.ReallocResponse)["missing"]:
        write_pass = decode_pass(block["pass"])
        assert (write_pass["hash"], write_pass["op"]) == (block["hash"], "w")
        assert block["primary"]["address"].split(":")[0] not in [s.split(":")[0] for s in write_pass["secondaries"]]


# =====================================================================
# Decisiones de la tarea 02c
# =====================================================================


# --- Decisión 1: zonas al confirmar un bloque ---


def test_confirm_can_answer_replicas_error():
    errors = documented_errors(metaserver_internal, "POST", "/api/v1/internal/blocks/confirm")
    assert "E_REPLICAS" in errors
    row = "| POST | `/api/v1/internal/blocks/confirm` | 200 `OkResponse` | 400, 403, 422 `E_REPLICAS`, 503 |"
    assert row in CONTRATOS_MD


# --- Decisión 2: tamaño de bloque ---


def test_upload_requires_block_size():
    example = first_example(models.UploadRequest)
    assert example["block_size"] == 8388608
    without = {key: value for key, value in example.items() if key != "block_size"}
    with pytest.raises(ValidationError):
        models.UploadRequest.model_validate(without)
    with pytest.raises(ValidationError):
        models.UploadRequest.model_validate({**example, "block_size": 0})
    with public_client() as client:  # la ruta exige el JWT antes de validar el cuerpo (sección 1)
        response = client.post("/api/v1/uploads", json=without, headers=bearer())
    assert response.status_code == 400


@pytest.mark.parametrize(
    ("app", "method", "path"),
    [(datanode_public, "PUT", "/api/v1/blocks/{hash}"), (datanode_internal, "PUT", "/api/v1/internal/blocks/{user}/{hash}")],
    ids=["publico", "interno"],
)
def test_block_puts_can_reject_oversized_bodies(app, method, path):
    assert "E_BAD_REQUEST" in documented_errors(app, method, path)
    assert "DFS_BLOCK_MIB" in operation(app, method, path)["description"]


# --- Decisión 3: pase vencido ---


def test_expired_pass_is_auth_error_and_bad_pass_is_forbidden():
    assert "pase ausente o vencido" in ERROR_TABLE[ErrorCode.E_AUTH].description
    assert "firma inválida" in ERROR_TABLE[ErrorCode.E_FORBIDDEN].description
    for method in ("PUT", "GET"):
        assert {"E_AUTH", "E_FORBIDDEN"} <= documented_errors(datanode_public, method, "/api/v1/blocks/{hash}")


# --- Decisión 5: errores inesperados y una sola excepción con código ---


def test_internal_error_is_in_the_table():
    assert ERROR_TABLE[ErrorCode.E_INTERNAL][:2] == (500, False)
    assert "| 500 | `E_INTERNAL` | error inesperado del servidor | false |" in CONTRATOS_MD


class BrokenEtcd(FakeEtcd):
    """Un etcd que falla de una forma que nadie previó."""

    async def get(self, key):
        raise RuntimeError("detalle interno que no debe salir")

    async def get_prefix(self, prefix, **kwargs):
        raise RuntimeError("detalle interno que no debe salir")


# Las rutas de bloques del DataNode tienen sus casos en tests/test_datanode_internal.py y
# tests/test_datanode_public.py.
UNEXPECTED_ERROR_CASES = [
    ("metaserver-public", "GET", "/api/v1/files/blocks", {"params": {"path": "/docs/informe.pdf"}, "headers": bearer()}),
    ("metaserver-internal", "POST", "/api/v1/internal/blocks/confirm", {"json": first_example(models.BlockConfirmRequest)}),
]


@pytest.mark.parametrize(
    ("name", "method", "url", "request_args"), UNEXPECTED_ERROR_CASES, ids=[case[0] for case in UNEXPECTED_ERROR_CASES]
)
def test_unexpected_exception_responds_500_internal(name, method, url, request_args, caplog):
    app = APPS[name]
    app.dependency_overrides.update(
        {
            metaserver_deps.get_etcd: BrokenEtcd,
            metaserver_deps.get_settings: lambda: Settings(_env_file=None),
            metaserver_deps.get_jwt_secret: lambda: JWT_SECRET,
            metaserver_deps.get_pass_key: lambda: PASS_KEY,
        }
    )
    try:
        # Starlette vuelve a lanzar la excepción después de responder; aquí solo interesa la respuesta.
        response = TestClient(app, raise_server_exceptions=False).request(method, url, **request_args)
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 500
    assert response.json() == {"error": "E_INTERNAL", "message": "error inesperado del servidor", "retryable": False}
    assert any(record.levelname == "ERROR" and record.exc_info for record in caplog.records)  # quedó en el log


def test_dfs_error_is_the_only_exception_with_an_error_code():
    """Una sola excepción lleva código de error y llega al cliente: DFSError.

    Las de metaserver.etcd (EtcdError, LeaseNotFound y ResponseLost, tarea 06) son internas del MetaServer:
    no llevan código y las rutas las convierten en un DFSError (o en 500 E_INTERNAL).
    """
    modules = ["dfs_common.errors", "dfs_common.models", "dfs_common.config", "dfs_common.app"]
    modules += ["metaserver.public_api", "metaserver.internal_api", "datanode.public_api", "datanode.internal_api"]
    modules += ["metaserver.etcd", "metaserver.auth", "metaserver.deps", "metaserver.fs", "metaserver.keys"]
    modules += ["metaserver.locks", "metaserver.nodes", "metaserver.placement", "metaserver.blocks"]
    modules += ["metaserver.uploads", "metaserver.files"]
    project_packages = {"dfs_common", "metaserver", "datanode"}
    project_exceptions = set()
    for name in modules:
        for obj in vars(importlib.import_module(name)).values():
            is_exception = inspect.isclass(obj) and issubclass(obj, Exception)
            if is_exception and obj.__module__.split(".")[0] in project_packages:
                project_exceptions.add(obj)
    internal = {EtcdError, LeaseNotFound, ResponseLost}
    assert project_exceptions == {errors.DFSError} | internal
    for exception in internal:
        assert not issubclass(exception, errors.DFSError)
        assert not hasattr(exception("mensaje de etcd"), "code")


# --- Decisión 6: tipo equivocado o sin padre ---


@pytest.mark.parametrize(
    ("method", "path", "code"),
    [
        ("POST", "/api/v1/locks", "E_BAD_REQUEST"),
        ("POST", "/api/v1/uploads", "E_BAD_REQUEST"),
        ("GET", "/api/v1/files/blocks", "E_BAD_REQUEST"),
        ("POST", "/api/v1/uploads", "E_NOT_FOUND"),
        ("POST", "/api/v1/uploads/{upload_id}/commit", "E_NOT_FOUND"),
        ("POST", "/api/v1/fs/mkdir", "E_NOT_FOUND"),
    ],
)
def test_wrong_type_or_missing_parent_is_documented(method, path, code):
    assert code in documented_errors(metaserver_public, method, path)


def test_append_and_rw_can_create_a_missing_file():
    request = {**first_example(models.UploadRequest), "base_version": 0, "blocks": []}
    for mode in ("a", "rw"):
        assert models.UploadRequest.model_validate({**request, "mode": mode}).base_version == 0


# --- Decisión 8: IP del nodo ---


def test_node_ip_setting(monkeypatch):
    clear_dfs_env(monkeypatch)
    assert Settings(_env_file=None).node_ip is None  # sin valor por defecto
    monkeypatch.setenv("DFS_NODE_IP", "172.31.16.20")
    assert str(Settings(_env_file=None).node_ip) == "172.31.16.20"
    monkeypatch.setenv("DFS_NODE_IP", "dn4")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)
    assert "DFS_NODE_IP=" in ENV_EXAMPLE.read_text(encoding="utf-8")
    assert "| `DFS_NODE_IP` |" in CONTRATOS_MD


# --- Sección 10: sin puntos abiertos ---


def test_contratos_md_has_no_open_points():
    assert "## 10. Decisiones de las tareas 02c y 02d" in CONTRATOS_MD
    assert "Pendientes" not in CONTRATOS_MD


# =====================================================================
# Tarea 02d: correcciones de la auditoría
# =====================================================================


# --- Quién valida la escritura: la reserva rechaza índices repetidos y bloques mayores que block_size ---


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda ex: {**ex, "blocks": [ex["blocks"][0], ex["blocks"][0]]}, "índices repetidos"),
        (lambda ex: {**ex, "block_size": 4194304}, "mide más que block_size"),
    ],
    ids=["indices-repetidos", "bloque-mayor"],
)
def test_upload_rejects_repeated_indices_and_oversized_blocks(change, message):
    body = change(first_example(models.UploadRequest))
    with pytest.raises(ValidationError, match=message):
        models.UploadRequest.model_validate(body)
    with public_client() as client:  # la ruta exige el JWT antes de validar el cuerpo (sección 1)
        response = client.post("/api/v1/uploads", json=body, headers=bearer())
    assert response.status_code == 400
    assert response.json()["error"] == "E_BAD_REQUEST"
    assert message in response.json()["message"]


# --- Copias fantasma: disk_id en el registro y bloques que no están en bad_blocks ---


def test_register_requires_disk_id():
    example = first_example(models.RegisterRequest)
    assert example["disk_id"]
    without = {key: value for key, value in example.items() if key != "disk_id"}
    with pytest.raises(ValidationError):
        models.RegisterRequest.model_validate(without)
    with internal_client(ip=example["ip"]) as client:
        response = client.post("/api/v1/internal/register", json=without)
    assert response.status_code == 400


def test_missing_copies_are_reported_like_damaged_ones():
    assert "no encontró" in models.HeartbeatRequest.model_fields["bad_blocks"].description
    get_block = operation(datanode_public, "GET", "/api/v1/blocks/{hash}")
    assert "404 E_NOT_FOUND" in get_block["description"] and "bad_blocks" in get_block["description"]


# --- Copias vivas y DataNodes suficientes ---


def test_unavailable_covers_missing_datanodes():
    assert "sin DataNodes vivos suficientes" in ERROR_TABLE[ErrorCode.E_UNAVAILABLE].description
    for method, path in [
        ("GET", "/api/v1/files/blocks"),
        ("POST", "/api/v1/uploads"),
        ("POST", "/api/v1/uploads/{upload_id}/realloc"),
    ]:
        assert "E_UNAVAILABLE" in documented_errors(metaserver_public, method, path)


# --- Orden de comprobación del pase: vencimiento antes que hash y operación ---


def test_expired_pass_for_another_block_is_auth_error():
    key = b"k" * 32
    expired = sign_pass(models.BlockPass(user="ana", hash=HASH, op="r", exp=1000), key)
    with pytest.raises(errors.DFSError) as caught:
        verify_pass(expired, key, hash="0" * 64, op="w", now=2000)
    assert caught.value.code == ErrorCode.E_AUTH  # 401, no 403: el vencimiento se revisa primero


# --- Arranque: cada servicio exige sus variables de identidad ---


def test_settings_require_names_every_missing_variable(monkeypatch):
    clear_dfs_env(monkeypatch)
    settings = Settings(_env_file=None, node_id="dn1")
    with pytest.raises(SystemExit, match="DFS_NODE_IP, DFS_META_SEEDS"):
        settings.require("node_id", "node_ip", "meta_seeds")
    settings.require("node_id")  # no falta nada: no hace nada


@pytest.mark.parametrize(
    ("module_name", "required"),
    [
        ("metaserver.__main__", "DFS_NODE_ID, DFS_NODE_IP, DFS_ETCD_ENDPOINTS"),
        ("datanode.__main__", "DFS_NODE_ID, DFS_NODE_IP, DFS_ZONE, DFS_META_SEEDS"),
    ],
    ids=["metaserver", "datanode"],
)
def test_services_refuse_to_start_without_their_identity(module_name, required, monkeypatch, tmp_path):
    clear_dfs_env(monkeypatch)
    monkeypatch.chdir(tmp_path)  # sin .env: solo cuentan las variables de entorno
    module = importlib.import_module(module_name)

    def started_anyway(*args, **kwargs):
        raise AssertionError("arrancó sin sus variables de identidad")

    # Si faltara la comprobación, la prueba falla aquí en vez de abrir puertos y quedarse colgada.
    monkeypatch.setattr(module, "run", started_anyway)
    with pytest.raises(SystemExit, match=required):
        module.main()


# =====================================================================
# Revisión de la segunda auditoría de la tarea 02d
# =====================================================================


def test_file_blocks_shows_where_each_block_is_even_without_live_replicas():
    """DA-16: dfs stat --blocks muestra stored_on; replicas solo trae vivos y puede ir vacía."""
    for block in first_example(models.FileBlocksResponse)["blocks"]:
        assert {r["node_id"] for r in block["replicas"]} <= set(block["stored_on"])
    block = first_example(models.FileBlocksResponse)["blocks"][0]
    no_live_replicas = models.BlockLocation.model_validate({**block, "replicas": []})
    assert no_live_replicas.stored_on == ["dn2", "dn3", "dn4"]


def test_disk_id_file_cannot_clash_with_a_user_folder():
    """El archivo del disk_id empieza con punto, que un nombre de usuario no puede tener."""
    assert re.fullmatch(models.USER_PATTERN, "disk_id")  # "disk_id" sí sería un usuario válido
    assert not re.fullmatch(models.USER_PATTERN, ".disk_id")
    assert "DFS_DATA_DIR/.disk_id" in models.RegisterRequest.model_fields["disk_id"].description


def test_replicas_error_talks_about_live_copies_and_dfs_w():
    description = ERROR_TABLE[ErrorCode.E_REPLICAS].description
    assert "vivas" in description and "DFS_W" in description
    assert "confirmadas" not in description


# =====================================================================
# Cambios de contrato de la tarea 12 (sección 13)
# =====================================================================


def test_file_sha256_is_optional_in_the_commit_and_the_manifest():
    """a y rw envían file_sha256 null: calcularlo obligaría a descargar el archivo entero."""
    commit = first_example(models.CommitRequest)
    assert models.CommitRequest.model_validate({**commit, "file_sha256": None}).file_sha256 is None
    without = {key: value for key, value in commit.items() if key != "file_sha256"}
    assert models.CommitRequest.model_validate(without).file_sha256 is None
    manifest = {**first_example(models.FileBlocksResponse), "file_sha256": None}
    assert models.FileBlocksResponse.model_validate(manifest).file_sha256 is None
    assert "`file_sha256` es opcional" in CONTRATOS_MD


def test_uploads_key_keeps_the_blocks_of_the_reservation():
    assert "| `/uploads/{upload_id}` | `{user, path, mode, base_version, block_size, lock_key, blocks, created_at}` |" in (
        CONTRATOS_MD
    )
