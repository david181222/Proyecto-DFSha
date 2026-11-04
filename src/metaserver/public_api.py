"""MetaServer · API pública (puerto 8443, HTTPS).

La usan el cliente dfs y, para descubrir MetaServers, los DataNodes (DA-11).
Todas las rutas cuelgan de /api/v1 y llevan Authorization: Bearer <JWT>, salvo
login, la lista de MetaServers y health.

Toda ruta de archivo se resuelve dentro del espacio del usuario del token, así que
aquí no existe "ruta de otro usuario". 403 E_FORBIDDEN solo aparece en /admin sin
rol de administrador y con un lock_id o upload_id de otro usuario.

Implementadas: health, login y POST /admin/users (tarea 07); ls, stat, mkdir, rmdir
y rm (tarea 08); y pedir, renovar y liberar locks (tarea 11). Las demás son stubs:
tienen su firma, su modelo de respuesta y sus errores, pero responden 501 E_NOT_IMPLEMENTED.

Al arrancar, el lifespan conecta con etcd, lee la clave del JWT y crea el primer
administrador (metaserver.deps y metaserver.auth). Se arranca con python -m metaserver.
"""

from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import APIRouter, Depends, FastAPI, Header, Path, Query
from pydantic import AfterValidator

from dfs_common.app import create_app
from dfs_common.errors import DFSError, ErrorCode, error_responses
from dfs_common.models import (
    PATH_RULES,
    AdminStatusResponse,
    CommitRequest,
    CommitResponse,
    CreateUserRequest,
    FileBlocksResponse,
    HealthResponse,
    LockRequest,
    LockResponse,
    LoginRequest,
    LoginResponse,
    LsResponse,
    MetaServerListResponse,
    MkdirRequest,
    OkResponse,
    ReallocRequest,
    ReallocResponse,
    StatResponse,
    UploadRequest,
    UploadResponse,
    check_dfs_path,
)
from dfs_common.security import create_jwt
from metaserver import auth, fs, locks
from metaserver.auth import CurrentUser, jwt_auth, require_admin
from metaserver.deps import EtcdDep, JwtSecretDep, SettingsDep, open_resources
from metaserver.keys import user_key

# Las rutas que todavía son stubs declaran el JWT así; las implementadas lo exigen con
# require_user o require_admin, que dependen del mismo jwt_auth (el OpenAPI no cambia).
JWT = [Depends(jwt_auth)]

# Una ruta inválida responde 400 E_BAD_REQUEST (check_dfs_path).
PathQuery = Annotated[
    str,
    AfterValidator(check_dfs_path),
    Query(description=PATH_RULES, examples=["/docs/informe.pdf"]),
]


def check_not_root(path: str) -> str:
    """La raíz / existe siempre: rmdir / (y rm /, porque es un directorio) responden 400."""
    if path == "/":
        raise ValueError("la raíz / no se puede borrar")
    return path


RemovablePathQuery = Annotated[
    str,
    AfterValidator(check_dfs_path),
    AfterValidator(check_not_root),
    Query(description=f"{PATH_RULES}. No puede ser la raíz /.", examples=["/docs/informe.pdf"]),
]

LockIdPath = Annotated[str, Path(description="lock_id que entregó POST /locks.", examples=["694d8a1c2e3b4f05"])]
UploadIdPath = Annotated[
    str,
    Path(description="upload_id que entregó POST /uploads.", examples=["9b2f6d1e-4c3a-4e8b-9f1d-2a7c5e8b1d43"]),
]

router = APIRouter(prefix="/api/v1")


# --- Autenticación y descubrimiento ---


@router.post(
    "/auth/login",
    response_model=LoginResponse,
    tags=["auth"],
    summary="Iniciar sesión",
    description="Verifica la contraseña (Argon2id) y entrega un JWT que dura DFS_JWT_TTL_S. Sin autenticación. DA-10.",
    responses=error_responses(ErrorCode.E_BAD_REQUEST, ErrorCode.E_AUTH, ErrorCode.E_UNAVAILABLE),
)
async def login(body: LoginRequest, etcd: EtcdDep, settings: SettingsDep, secret: JwtSecretDep):
    stored = await etcd.get(user_key(body.user))
    user = stored.json() if stored else None
    # Usuario inexistente y contraseña mala dan la misma respuesta: no se revela cuál falló.
    if user is None or not await auth.password_matches(user["password_hash"], body.password):
        raise DFSError(ErrorCode.E_AUTH, "usuario o contraseña incorrectos")
    token = create_jwt(body.user, user["is_admin"], secret, settings.jwt_ttl_s)
    return LoginResponse(token=token, expires_in=settings.jwt_ttl_s)


