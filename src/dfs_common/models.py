"""Mensajes de las cuatro APIs de DFSha y formatos de JWT, pase y órdenes (tarea 01).

Cada modelo de petición o respuesta trae un ejemplo en json_schema_extra. Los mismos
ejemplos aparecen en docs/contratos.md y tests/test_contratos.py verifica que
coincidan y que validen contra su modelo.

Convenciones (detalle en docs/contratos.md, sección 1):
- Rutas de archivo: absolutas dentro del espacio del usuario (RF1); reglas en check_dfs_path.
- Hashes: SHA-256 del contenido sin cifrar, en hexadecimal minúscula (DA-02).
- Direcciones: "ip:puerto" con la IP privada (DA-11).
- Tamaños en bytes; versiones desde 1; la versión 0 significa "el archivo no existe".
- Instantes: enteros en milisegundos desde epoch (UTC). La excepción son iat y exp
  del JWT y exp del pase, que van en segundos.

Todos los ejemplos cuentan la misma historia: la usuaria ana reemplaza
/docs/informe.pdf (versión 3 -> 4, tres bloques) en el despliegue de DA-13:
dn1 en us-east-1a (172.31.0.10), dn2 en us-east-1b (172.31.16.10),
dn3 en us-east-1c (172.31.32.10) y dn4 en us-east-1b (172.31.16.20).
"""

from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator

SHA256_PATTERN = r"^[0-9a-f]{64}$"
# El usuario aparece en rutas HTTP, en claves de etcd y en /data/{usuario}/ (DA-02).
USER_PATTERN = r"^[a-z0-9_-]{1,32}$"
ADDRESS_PATTERN = r"^[^:\s]+:[0-9]{1,5}$"

MAX_PATH_BYTES = 1024
MAX_NAME_BYTES = 255
PATH_RULES = (
    "Ruta absoluta dentro del espacio del usuario (RF1): empieza en /, sin / al final (salvo la raíz) "
    f"y sin //; hasta {MAX_PATH_BYTES} bytes en UTF-8; cada componente mide de 1 a {MAX_NAME_BYTES} "
    "bytes, no contiene el carácter nulo y no es . ni .."
)


def check_dfs_path(path: str) -> str:
    """Valida una ruta de archivo. Si no cumple, lanza ValueError y la API responde 400 E_BAD_REQUEST."""
    if not path.startswith("/"):
        raise ValueError("la ruta debe empezar con /")
    if len(path.encode("utf-8")) > MAX_PATH_BYTES:
        raise ValueError(f"la ruta mide más de {MAX_PATH_BYTES} bytes")
    if path == "/":
        return path
    if path.endswith("/"):
        raise ValueError("la ruta no puede terminar en /")
    for name in path[1:].split("/"):
        if name == "":
            raise ValueError("la ruta no puede contener //")
        if name in (".", ".."):
            raise ValueError("la ruta no puede contener . ni ..")
        if "\0" in name:
            raise ValueError("la ruta no puede contener el carácter nulo")
        if len(name.encode("utf-8")) > MAX_NAME_BYTES:
            raise ValueError(f"un componente de la ruta mide más de {MAX_NAME_BYTES} bytes")
    return path


Sha256Hex = Annotated[str, Field(pattern=SHA256_PATTERN, description="SHA-256 en hexadecimal minúscula (DA-02)")]
UserName = Annotated[str, Field(pattern=USER_PATTERN, description="Nombre de usuario")]
DfsPath = Annotated[str, AfterValidator(check_dfs_path), Field(description=PATH_RULES)]
Address = Annotated[str, Field(pattern=ADDRESS_PATTERN, description="ip:puerto, con la IP privada (DA-11)")]
EpochMs = Annotated[int, Field(ge=0, description="Milisegundos desde epoch (UTC)")]
FileType = Literal["dir", "file"]

