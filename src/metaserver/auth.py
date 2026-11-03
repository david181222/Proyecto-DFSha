"""Autenticación del MetaServer (DA-10): contraseñas, JWT, usuarios y primer administrador.

- Contraseñas con Argon2id (PasswordHasher de argon2-cffi, con sus valores por defecto).
  Calcular y verificar gasta CPU a propósito, así que corre en otro hilo
  (run_in_threadpool) para no bloquear el bucle de eventos.
- require_user y require_admin son dependencias de FastAPI: leen el JWT de
  Authorization: Bearer y devuelven sus claims (sección 7.1).
- Un usuario se guarda en /users/{user} como {password_hash, is_admin}.
"""

import logging
from typing import Annotated

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from fastapi import Depends
from fastapi.concurrency import run_in_threadpool
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from dfs_common.config import Settings
from dfs_common.errors import DFSError, ErrorCode
from dfs_common.models import JwtClaims
from dfs_common.security import read_secret, verify_jwt
from metaserver.deps import JwtSecretDep
from metaserver.etcd import Etcd, compare, op_put
from metaserver.keys import user_key

ADMIN_USER = "admin"

logger = logging.getLogger("dfsha")

# auto_error=False: si falta el token, la ruta (no FastAPI) responde 401 E_AUTH.
# Todas las rutas con JWT dependen de este mismo objeto: así el OpenAPI declara un solo esquema.
jwt_auth = HTTPBearer(
    auto_error=False,
    scheme_name="JWT",
    bearerFormat="JWT",
    description="JWT de POST /api/v1/auth/login, en Authorization: Bearer (DA-10).",
)

_hasher = PasswordHasher()


async def hash_password(password: str) -> str:
    """Hash Argon2id; empieza con $argon2id$ y trae su propia sal."""
    return await run_in_threadpool(_hasher.hash, password)


async def password_matches(password_hash: str, password: str) -> bool:
    try:
        return await run_in_threadpool(_hasher.verify, password_hash, password)
    except (VerificationError, InvalidHashError):
        return False


# --- Dependencias de las rutas ---


async def require_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(jwt_auth)],
    secret: JwtSecretDep,
) -> JwtClaims:
    """Claims del JWT de la petición. Ausente, inválido o vencido: 401 E_AUTH."""
    if credentials is None:
        raise DFSError(ErrorCode.E_AUTH, "falta el JWT en Authorization: Bearer")
    return verify_jwt(credentials.credentials, secret)


async def require_admin(claims: Annotated[JwtClaims, Depends(require_user)]) -> JwtClaims:
    """Como require_user, y además exige admin: true; si no, 403 E_FORBIDDEN."""
    if not claims.admin:
        raise DFSError(ErrorCode.E_FORBIDDEN, "esta ruta exige rol de administrador")
    return claims


CurrentUser = Annotated[JwtClaims, Depends(require_user)]


# --- Usuarios ---


async def create_user(etcd: Etcd, user: str, password: str, *, is_admin: bool = False) -> bool:
    """Crea /users/{user} solo si no existe (create_revision = 0). Devuelve si lo creó.

    Es una transacción: si varios lo intentan a la vez, solo uno lo crea.
    """
    key = user_key(user)
    value = {"password_hash": await hash_password(password), "is_admin": is_admin}
    result = await etcd.txn([compare(key, "CREATE", "EQUAL", 0)], [op_put(key, value)], [])
    return result.succeeded


async def create_first_admin(etcd: Etcd, settings: Settings) -> None:
    """Al arrancar: si DFS_ADMIN_PASSWORD_FILE tiene valor, crea el usuario admin si todavía no existe.

    Lo intentan los tres MetaServers; la transacción de create_user deja que lo cree uno solo.
    Solo admin puede ser administrador (POST /admin/users crea usuarios normales), así que
    "no existe /users/admin" es lo mismo que "no existe ningún administrador" (sección 2).
    Si etcd no responde, la excepción detiene el arranque y Docker reinicia el contenedor.

    Si admin ya existe, su contraseña no se cambia. Si no es la del archivo (por ejemplo,
    porque se regeneraron los secretos y etcd conservó sus datos), se avisa en el log.
    """
    if settings.admin_password_file is None:
        return
    password = read_secret(settings.admin_password_file).decode("utf-8").rstrip()
    if not password:
        # Un admin con contraseña vacía no podría entrar nunca, y ya no se volvería a crear.
        raise ValueError(f"el archivo {settings.admin_password_file} no tiene contraseña")
    stored = await etcd.get(user_key(ADMIN_USER))
    if stored is None:
        await create_user(etcd, ADMIN_USER, password, is_admin=True)
    elif not await password_matches(stored.json()["password_hash"], password):
        logger.warning(
            "el usuario admin ya existe y su contraseña no es la de %s; no se cambia", settings.admin_password_file
        )