@router.get(
    "/cluster/metaservers",
    response_model=MetaServerListResponse,
    tags=["cluster"],
    summary="Listar MetaServers vivos",
    description=(
        "Lista actual de MetaServers vivos, leída de /nodes/meta/ en etcd, dentro del objeto "
        "{metaservers}. El cliente usa public_port y los DataNodes internal_port. Sin autenticación. DA-11."
    ),
    responses=error_responses(ErrorCode.E_UNAVAILABLE),
)
def list_metaservers():
    raise DFSError(ErrorCode.E_NOT_IMPLEMENTED)


# --- Sistema de archivos (RF1) ---


@router.get(
    "/fs/ls",
    response_model=LsResponse,
    tags=["fs"],
    summary="Listar un directorio",
    description=(
        "Entradas del directorio con nombre, tipo, tamaño, versión y fecha. La raíz / existe siempre. "
        "Sobre un archivo devuelve ese archivo como única entrada, como ls archivo en Linux. RF1."
    ),
    responses=error_responses(
        ErrorCode.E_BAD_REQUEST,
        ErrorCode.E_AUTH,
        ErrorCode.E_NOT_FOUND,
        ErrorCode.E_UNAVAILABLE,
    ),
)
async def ls(path: PathQuery, claims: CurrentUser, etcd: EtcdDep):
    return await fs.ls(etcd, claims.sub, path)


@router.get(
    "/fs/stat",
    response_model=StatResponse,
    tags=["fs"],
    summary="Consultar un archivo o directorio",
    description=(
        "Tipo, tamaño, versión actual y fecha. Un directorio tiene tamaño 0, versión 0 y su fecha de "
        "creación; la raíz /, que no se guarda en etcd, tiene modified_at 0. RF1."
    ),
    responses=error_responses(
        ErrorCode.E_BAD_REQUEST,
        ErrorCode.E_AUTH,
        ErrorCode.E_NOT_FOUND,
        ErrorCode.E_UNAVAILABLE,
    ),
)
async def stat(path: PathQuery, claims: CurrentUser, etcd: EtcdDep):
    return await fs.stat(etcd, claims.sub, path)


@router.post(
    "/fs/mkdir",
    response_model=OkResponse,
    tags=["fs"],
    summary="Crear un directorio",
    description=(
        "Crea el directorio con una transacción que exige que la clave no exista y que el padre no "
        "haya cambiado (docs/contratos.md, sección 8). No crea los padres: si el padre no existe o es "
        "un archivo, 404 E_NOT_FOUND. Si la ruta ya existe (incluida la raíz /), 409 E_EXISTS. RF1."
    ),
    responses=error_responses(
        ErrorCode.E_BAD_REQUEST,
        ErrorCode.E_AUTH,
        ErrorCode.E_NOT_FOUND,
        ErrorCode.E_EXISTS,
        ErrorCode.E_UNAVAILABLE,
    ),
)
async def mkdir(body: MkdirRequest, claims: CurrentUser, etcd: EtcdDep):
    await fs.mkdir(etcd, claims.sub, body.path)
    return OkResponse(ok=True)


@router.delete(
    "/fs/rmdir",
    response_model=OkResponse,
    tags=["fs"],
    summary="Borrar un directorio vacío",
    description=(
        "Borra el directorio con una transacción que exige que no haya cambiado y que no tenga "
        "claves debajo (docs/contratos.md, sección 8); 409 E_NOT_EMPTY si tiene entradas. "
        "400 E_BAD_REQUEST si la ruta es un archivo o la raíz /, que existe siempre. RF1."
    ),
    responses=error_responses(
        ErrorCode.E_BAD_REQUEST,
        ErrorCode.E_AUTH,
        ErrorCode.E_NOT_FOUND,
        ErrorCode.E_NOT_EMPTY,
        ErrorCode.E_UNAVAILABLE,
    ),
)
async def rmdir(path: RemovablePathQuery, claims: CurrentUser, etcd: EtcdDep):
    await fs.rmdir(etcd, claims.sub, path)
    return OkResponse(ok=True)