# Valores largos que se repiten en los ejemplos.
_H0 = "6ce5a55b446b43e4e935c44695a9a09f161e8e87d1ff2359adebc5f9ee5ee5cf"
_H1 = "b64bb9046f61749097db060781bcbbba317c08ff0ac378e9aed9a6d18217e449"
_H2 = "8de70b36bbfaaf4bd8fecc81370e32e388e0fa57cabc32a1fbd46777ac7ac937"
_H_OLD = "29124d08ab194c99b65e04622016f3306c6c39ce331241c0e089b10e66411607"
_FILE_SHA256 = "247a8174ec0c61e4f6c2f0299b27648c9ef3dc8482f7083d01c8a389976a6a76"
_JWT = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
    ".eyJzdWIiOiJhbmEiLCJhZG1pbiI6ZmFsc2UsImlhdCI6MTc5MzYyODAwMCwiZXhwIjoxNzkzNjMxNjAwfQ"
    ".jCuFONqnGHr41k8gQqELbF7cDtJhsfrCuGdn-vYMO94"
)
# Pases de lectura de la versión 4: se piden a las 14:02:00, después del commit de las 14:01:30.
_PASS_R0 = (
    "eyJ1c2VyIjoiYW5hIiwiaGFzaCI6IjZjZTVhNTViNDQ2YjQzZTRlOTM1YzQ0Njk1YTlhMDlmMTYxZThlODdkMWZmMjM1OWFkZWJjNWY5ZWU1ZWU1Y2YiLCJvcCI6InIiLCJleHAiOjE3OTM2Mjg3MjB9"
    ".z0WiI50c6AfbzmSTYB6-LFdTZU2A_zS5GxQMu4iXGKc"
)
_PASS_R1 = (
    "eyJ1c2VyIjoiYW5hIiwiaGFzaCI6ImI2NGJiOTA0NmY2MTc0OTA5N2RiMDYwNzgxYmNiYmJhMzE3YzA4ZmYwYWMzNzhlOWFlZDlhNmQxODIxN2U0NDkiLCJvcCI6InIiLCJleHAiOjE3OTM2Mjg3MjB9"
    ".2aosUSBmyey_LOS0yTROyMoPw2rPa0MY_RU8amfSLuc"
)
_PASS_R2 = (
    "eyJ1c2VyIjoiYW5hIiwiaGFzaCI6IjhkZTcwYjM2YmJmYWFmNGJkOGZlY2M4MTM3MGUzMmUzODhlMGZhNTdjYWJjMzJhMWZiZDQ2Nzc3YWM3YWM5MzciLCJvcCI6InIiLCJleHAiOjE3OTM2Mjg3MjB9"
    ".3ekV5C2jUAFMJZrsb41tDqE8q4U2BD-FH1hE1DwYnYg"
)
_PASS_W1 = (
    "eyJ1c2VyIjoiYW5hIiwiaGFzaCI6ImI2NGJiOTA0NmY2MTc0OTA5N2RiMDYwNzgxYmNiYmJhMzE3YzA4ZmYwYWMzNzhlOWFlZDlhNmQxODIxN2U0NDkiLCJvcCI6InciLCJzZWNvbmRhcmllcyI6WyIxNzIuMzEuMTYuMjA6OTQ0NCIsIjE3Mi4zMS4wLjEwOjk0NDQiXSwiZXhwIjoxNzkzNjI4NjAwfQ"
    ".cmdifTy29ib542pe296X3Ud6ZGdU0WvkgzZm4fWTDC0"
)
_PASS_W2 = (
    "eyJ1c2VyIjoiYW5hIiwiaGFzaCI6IjhkZTcwYjM2YmJmYWFmNGJkOGZlY2M4MTM3MGUzMmUzODhlMGZhNTdjYWJjMzJhMWZiZDQ2Nzc3YWM3YWM5MzciLCJvcCI6InciLCJzZWNvbmRhcmllcyI6WyIxNzIuMzEuMC4xMDo5NDQ0IiwiMTcyLjMxLjE2LjEwOjk0NDQiXSwiZXhwIjoxNzkzNjI4NjAwfQ"
    ".Euwkh7fQO8z-ga48XdTOvZBGlqRQRL8170T6T_eotMs"
)
_PASS_W2_REALLOC = (
    "eyJ1c2VyIjoiYW5hIiwiaGFzaCI6IjhkZTcwYjM2YmJmYWFmNGJkOGZlY2M4MTM3MGUzMmUzODhlMGZhNTdjYWJjMzJhMWZiZDQ2Nzc3YWM3YWM5MzciLCJvcCI6InciLCJzZWNvbmRhcmllcyI6WyIxNzIuMzEuMTYuMTA6OTQ0NCIsIjE3Mi4zMS4zMi4xMDo5NDQ0Il0sImV4cCI6MTc5MzYyODY2MH0"
    ".Df5QOig70VjvVVw_06qWo8BllA6DahuI9B4VxRR41uE"
)


