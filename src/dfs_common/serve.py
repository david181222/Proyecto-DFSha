"""Arranque de un servicio con su API pública (HTTPS) y su API interna (mTLS) en un mismo proceso.

Lo usan `python -m metaserver` y `python -m datanode` (DA-06, DA-13). Las dos APIs:

- usan el certificado de este nodo (DFS_TLS_CERT_FILE y DFS_TLS_KEY_FILE);
- exigen TLS 1.3 como mínimo;
- la interna, además, exige un certificado de cliente firmado por la CA propia (DFS_TLS_CA_FILE): mTLS.

Nada de esto se lee al importar: las pruebas y scripts/export_openapi.py importan las
apps sin certificados.
"""

import asyncio
import ssl

import uvicorn
from fastapi import FastAPI

from dfs_common.config import Settings

# En todas las interfaces: en local, la IP de la red dfs; en AWS (red del host), el
# grupo de seguridad solo abre estos puertos dentro de la VPC (DA-13).
HOST = "0.0.0.0"


def server_config(settings: Settings, app: FastAPI, port: int, *, mtls: bool) -> uvicorn.Config:
    config = uvicorn.Config(
        app,
        host=HOST,
        port=port,
        ssl_certfile=settings.tls_cert_file,
        ssl_keyfile=settings.tls_key_file,
        ssl_ca_certs=settings.tls_ca_file if mtls else None,
        ssl_cert_reqs=ssl.CERT_REQUIRED if mtls else ssl.CERT_NONE,
    )
    # uvicorn no tiene una opción para la versión mínima de TLS: load() crea el
    # SSLContext y aquí se ajusta. Server.serve() no vuelve a cargar una Config ya cargada.
    config.load()
    config.ssl.minimum_version = ssl.TLSVersion.TLSv1_3
    return config


def server_configs(
    settings: Settings, public: tuple[FastAPI, int], internal: tuple[FastAPI, int]
) -> list[uvicorn.Config]:
    """public e internal son (app, puerto)."""
    return [server_config(settings, *public, mtls=False), server_config(settings, *internal, mtls=True)]


async def serve(configs: list[uvicorn.Config]) -> int:
    """Corre los servidores hasta que terminen. Devuelve 0, o el código de salida si uno no arrancó.

    Cada Server captura SIGTERM y SIGINT; al terminar restaura el manejador anterior
    y vuelve a lanzar la señal, así que una sola señal detiene a los dos.

    Si un servidor no arranca (por ejemplo, su lifespan no pudo hablar con etcd), uvicorn
    hace sys.exit() dentro del bucle de eventos. Aquí se atrapa, se detienen los demás y
    run() sale después, ya fuera del bucle: si saliera dentro, un hilo de trabajo de anyio
    (run_in_threadpool) quedaría vivo y el proceso se colgaría sin APIs, en vez de terminar
    para que Docker lo reinicie (restart: unless-stopped).
    """
    servers = [uvicorn.Server(config) for config in configs]
    exit_codes = []

    async def serve_one(server: uvicorn.Server) -> None:
        try:
            await server.serve()
        except SystemExit as exc:
            exit_codes.append(exc.code)
            for other in servers:
                other.should_exit = True

    await asyncio.gather(*(serve_one(server) for server in servers))
    return exit_codes[0] if exit_codes else 0


def run(settings: Settings, public: tuple[FastAPI, int], internal: tuple[FastAPI, int]) -> None:
    exit_code = asyncio.run(serve(server_configs(settings, public, internal)))
    if exit_code:
        raise SystemExit(exit_code)