@router.delete(
    "/fs/rm",
    response_model=OkResponse,
    tags=["fs"],
    summary="Borrar un archivo",
    description=(
        "Toma internamente el lock del archivo con espera 0 (423 E_LOCKED si está ocupado); el cliente no "
        "llama a /locks para borrar. La versión actual pasa a retenida (replaced_at) y la limpieza la quita "
        "cuando vence DFS_RETENTION_S (DA-02, DA-14); un lector que ya la tenía abierta termina con los "
        "pases que tiene, pero no puede renovarlos porque la ruta ya no existe. "
        "400 E_BAD_REQUEST si la ruta es un directorio, "
        "incluida la raíz /. RF1."
    ),
    responses=error_responses(
        ErrorCode.E_BAD_REQUEST,
        ErrorCode.E_AUTH,
        ErrorCode.E_NOT_FOUND,
        ErrorCode.E_LOCKED,
        ErrorCode.E_UNAVAILABLE,
    ),
)
async def rm(path: RemovablePathQuery, claims: CurrentUser, etcd: EtcdDep, settings: SettingsDep):
    await fs.rm(etcd, claims.sub, path, settings.lock_lease_s)
    return OkResponse(ok=True)


# --- Locks (DA-08) ---


@router.post(
    "/locks",
    response_model=LockResponse,
    tags=["locks"],
    summary="Tomar el lock de un archivo",
    description=(
        "Lock de etcd con su propio lease de DFS_LOCK_LEASE_S: los escritores esperan en fila, "
        "en orden de llegada, hasta wait_s segundos (de 0 a 60; 30 si no se indica). Si la espera "
        "se agota o era 0, responde 423 E_LOCKED y revoca el lease del intento. Si la ruta es un "
        "directorio, 400 E_BAD_REQUEST. La ruta no tiene que existir todavía: con w se toma antes de "
        "crear el archivo, y el directorio padre se comprueba en la reserva y en el commit. DA-08."
    ),
    responses=error_responses(
        ErrorCode.E_BAD_REQUEST,
        ErrorCode.E_AUTH,
        ErrorCode.E_LOCKED,
        ErrorCode.E_UNAVAILABLE,
    ),
)
async def acquire_lock(body: LockRequest, claims: CurrentUser, etcd: EtcdDep, settings: SettingsDep):
    lock_id = await locks.acquire_lock(
        etcd,
        claims.sub,
        body.path,
        wait_s=body.wait_s,
        lease_s=settings.lock_lease_s,
        renew_s=settings.lock_renew_s,
    )
    return LockResponse(lock_id=lock_id, lease_s=settings.lock_lease_s)


@router.put(
    "/locks/{lock_id}",
    response_model=LockResponse,
    tags=["locks"],
    summary="Renovar el lease del lock",
    description=(
        "Renueva el lease del lock; el cliente lo hace cada DFS_LOCK_RENEW_S. Si el lock ya venció, "
        "409 E_LOCK_LOST: el cliente lo perdió y empieza de nuevo. DA-08."
    ),
    responses=error_responses(
        ErrorCode.E_AUTH,
        ErrorCode.E_FORBIDDEN,
        ErrorCode.E_LOCK_LOST,
        ErrorCode.E_UNAVAILABLE,
    ),
)
async def renew_lock(lock_id: LockIdPath, claims: CurrentUser, etcd: EtcdDep, settings: SettingsDep):
    await locks.renew_lock(etcd, lock_id, claims.sub)
    return LockResponse(lock_id=lock_id, lease_s=settings.lock_lease_s)


@router.delete(
    "/locks/{lock_id}",
    response_model=OkResponse,
    tags=["locks"],
    summary="Liberar el lock",
    description=(
        "Libera el lock revocando su lease; entra el siguiente de la fila. Es idempotente: si el lock "
        "ya no existe, también responde 200. DA-08."
    ),
    responses=error_responses(ErrorCode.E_AUTH, ErrorCode.E_FORBIDDEN, ErrorCode.E_UNAVAILABLE),
)
async def release_lock(lock_id: LockIdPath, claims: CurrentUser, etcd: EtcdDep):
    await locks.release_lock(etcd, lock_id, claims.sub)
    return OkResponse(ok=True)