def _examples(*examples: dict) -> ConfigDict:
    """Configuración de un modelo con sus ejemplos (van al OpenAPI y a docs/contratos.md)."""
    return ConfigDict(json_schema_extra={"examples": list(examples)})


# =====================================================================
# Formatos que viajan dentro de otros mensajes (DA-10, DA-12)
# =====================================================================


class JwtClaims(BaseModel):
    """Contenido del JWT del login: HS256 firmado con la clave de DFS_JWT_SECRET_FILE (DA-10)."""

    model_config = _examples({"sub": "ana", "admin": False, "iat": 1793628000, "exp": 1793631600})

    sub: UserName = Field(description="Usuario dueño del token")
    admin: bool = Field(description="Si tiene rol de administrador")
    iat: int = Field(description="Emitido en, segundos desde epoch (UTC)")
    exp: int = Field(description="Vence en, segundos desde epoch (UTC): iat + DFS_JWT_TTL_S")


class BlockPass(BaseModel):
    """Contenido del pase de bloque que el MetaServer firma y el DataNode verifica (DA-10).

    Viaja como base64url(json).base64url(hmac_sha256(json)), sin relleno "=".
    """

    model_config = _examples(
        {"user": "ana", "hash": _H0, "op": "r", "exp": 1793628720},
        {
            "user": "ana",
            "hash": _H1,
            "op": "w",
            "secondaries": ["172.31.16.20:9444", "172.31.0.10:9444"],
            "exp": 1793628600,
        },
    )

    user: UserName
    hash: Sha256Hex
    op: Literal["r", "w"] = Field(description="r = leer el bloque, w = escribirlo")
    secondaries: list[Address] | None = Field(
        None,
        min_length=1,
        max_length=2,
        description="ip:puerto internos de los secundarios: 2, o 1 si solo quedan 2 DataNodes vivos (DA-05); "
        "solo en pases de escritura (DA-04)",
    )
    exp: int = Field(description="Vence en, segundos desde epoch (UTC): emisión + DFS_PASS_TTL_S")


class NodeAddress(BaseModel):
    """Un DataNode y su dirección ip:puerto.

    Al cliente se le da el puerto público (9443); en la orden copy va el interno (9444).
    """

    node_id: str
    address: Address


class BlockRef(BaseModel):
    """Un bloque de un usuario."""

    user: UserName
    hash: Sha256Hex


class CopyOrder(BaseModel):
    """Orden de copiar un bloque a otro DataNode (re-réplica, DA-12).

    El DataNode que la recibe envía el bloque con PUT /internal/blocks/{user}/{hash} a
    target.address y solo la reporta en done_orders si el destino respondió 201.
    Entonces el MetaServer agrega target.node_id a stored_on.
    """

    model_config = _examples(
        {
            "order_id": "b7e3c2a1-4d5f-4a6b-8c9d-0e1f2a3b4c5d",
            "type": "copy",
            "user": "ana",
            "hash": _H0,
            "target": {"node_id": "dn1", "address": "172.31.0.10:9444"},
        }
    )

    order_id: str
    type: Literal["copy"]
    user: UserName
    hash: Sha256Hex
    target: NodeAddress = Field(description="DataNode destino, con su dirección interna (puerto 9444)")


