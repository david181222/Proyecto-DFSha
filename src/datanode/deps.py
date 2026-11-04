"""Recursos del DataNode: configuración y almacén de bloques.

Se crean en el lifespan de cada app, al arrancar el servidor, nunca al importar: así
las pruebas y scripts/export_openapi.py importan las apps sin secretos ni carpeta de
datos. Las rutas los reciben como dependencias (SettingsDep y StoreDep), y las pruebas
los reemplazan con app.dependency_overrides.
"""

from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, Request

from datanode.storage import BlockStore
from dfs_common.config import Settings
from dfs_common.security import read_secret

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
    """Lee la configuración y la clave de cifrado y abre el almacén.

    Si falta un archivo, la app no arranca: el proceso termina y Docker lo reinicia.
    """
    settings = Settings()
    app.state.settings = settings
    app.state.store = shared_store(settings)
    yield


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_store(request: Request) -> BlockStore:
    return request.app.state.store


SettingsDep = Annotated[Settings, Depends(get_settings)]
StoreDep = Annotated[BlockStore, Depends(get_store)]
