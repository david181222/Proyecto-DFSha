"""Recursos del DataNode: configuración, almacén de bloques, clave de los pases y clientes HTTP.

Se crean en el lifespan de cada app, al arrancar el servidor, nunca al importar: así
las pruebas y scripts/export_openapi.py importan las apps sin secretos ni carpeta de
datos. Las rutas los reciben como dependencias (SettingsDep, StoreDep, PassKeyDep, PeersDep y
MetaClientDep), y las pruebas los reemplazan con app.dependency_overrides.
"""

from contextlib import asynccontextmanager
from typing import Annotated

import httpx
from fastapi import Depends, FastAPI, Request

from datanode.meta_client import MetaClient
from datanode.storage import BlockStore
from dfs_common.config import Settings
from dfs_common.security import read_secret
from dfs_common.tls import client_ssl_context

MIB = 1024 * 1024
NODE_KEY_BYTES = 32  # AES-256 (DA-09)

# Las dos apps del DataNode (pública e interna) corren en el mismo proceso y comparten un
# solo almacén: así los temporales se limpian una sola vez al arrancar y los bloques dañados
# quedan en una sola lista, la que manda el latido.
_store: BlockStore | None = None


def shared_store(settings: Settings) -> BlockStore:
    global _store
    if _store is None:
        node_key = read_secret(settings.node_key_file, NODE_KEY_BYTES)
        _store = BlockStore(settings.data_dir, node_key, settings.block_mib * MIB)
    return _store


@asynccontextmanager
async def open_resources(app: FastAPI):
    """Lee la configuración, la clave de cifrado y la de los pases, y abre el almacén.

    Si falta un archivo, la app no arranca: el proceso termina y Docker lo reinicia.
    """
    settings = Settings()
    app.state.settings = settings
    app.state.store = shared_store(settings)
    app.state.pass_key = read_secret(settings.pass_key_file)
    yield


@asynccontextmanager
async def open_clients(app: FastAPI):
    """Clientes HTTP de la API pública, por mTLS con el certificado del nodo (tarea 14).

    peers: hacia la API interna de los secundarios; su timeout es DFS_PRIMARY_WAIT_S, la espera
    total del primario. meta_client: hacia los MetaServers (MetaClient). Se crean una vez y se
    cierran al apagar. Va después de open_resources, que deja la configuración en app.state.
    """
    settings = app.state.settings
    context = client_ssl_context(settings.tls_ca_file, settings.tls_cert_file, settings.tls_key_file)
    app.state.peers = httpx.AsyncClient(verify=context, timeout=settings.primary_wait_s)
    app.state.meta_client = MetaClient(settings, context)
    try:
        yield
    finally:
        await app.state.peers.aclose()
        await app.state.meta_client.aclose()


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_store(request: Request) -> BlockStore:
    return request.app.state.store


def get_pass_key(request: Request) -> bytes:
    return request.app.state.pass_key


def get_peers(request: Request) -> httpx.AsyncClient:
    return request.app.state.peers


def get_meta_client(request: Request) -> MetaClient:
    return request.app.state.meta_client


SettingsDep = Annotated[Settings, Depends(get_settings)]
StoreDep = Annotated[BlockStore, Depends(get_store)]
PassKeyDep = Annotated[bytes, Depends(get_pass_key)]
PeersDep = Annotated[httpx.AsyncClient, Depends(get_peers)]
MetaClientDep = Annotated[MetaClient, Depends(get_meta_client)]