class DeleteOrder(BaseModel):
    """Orden de borrar una copia de un bloque.

    La usan la limpieza, para un bloque que ya sacó de etcd (DA-02), y el líder, para la
    copia que sobra cuando un DataNode vuelve y el bloque queda con más de 3 copias vivas (DA-12).
    """

    model_config = _examples(
        {
            "order_id": "d9a5e4c3-6f7b-4c8d-8e1f-2a3b4c5d6e7f",
            "type": "delete",
            "user": "ana",
            "hash": _H_OLD,
            "written_before_ms": 1793628599000,
        }
    )

    order_id: str
    type: Literal["delete"]
    user: UserName
    hash: Sha256Hex
    written_before_ms: EpochMs = Field(
        description="Borra solo si escribió el bloque antes de este instante (ms desde epoch, UTC); "
        "ya es T - DFS_GC_CLOCK_MARGIN_S"
    )


Order = Annotated[CopyOrder | DeleteOrder, Field(discriminator="type")]


# =====================================================================
# MetaServer · API pública (8443)
# =====================================================================


class LoginRequest(BaseModel):
    model_config = _examples({"user": "ana", "password": "caballo-correcto-bateria"})

    user: UserName
    password: str = Field(min_length=1)


class LoginResponse(BaseModel):
    model_config = _examples({"token": _JWT, "expires_in": 3600})

    token: str = Field(description="JWT (ver JwtClaims)")
    expires_in: int = Field(description="Segundos hasta que vence (DFS_JWT_TTL_S)")


class MetaServerInfo(BaseModel):
    """Un MetaServer, como está en /nodes/meta/{meta_id} (DA-11).

    En GET /admin/status va como MetaServerStatus, con esta misma forma.
    """

    meta_id: str
    ip: str = Field(description="IP privada")
    public_port: int = Field(ge=1, le=65535, description="API pública (8443): cliente")
    internal_port: int = Field(ge=1, le=65535, description="API interna (8444): DataNodes")


class MetaServerListResponse(BaseModel):
    model_config = _examples(
        {
            "metaservers": [
                {"meta_id": "meta1", "ip": "172.31.0.10", "public_port": 8443, "internal_port": 8444},
                {"meta_id": "meta2", "ip": "172.31.16.10", "public_port": 8443, "internal_port": 8444},
                {"meta_id": "meta3", "ip": "172.31.32.10", "public_port": 8443, "internal_port": 8444},
            ]
        }
    )

    metaservers: list[MetaServerInfo] = Field(
        description="MetaServers vivos; el cliente usa public_port y los DataNodes internal_port"
    )


_MODIFIED_AT = "ms desde epoch (UTC) de la versión actual; en directorios, su created_at; en la raíz /, 0"


class DirEntry(BaseModel):
    name: str
    type: FileType
    size: int = Field(ge=0, description="Bytes; 0 en directorios")
    version: int = Field(ge=0, description="Versión actual; 0 en directorios")
    modified_at: EpochMs = Field(description=_MODIFIED_AT)


class LsResponse(BaseModel):
    model_config = _examples(
        {
            "entries": [
                {"name": "fotos", "type": "dir", "size": 0, "version": 0, "modified_at": 1793178900000},
                {
                    "name": "informe.pdf",
                    "type": "file",
                    "size": 20971520,
                    "version": 4,
                    "modified_at": 1793628090000,
                },
            ]
        },
        # ls sobre un archivo: ese archivo como única entrada, como ls archivo en Linux.
        {
            "entries": [
                {
                    "name": "informe.pdf",
                    "type": "file",
                    "size": 20971520,
                    "version": 4,
                    "modified_at": 1793628090000,
                }
            ]
        },
    )

    entries: list[DirEntry]


class StatResponse(BaseModel):
    model_config = _examples(
        {
            "path": "/docs/informe.pdf",
            "type": "file",
            "size": 20971520,
            "version": 4,
            "modified_at": 1793628090000,
        },
        # La raíz no se guarda en etcd y no tiene created_at.
        {"path": "/", "type": "dir", "size": 0, "version": 0, "modified_at": 0},
    )

    path: DfsPath
    type: FileType
    size: int = Field(ge=0, description="Bytes; 0 en directorios")
    version: int = Field(ge=0, description="Versión actual; 0 en directorios")
    modified_at: EpochMs = Field(description=_MODIFIED_AT)


