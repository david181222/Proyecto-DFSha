"""Recursos del MetaServer: configuración, cliente de etcd, clave del JWT y clave de los pases.

Se crean en el lifespan de cada app (pública e interna), al arrancar el servidor, nunca al
importar: así las pruebas y scripts/export_openapi.py importan las apps sin secretos ni
etcd. Cada app tiene su propio cliente de etcd. Las rutas los reciben como dependencias
(SettingsDep, EtcdDep, JwtSecretDep y PassKeyDep), y las pruebas los reemplazan con
app.dependency_overrides.
"""

from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, Request

from dfs_common.config import Settings
from dfs_common.security import read_secret
from dfs_common.tls import client_ssl_context
from metaserver.etcd import Etcd


def connect_etcd(settings: Settings) -> Etcd:
    """Cliente de etcd por mTLS, con el certificado de este nodo (docs/contratos.md, sección 8)."""
    context = client_ssl_context(settings.tls_ca_file, settings.tls_cert_file, settings.tls_key_file)
    return Etcd(settings.etcd_endpoints, context)


@asynccontextmanager
async def open_resources(app: FastAPI):
    """Lee la configuración, la clave del JWT y la de los pases, y conecta con etcd; al apagar,
    cierra la conexión.

    Si falta un archivo, la app no arranca: el proceso termina y Docker lo reinicia.
    """
    settings = Settings()
    app.state.settings = settings
    app.state.jwt_secret = read_secret(settings.jwt_secret_file)
    app.state.pass_key = read_secret(settings.pass_key_file)
    app.state.etcd = connect_etcd(settings)
    try:
        yield
    finally:
        await app.state.etcd.aclose()


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_etcd(request: Request) -> Etcd:
    return request.app.state.etcd


def get_jwt_secret(request: Request) -> bytes:
    return request.app.state.jwt_secret


def get_pass_key(request: Request) -> bytes:
    return request.app.state.pass_key


SettingsDep = Annotated[Settings, Depends(get_settings)]
EtcdDep = Annotated[Etcd, Depends(get_etcd)]
JwtSecretDep = Annotated[bytes, Depends(get_jwt_secret)]
PassKeyDep = Annotated[bytes, Depends(get_pass_key)]