# --- Lectura y escritura de archivos (RF2, RF3) ---


@router.get(
    "/files/blocks",
    response_model=FileBlocksResponse,
    tags=["files"],
    summary="Ubicar los bloques de un archivo",
    description=(
        "Manifiesto de una versión (la actual si no se indica). Por bloque: stored_on, todos los DataNodes "
        "con copia según etcd; replicas, los vivos, primero los de la zona de X-Client-Zone (vacía si "
        "ninguno está vivo); y un pase de lectura. Una versión reemplazada se puede leer "
        "durante DFS_RETENTION_S (DA-14); después, 404 E_NOT_FOUND y el cliente reabre el archivo. Si la "
        "ruta es un directorio, 400 E_BAD_REQUEST. Para renovar pases vencidos, se vuelve a pedir con la "
        "misma version. DA-05, DA-10, DA-11, DA-14."
    ),
    dependencies=JWT,
    responses=error_responses(
        ErrorCode.E_BAD_REQUEST,
        ErrorCode.E_AUTH,
        ErrorCode.E_NOT_FOUND,
        ErrorCode.E_UNAVAILABLE,
    ),
)
def file_blocks(
    path: PathQuery,
    version: Annotated[int | None, Query(ge=1, description="Versión a leer; si falta, la actual.")] = None,
    x_client_zone: Annotated[
        str | None,
        Header(alias="X-Client-Zone", description="Zona del cliente (DFS_ZONE).", examples=["us-east-1a"]),
    ] = None,
):
    raise DFSError(ErrorCode.E_NOT_IMPLEMENTED)


@router.post(
    "/uploads",
    response_model=UploadResponse,
    tags=["uploads"],
    summary="Reservar una subida",
    description=(
        "Recibe los bloques nuevos o cambiados y responde cuáles ya están guardados con copias vivas "
        "en 2 zonas (present) y, para los que faltan, el DataNode primario y un pase de escritura con "
        "los secundarios (2, o 1 si solo quedan 2 DataNodes vivos). 503 E_UNAVAILABLE si no hay al "
        "menos DFS_W DataNodes vivos que cubran 2 zonas; 507 E_NO_SPACE si los hay pero sin espacio. "
        "400 E_BAD_REQUEST si blocks repite un índice o trae un bloque mayor que block_size. "
        "Exige el lock del archivo (modos w, a y rw): si el lock_id venció o es de otro archivo, "
        "409 E_LOCK_LOST. /uploads/{upload_id} queda atada al lease de ese lock y desaparece sola si "
        "vence. 400 E_BAD_REQUEST si la escritura deja un hueco, si la ruta es un directorio o si "
        "block_size no es el que corresponde; 404 E_NOT_FOUND si el directorio padre no existe o es "
        "un archivo. a y rw sobre un archivo que no existe lo crean con base_version 0, igual que w. "
        "DA-02, DA-03, DA-04, DA-05, DA-14."
    ),
    dependencies=JWT,
    responses=error_responses(
        ErrorCode.E_BAD_REQUEST,
        ErrorCode.E_AUTH,
        ErrorCode.E_FORBIDDEN,
        ErrorCode.E_NOT_FOUND,
        ErrorCode.E_VERSION_CONFLICT,
        ErrorCode.E_LOCK_LOST,
        ErrorCode.E_UNAVAILABLE,
        ErrorCode.E_NO_SPACE,
    ),
)
def create_upload(body: UploadRequest):
    raise DFSError(ErrorCode.E_NOT_IMPLEMENTED)


@router.post(
    "/uploads/{upload_id}/realloc",
    response_model=ReallocResponse,
    tags=["uploads"],
    summary="Pedir otros destinos para bloques que fallaron",
    description=(
        "Asigna un primario nuevo, sin los nodos de exclude_nodes, a los bloques que recibieron "
        "un error reintentable. El cliente reenvía el mismo bloque, con el mismo hash. Con los "
        "mismos índices y exclude_nodes vacío sirve para renovar pases de escritura vencidos. Sin al "
        "menos DFS_W DataNodes vivos que cubran 2 zonas (sin contar exclude_nodes), 503 E_UNAVAILABLE. "
        "DA-04, DA-05, DA-10."
    ),
    dependencies=JWT,
    responses=error_responses(
        ErrorCode.E_BAD_REQUEST,
        ErrorCode.E_AUTH,
        ErrorCode.E_FORBIDDEN,
        ErrorCode.E_NOT_FOUND,
        ErrorCode.E_UNAVAILABLE,
        ErrorCode.E_NO_SPACE,
    ),
)
def realloc_upload(upload_id: UploadIdPath, body: ReallocRequest):
    raise DFSError(ErrorCode.E_NOT_IMPLEMENTED)