class MkdirRequest(BaseModel):
    model_config = _examples({"path": "/docs/fotos"})

    path: DfsPath


class OkResponse(BaseModel):
    """Confirmación sin más datos."""

    model_config = _examples({"ok": True})

    ok: bool


class LockRequest(BaseModel):
    model_config = _examples({"path": "/docs/informe.pdf", "wait_s": 30})

    path: DfsPath
    # Límite de validación del contrato: va fijo, no sale de una variable de entorno.
    wait_s: int = Field(30, ge=0, le=60, description="Segundos que espera en la fila, de 0 a 60; 0 = no esperar (DA-08)")


class LockResponse(BaseModel):
    model_config = _examples({"lock_id": "694d8a1c2e3b4f05", "lease_s": 30})

    lock_id: str = Field(
        description="ID del lease de etcd de este intento, en hexadecimal; la clave del lock es "
        "/locks/{user}{path}/{lock_id}. Para el cliente es opaco"
    )
    lease_s: int = Field(description="Segundos que dura el lease si no se renueva (DFS_LOCK_LEASE_S)")


class BlockLocation(BaseModel):
    model_config = ConfigDict(validate_by_name=True)

    index: int = Field(ge=0)
    hash: Sha256Hex
    size: int = Field(ge=1)
    stored_on: list[str] = Field(
        description="Todos los DataNodes con copia según etcd, vivos o caídos; lo muestra dfs stat --blocks (DA-16)"
    )
    replicas: list[NodeAddress] = Field(
        description="DataNodes vivos con copia, primero los de la zona del cliente (X-Client-Zone); "
        "vacía si ninguno está vivo"
    )
    pass_: str = Field(alias="pass", description="Pase de lectura (op r) para este bloque")


class FileBlocksResponse(BaseModel):
    model_config = _examples(
        {
            "path": "/docs/informe.pdf",
            "version": 4,
            "size": 20971520,
            "block_size": 8388608,
            "file_sha256": _FILE_SHA256,
            "blocks": [
                {
                    "index": 0,
                    "hash": _H0,
                    "size": 8388608,
                    "stored_on": ["dn2", "dn3", "dn4"],
                    "replicas": [
                        {"node_id": "dn2", "address": "172.31.16.10:9443"},
                        {"node_id": "dn3", "address": "172.31.32.10:9443"},
                        {"node_id": "dn4", "address": "172.31.16.20:9443"},
                    ],
                    "pass": _PASS_R0,
                },
                {
                    "index": 1,
                    "hash": _H1,
                    "size": 8388608,
                    "stored_on": ["dn3", "dn4", "dn1"],
                    "replicas": [
                        {"node_id": "dn1", "address": "172.31.0.10:9443"},
                        {"node_id": "dn3", "address": "172.31.32.10:9443"},
                        {"node_id": "dn4", "address": "172.31.16.20:9443"},
                    ],
                    "pass": _PASS_R1,
                },
                {
                    "index": 2,
                    "hash": _H2,
                    "size": 4194304,
                    "stored_on": ["dn1", "dn2", "dn3"],
                    "replicas": [
                        {"node_id": "dn1", "address": "172.31.0.10:9443"},
                        {"node_id": "dn2", "address": "172.31.16.10:9443"},
                        {"node_id": "dn3", "address": "172.31.32.10:9443"},
                    ],
                    "pass": _PASS_R2,
                },
            ],
        }
    )

    path: DfsPath
    version: int = Field(ge=1)
    size: int = Field(ge=0)
    block_size: int = Field(ge=1, description="Tamaño de bloque con que se escribió (DFS_BLOCK_MIB)")
    file_sha256: Sha256Hex = Field(description="SHA-256 del archivo completo")
    blocks: list[BlockLocation]


class BlockSpec(BaseModel):
    index: int = Field(ge=0)
    hash: Sha256Hex
    size: int = Field(ge=1)


