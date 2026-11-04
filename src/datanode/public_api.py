"""DataNode · API pública (puerto 9443, HTTPS).

La usa el cliente para enviar y recibir bloques. Cada petición lleva el pase del
bloque en Authorization: Bearer <pase>, firmado por el MetaServer (DA-10).
El cuerpo de los bloques es binario (application/octet-stream, DA-06).

Implementadas: health y GET /blocks/{hash} (tarea 10). PUT /blocks/{hash} verifica el
pase y sigue respondiendo 501 E_NOT_IMPLEMENTED: guardar, reenviar a los secundarios y
confirmar al MetaServer es la tarea 14.

Al arrancar, el lifespan abre el almacén y lee la clave de los pases (datanode.deps).
Se arranca con python -m datanode.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response

from datanode.auth import HashPath, require_pass
from datanode.deps import StoreDep, open_resources
from dfs_common.app import BINARY_REQUEST_BODY, create_app
from dfs_common.errors import DFSError, ErrorCode, error_responses
from dfs_common.models import BlockPass, BlockStoredResponse, HealthResponse

router = APIRouter(prefix="/api/v1")


class BlockBytesResponse(Response):
    media_type = "application/octet-stream"


@router.put(
    "/blocks/{hash}",
    status_code=201,
    response_model=BlockStoredResponse,
    tags=["blocks"],
    summary="Enviar un bloque al primario",
    description=(
        "El primario recibe el bloque en un temporal, verifica el hash, lo cifra con AES-256-GCM, "
        "hace fsync y lo renombra; luego lo reenvía a los secundarios del pase (puerto 9444; 2, o 1 si "
        "solo quedan 2 DataNodes vivos), "
        "espera hasta DFS_PRIMARY_WAIT_S, confirma al MetaServer y responde dónde quedó. "
        "400 E_BAD_REQUEST si el cuerpo pasa de DFS_BLOCK_MIB MiB. 422 E_CHECKSUM si el contenido no "
        "coincide con el hash; 422 E_REPLICAS si no logró W copias o si la confirmación dice que no "
        "cubren 2 zonas; ambos reintentables con realloc. DA-03, DA-04, DA-09."
    ),
    dependencies=[Depends(require_pass("w"))],
    openapi_extra=BINARY_REQUEST_BODY,
    responses=error_responses(
        ErrorCode.E_BAD_REQUEST,
        ErrorCode.E_AUTH,
        ErrorCode.E_FORBIDDEN,
        ErrorCode.E_CHECKSUM,
        ErrorCode.E_REPLICAS,
        ErrorCode.E_UNAVAILABLE,
        ErrorCode.E_NO_SPACE,
    ),
)
async def put_block(hash: HashPath, request: Request):
    raise DFSError(ErrorCode.E_NOT_IMPLEMENTED)


@router.get(
    "/blocks/{hash}",
    response_class=BlockBytesResponse,
    tags=["blocks"],
    summary="Leer un bloque",
    description=(
        "Descifra el bloque guardado y lo devuelve en binario, sin cifrar. Exige un pase de lectura. "
        "Si la copia está dañada (falla AES-GCM o el hash no coincide), responde 422 E_CHECKSUM, la "
        "borra y la reporta en bad_blocks de su siguiente latido; el cliente prueba la siguiente réplica. "
        "Si no tiene el bloque, 404 E_NOT_FOUND, y también lo reporta en bad_blocks: el MetaServer "
        "creía que la copia estaba aquí. DA-09, DA-10, DA-12."
    ),
    responses={
        200: {
            "description": "Contenido del bloque, sin cifrar.",
            "content": {"application/octet-stream": {"schema": {"type": "string", "format": "binary"}}},
        },
        **error_responses(ErrorCode.E_AUTH, ErrorCode.E_FORBIDDEN, ErrorCode.E_NOT_FOUND, ErrorCode.E_CHECKSUM),
    },
)
def get_block(hash: HashPath, block_pass: Annotated[BlockPass, Depends(require_pass("r"))], store: StoreDep):
    # El bloque se busca en la carpeta del usuario del pase: cada usuario solo ve sus bloques.
    return BlockBytesResponse(content=store.read(block_pass.user, hash))


@router.get(
    "/health",
    response_model=HealthResponse,
    tags=["health"],
    summary="Estado del proceso",
    description="Responde si el proceso está arriba. Sin autenticación.",
)
def health():
    return HealthResponse(status="ok")


app = create_app(
    title="DFSha · DataNode público",
    description="API pública del DataNode (puerto 9443, HTTPS). Contrato legible en docs/contratos.md.",
    lifespan=open_resources,
)
app.include_router(router)