@router.post(
    "/uploads/{upload_id}/commit",
    response_model=CommitResponse,
    tags=["uploads"],
    summary="Publicar la versión nueva",
    description=(
        "Transacción de etcd que publica la versión nueva solo si la versión base sigue siendo la "
        "actual (si no, 409 E_VERSION_CONFLICT) y la clave del lock sigue existiendo y es la de ese "
        "archivo (si no, 409 E_LOCK_LOST). Si algún bloque no tiene copias vivas en 2 zonas, "
        "422 E_REPLICAS: el cliente vuelve a llamar POST /uploads con la misma lista y envía los "
        "bloques que falten. 400 E_BAD_REQUEST si hashes y size no forman un archivo sin huecos o si "
        "a o rw lo truncan (docs/contratos.md, sección 2). 404 E_NOT_FOUND si el directorio padre "
        "ya no existe o es un archivo. "
        "Con base_version 0 crea la entrada con una transacción que exige que la clave no exista y "
        "que el padre no haya cambiado (docs/contratos.md, sección 8). DA-02, DA-08, G1, G4."
    ),
    dependencies=JWT,
    responses=error_responses(
        ErrorCode.E_BAD_REQUEST,
        ErrorCode.E_AUTH,
        ErrorCode.E_FORBIDDEN,
        ErrorCode.E_NOT_FOUND,
        ErrorCode.E_VERSION_CONFLICT,
        ErrorCode.E_LOCK_LOST,
        ErrorCode.E_REPLICAS,
        ErrorCode.E_UNAVAILABLE,
    ),
)
def commit_upload(upload_id: UploadIdPath, body: CommitRequest):
    raise DFSError(ErrorCode.E_NOT_IMPLEMENTED)


# --- Administración (DA-10, DA-16) ---


@router.post(
    "/admin/users",
    response_model=OkResponse,
    tags=["admin"],
    summary="Crear un usuario",
    description="Crea un usuario con la contraseña guardada en Argon2id. Requiere JWT con admin=true. DA-10.",
    dependencies=[Depends(require_admin)],
    responses=error_responses(
        ErrorCode.E_BAD_REQUEST,
        ErrorCode.E_AUTH,
        ErrorCode.E_FORBIDDEN,
        ErrorCode.E_EXISTS,
        ErrorCode.E_UNAVAILABLE,
    ),
)
async def create_user(body: CreateUserRequest, etcd: EtcdDep):
    if not await auth.create_user(etcd, body.user, body.password):
        raise DFSError(ErrorCode.E_EXISTS, f"el usuario {body.user} ya existe")
    return OkResponse(ok=True)


@router.get(
    "/admin/status",
    response_model=AdminStatusResponse,
    tags=["admin"],
    summary="Estado del sistema",
    description=(
        "Líder, MetaServers, DataNodes con zona y estado, bloques con menos de 3 copias y órdenes "
        "pendientes. Requiere JWT con admin=true. DA-16."
    ),
    dependencies=JWT,
    responses=error_responses(ErrorCode.E_AUTH, ErrorCode.E_FORBIDDEN, ErrorCode.E_UNAVAILABLE),
)
def admin_status():
    raise DFSError(ErrorCode.E_NOT_IMPLEMENTED)


@router.get(
    "/health",
    response_model=HealthResponse,
    tags=["health"],
    summary="Estado del proceso",
    description="Responde si el proceso está arriba. Sin autenticación.",
)
def health():
    return HealthResponse(status="ok")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Al arrancar: recursos (configuración, etcd, clave del JWT) y primer administrador (sección 2)."""
    async with open_resources(app):
        await auth.create_first_admin(app.state.etcd, app.state.settings)
        yield


app = create_app(
    title="DFSha · MetaServer público",
    description="API pública del MetaServer (puerto 8443, HTTPS). Contrato legible en docs/contratos.md.",
    lifespan=lifespan,
)
app.include_router(router)