class UploadRequest(BaseModel):
    model_config = _examples(
        {
            "path": "/docs/informe.pdf",
            "mode": "w",
            "base_version": 3,
            "block_size": 8388608,
            "lock_id": "694d8a1c2e3b4f05",
            "blocks": [
                {"index": 0, "hash": _H0, "size": 8388608},
                {"index": 1, "hash": _H1, "size": 8388608},
                {"index": 2, "hash": _H2, "size": 4194304},
            ],
        }
    )

    path: DfsPath
    mode: Literal["w", "a", "rw"] = Field(
        description="Modo de open() (RF3, DA-14): w crea o reemplaza; a anexa al final; "
        "rw modifica partes reescribiendo completos los bloques que toca. a y rw sobre un archivo "
        "que no existe lo crean con base_version 0, igual que w; con rw el único offset válido es 0"
    )
    base_version: int = Field(ge=0, description="Versión sobre la que se escribe; 0 si el archivo no existe")
    block_size: int = Field(
        gt=0,
        description="Bytes por bloque (DA-03). Con w, o con a/rw sobre un archivo que no existe: "
        "DFS_BLOCK_MIB × 1048576. Con a/rw sobre un archivo que existe: el block_size de su versión "
        "actual. Si no coincide: 400 E_BAD_REQUEST",
    )
    lock_id: str
    blocks: list[BlockSpec] = Field(
        description="Bloques nuevos o cambiados de la versión que se va a publicar; índices sin repetir "
        "y ningún bloque de más de block_size bytes. Que block_size sea el correcto lo comprueba la reserva"
    )

    @model_validator(mode="after")
    def check_blocks(self) -> "UploadRequest":
        """Lo que se puede validar sin leer etcd; si falla, la API responde 400 E_BAD_REQUEST."""
        indices = [block.index for block in self.blocks]
        if len(indices) != len(set(indices)):
            raise ValueError("blocks tiene índices repetidos")
        if any(block.size > self.block_size for block in self.blocks):
            raise ValueError("un bloque de blocks mide más que block_size")
        return self


class BlockAssignment(BaseModel):
    model_config = ConfigDict(validate_by_name=True)

    index: int = Field(ge=0)
    hash: Sha256Hex
    primary: NodeAddress = Field(description="DataNode al que el cliente envía el bloque (DA-04, DA-05)")
    pass_: str = Field(alias="pass", description="Pase de escritura (op w) con los secundarios (DA-04, DA-05)")


class UploadResponse(BaseModel):
    model_config = _examples(
        {
            "upload_id": "9b2f6d1e-4c3a-4e8b-9f1d-2a7c5e8b1d43",
            "present": [0],
            "missing": [
                {
                    "index": 1,
                    "hash": _H1,
                    "primary": {"node_id": "dn3", "address": "172.31.32.10:9443"},
                    "pass": _PASS_W1,
                },
                {
                    "index": 2,
                    "hash": _H2,
                    "primary": {"node_id": "dn4", "address": "172.31.16.20:9443"},
                    "pass": _PASS_W2,
                },
            ],
        }
    )

    upload_id: str
    present: list[int] = Field(
        description="Índices cuyo bloque ya está guardado con copias vivas en 2 zonas: no se envían (DA-02)"
    )
    missing: list[BlockAssignment] = Field(description="Bloques que el cliente debe enviar")


class ReallocRequest(BaseModel):
    model_config = _examples({"indices": [2], "exclude_nodes": ["dn4"]})

    indices: list[int] = Field(min_length=1, description="Índices que hay que reenviar")
    exclude_nodes: list[str] = Field(default_factory=list, description="node_id que fallaron y no se deben usar")


class ReallocResponse(BaseModel):
    model_config = _examples(
        {
            "missing": [
                {
                    "index": 2,
                    "hash": _H2,
                    "primary": {"node_id": "dn1", "address": "172.31.0.10:9443"},
                    "pass": _PASS_W2_REALLOC,
                }
            ]
        }
    )

    missing: list[BlockAssignment]


class CommitRequest(BaseModel):
    model_config = _examples(
        {
            "base_version": 3,
            "lock_id": "694d8a1c2e3b4f05",
            "hashes": [_H0, _H1, _H2],
            "size": 20971520,
            "file_sha256": _FILE_SHA256,
        }
    )

    base_version: int = Field(ge=0)
    lock_id: str
    hashes: list[Sha256Hex] = Field(description="Todos los bloques de la versión nueva, en orden")
    size: int = Field(ge=0)
    file_sha256: Sha256Hex


