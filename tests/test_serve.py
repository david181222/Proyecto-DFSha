"""Pruebas de la tarea 05 que no necesitan Docker: TLS de las dos APIs de cada servicio, arranque y apagado."""

import importlib
import importlib.util
import signal
import ssl
import subprocess
import sys
import time
from pathlib import Path

import pytest

from datanode.internal_api import app as datanode_internal
from datanode.public_api import app as datanode_public
from dfs_common.config import Settings
from dfs_common.serve import server_configs
from metaserver.internal_api import app as metaserver_internal
from metaserver.public_api import app as metaserver_public

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("gen_secrets", ROOT / "scripts" / "gen_secrets.py")
gen_secrets = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gen_secrets)


@pytest.fixture(scope="module")
def settings(tmp_path_factory) -> Settings:
    """Settings con un certificado de nodo real, generado por la tarea 04."""
    out = tmp_path_factory.mktemp("serve") / "secrets"
    gen_secrets.main(["--out", str(out), "--nodes", "meta1=10.88.0.21"])
    return Settings(
        _env_file=None,
        tls_ca_file=out / "ca.crt",
        tls_cert_file=out / "meta1" / "tls.crt",
        tls_key_file=out / "meta1" / "tls.key",
    )


@pytest.mark.parametrize(
    ("public_app", "internal_app", "ports"),
    [
        (metaserver_public, metaserver_internal, (8443, 8444)),
        (datanode_public, datanode_internal, (9443, 9444)),
    ],
    ids=["metaserver", "datanode"],
)
def test_public_is_https_and_internal_is_mtls_both_tls_1_3(settings, public_app, internal_app, ports):
    public, internal = server_configs(settings, (public_app, ports[0]), (internal_app, ports[1]))
    assert (public.port, internal.port) == ports
    assert public.ssl.verify_mode == ssl.CERT_NONE  # el cliente dfs no presenta certificado
    assert internal.ssl.verify_mode == ssl.CERT_REQUIRED  # mTLS: solo nodos con certificado de la CA
    for config in (public, internal):
        assert config.ssl.minimum_version == ssl.TLSVersion.TLSv1_3
        assert config.loaded


# Una API cuyo lifespan usa un hilo de trabajo (como hash_password al crear el primer admin)
# y después falla (como cuando etcd no responde), junto a otra API que sí arranca.
FAILED_STARTUP = """
import asyncio
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI
from fastapi.concurrency import run_in_threadpool

from dfs_common.serve import serve


@asynccontextmanager
async def broken(app):
    await run_in_threadpool(sum, [1, 2])
    raise RuntimeError("etcd no respondió")
    yield


configs = [uvicorn.Config(FastAPI(lifespan=broken), port=0), uvicorn.Config(FastAPI(), port=0)]
raise SystemExit(asyncio.run(serve(configs)))
"""


def test_a_failed_startup_ends_the_process_so_docker_restarts_it(tmp_path):
    """Sin esto, el proceso quedaba vivo y sin APIs: Docker no lo reiniciaba (tarea 07)."""
    script = tmp_path / "failed_startup.py"
    script.write_text(FAILED_STARTUP, encoding="utf-8")
    # Si el proceso se colgara, subprocess.run lo mata al vencer el timeout y la prueba falla.
    result = subprocess.run([sys.executable, str(script)], capture_output=True, timeout=30)
    assert result.returncode == 3  # el código con que uvicorn avisa que la app no arrancó
    assert b"etcd no respondi" in result.stderr


# Dos APIs con un lifespan que avisa por la salida cuando arranca y cuando se apaga, como las
# del MetaServer: la pública tiene las tareas de fondo (registro y líder) y la interna no.
TWO_LIFESPANS = """
import asyncio
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI

from dfs_common.serve import serve


def app(name):
    @asynccontextmanager
    async def lifespan(app):
        print(f"arranco {name}", flush=True)
        yield
        print(f"apagado {name}", flush=True)

    return FastAPI(lifespan=lifespan)


configs = [uvicorn.Config(app("publica"), port=0), uvicorn.Config(app("interna"), port=0)]
raise SystemExit(asyncio.run(serve(configs)))
"""


@pytest.mark.skipif(sys.platform == "win32", reason="Windows no le envía SIGTERM a un proceso hijo")
def test_one_sigterm_shuts_down_both_apis_with_their_lifespan(tmp_path):
    """docker stop envía SIGTERM: las dos APIs deben correr su apagado (el líder hace resign, tarea 19)."""
    script = tmp_path / "two_lifespans.py"
    script.write_text(TWO_LIFESPANS, encoding="utf-8")
    process = subprocess.Popen([sys.executable, str(script)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        started = sorted(process.stdout.readline() for _ in range(2))
        assert started == [b"arranco interna\n", b"arranco publica\n"]
        time.sleep(0.5)  # que los dos servidores terminen de arrancar
        process.send_signal(signal.SIGTERM)
        out, err = process.communicate(timeout=30)
    finally:
        process.kill()
    assert b"apagado publica" in out, err.decode()
    assert b"apagado interna" in out, err.decode()
    assert process.returncode == 0


def test_main_modules_read_nothing_on_import(monkeypatch):
    """python -m metaserver/datanode lee la configuración en main(), no al importar."""
    monkeypatch.setenv("DFS_TLS_CERT_FILE", "/no/existe")
    for name in ("metaserver.__main__", "datanode.__main__"):
        module = importlib.import_module(name)
        assert callable(module.main)