class CommitResponse(BaseModel):
    model_config = _examples({"path": "/docs/informe.pdf", "version": 4})

    path: DfsPath
    version: int = Field(ge=1)


class CreateUserRequest(BaseModel):
    model_config = _examples({"user": "bruno", "password": "otra-clave-larga-de-prueba"})

    user: UserName
    password: str = Field(min_length=1)


class MetaServerStatus(MetaServerInfo):
    """Un MetaServer en /admin/status: la misma forma que en GET /cluster/metaservers."""


class DataNodeStatus(BaseModel):
    node_id: str
    address: Address | None = Field(description="ip:puerto público; null si el nodo está caído")
    zone: str | None = Field(description="null si el nodo está caído")
    free_bytes: int | None = Field(description="null si el nodo está caído")
    alive: bool = Field(description="Si su clave en /nodes/dn/ sigue viva (DA-12)")


class UnderReplicatedBlock(BaseModel):
    user: UserName
    hash: Sha256Hex
    alive_on: list[str] = Field(description="node_id de los DataNodes vivos que tienen el bloque")


class PendingOrder(BaseModel):
    node_id: str = Field(description="DataNode que debe cumplir la orden")
    order: Order


class AdminStatusResponse(BaseModel):
    model_config = _examples(
        {
            "leader": "meta2",
            "metaservers": [
                {"meta_id": "meta1", "ip": "172.31.0.10", "public_port": 8443, "internal_port": 8444},
                {"meta_id": "meta2", "ip": "172.31.16.10", "public_port": 8443, "internal_port": 8444},
                {"meta_id": "meta3", "ip": "172.31.32.10", "public_port": 8443, "internal_port": 8444},
            ],
            "datanodes": [
                {
                    "node_id": "dn1",
                    "address": "172.31.0.10:9443",
                    "zone": "us-east-1a",
                    "free_bytes": 15032385536,
                    "alive": True,
                },
                {
                    "node_id": "dn2",
                    "address": "172.31.16.10:9443",
                    "zone": "us-east-1b",
                    "free_bytes": 14495514624,
                    "alive": True,
                },
                {
                    "node_id": "dn3",
                    "address": "172.31.32.10:9443",
                    "zone": "us-east-1c",
                    "free_bytes": 15569256448,
                    "alive": True,
                },
                {"node_id": "dn4", "address": None, "zone": None, "free_bytes": None, "alive": False},
            ],
            "under_replicated": [
                {"user": "ana", "hash": _H0, "alive_on": ["dn2", "dn3"]},
                {"user": "ana", "hash": _H1, "alive_on": ["dn3", "dn1"]},
            ],
            "pending_orders": [
                {
                    "node_id": "dn2",
                    "order": {
                        "order_id": "b7e3c2a1-4d5f-4a6b-8c9d-0e1f2a3b4c5d",
                        "type": "copy",
                        "user": "ana",
                        "hash": _H0,
                        "target": {"node_id": "dn1", "address": "172.31.0.10:9444"},
                    },
                },
                {
                    "node_id": "dn3",
                    "order": {
                        "order_id": "c8f4d3b2-5e6a-4b7c-9d0e-1f2a3b4c5d6e",
                        "type": "copy",
                        "user": "ana",
                        "hash": _H1,
                        "target": {"node_id": "dn2", "address": "172.31.16.10:9444"},
                    },
                },
            ],
        }
    )

    leader: str | None = Field(description="meta_id del líder actual; null si no hay líder")
    metaservers: list[MetaServerStatus]
    datanodes: list[DataNodeStatus]
    under_replicated: list[UnderReplicatedBlock] = Field(description="Bloques con menos de 3 copias vivas")
    pending_orders: list[PendingOrder]


class HealthResponse(BaseModel):
    model_config = _examples({"status": "ok"})

    status: Literal["ok"]


# =====================================================================
# MetaServer · API interna (8444, mTLS)
# =====================================================================


class RegisterRequest(BaseModel):
    model_config = _examples(
        {
            "node_id": "dn1",
            "disk_id": "5f0c7e2a-9d41-4b8e-a3c6-1e2f3a4b5c6d",
            "ip": "172.31.0.10",
            "public_port": 9443,
            "internal_port": 9444,
            "zone": "us-east-1a",
            "free_bytes": 15032385536,
        }
    )

    node_id: str
    disk_id: str = Field(
        min_length=1,
        description="Identificador del disco de datos: el DataNode lo crea la primera vez que arranca con "
        "DFS_DATA_DIR vacía y lo guarda en DFS_DATA_DIR/.disk_id (el punto evita chocar con la carpeta de "
        "un usuario). Si la carpeta tiene bloques pero no .disk_id, el DataNode no arranca. Si el valor "
        "cambia, el disco es nuevo y no tiene bloques",
    )
    ip: str = Field(description="IP privada del DataNode (DFS_NODE_IP)")
    public_port: int = Field(ge=1, le=65535)
    internal_port: int = Field(ge=1, le=65535)
    zone: str
    free_bytes: int = Field(ge=0)


class RegisterResponse(BaseModel):
    model_config = _examples({"lease_id": "694d8a1c2e3b4f12", "heartbeat_s": 5})

    lease_id: str = Field(description="Lease de etcd al que queda atada /nodes/dn/{node_id}")
    heartbeat_s: int = Field(description="Cada cuántos segundos enviar el latido (DFS_HEARTBEAT_S)")


class HeartbeatRequest(BaseModel):
    model_config = _examples(
        {
            "node_id": "dn2",
            "lease_id": "694d8a1c2e3b4f13",
            "free_bytes": 14495514624,
            "done_orders": ["a6d2b1f0-3c4e-4f5a-9b8c-7d6e5f4a3b2c"],
            "bad_blocks": [{"user": "ana", "hash": _H2}],
        }
    )

    node_id: str
    lease_id: str
    free_bytes: int = Field(ge=0)
    done_orders: list[str] = Field(
        default_factory=list,
        description="order_id cumplidos desde el latido anterior; una copy solo si el destino respondió 201",
    )
    bad_blocks: list[BlockRef] = Field(
        default_factory=list,
        description="Copias que el DataNode encontró dañadas (y borró) o que debía tener y no encontró; "
        "el MetaServer quita este nodo de su stored_on",
    )


class HeartbeatResponse(BaseModel):
    model_config = _examples(
        {
            "orders": [
                {
                    "order_id": "b7e3c2a1-4d5f-4a6b-8c9d-0e1f2a3b4c5d",
                    "type": "copy",
                    "user": "ana",
                    "hash": _H0,
                    "target": {"node_id": "dn1", "address": "172.31.0.10:9444"},
                },
                {
                    "order_id": "d9a5e4c3-6f7b-4c8d-8e1f-2a3b4c5d6e7f",
                    "type": "delete",
                    "user": "ana",
                    "hash": _H_OLD,
                    "written_before_ms": 1793628599000,
                },
            ]
        }
    )

    orders: list[Order] = Field(description="Órdenes pendientes de este DataNode (DA-12)")


class BlockConfirmRequest(BaseModel):
    model_config = _examples(
        {"user": "ana", "hash": _H1, "size": 8388608, "stored_on": ["dn3", "dn4", "dn1"]}
    )

    user: UserName
    hash: Sha256Hex
    size: int = Field(ge=1)
    stored_on: list[str] = Field(description="node_id donde quedó el bloque, empezando por el primario")


# =====================================================================
# DataNode · API pública (9443) e interna (9444)
# =====================================================================


class BlockStoredResponse(BaseModel):
    model_config = _examples({"hash": _H1, "stored_on": ["dn3", "dn4", "dn1"]})

    hash: Sha256Hex
    stored_on: list[str] = Field(description="node_id donde quedó el bloque, empezando por el primario")


class ReplicaStoredResponse(BaseModel):
    model_config = _examples({"hash": _H1, "node_id": "dn4"})

    hash: Sha256Hex
    node_id: str = Field(description="DataNode que guardó la copia")
