"""DataNode · API interna (puerto 9444, mTLS).

Recibe copias de otros DataNodes: la réplica que envía el primario (DA-04) o la
copia de una re-réplica (DA-12). El bloque viaja sin cifrar dentro de mTLS y
cada DataNode lo cifra con su propia clave (DA-09). Prefijo /api/v1/internal.

La ruta no revisa certificados: uvicorn exige mTLS en este puerto (dfs_common.serve).
Al arrancar, el lifespan abre el almacén de bloques (datanode.deps).
"""

from typing import Annotated

from fastapi import APIRouter, Path, Request

from datanode.deps import SettingsDep, StoreDep, open_resources
from dfs_common.app import BINARY_REQUEST_BODY, create_app
from dfs_common.errors import ErrorCode, error_responses
from dfs_common.models import SHA256_PATTERN, USER_PATTERN, ReplicaStoredResponse

router = APIRouter(prefix="/api/v1/internal", tags=["internal"])


@router.put(
    "/blocks/{user}/{hash}",
    status_code=201,
    response_model=ReplicaStoredResponse,
    summary="Guardar una copia de un bloque",
    description=(
        "Recibe el bloque sin cifrar, verifica el hash, lo cifra con la clave de este nodo, "
        "hace fsync y lo renombra. 400 E_BAD_REQUEST si el cuerpo pasa de DFS_BLOCK_MIB MiB; "
        "422 E_CHECKSUM si el contenido no coincide con el hash. DA-03, DA-04, DA-09, DA-12."
    ),
    openapi_extra=BINARY_REQUEST_BODY,
    responses=error_responses(ErrorCode.E_BAD_REQUEST, ErrorCode.E_CHECKSUM, ErrorCode.E_NO_SPACE),
)
async def put_replica(
    user: Annotated[str, Path(pattern=USER_PATTERN, description="Dueño del bloque.")],
    hash: Annotated[str, Path(pattern=SHA256_PATTERN, description="SHA-256 del bloque sin cifrar (DA-02).")],
    request: Request,
    store: StoreDep,
    settings: SettingsDep,
):
    await store.write(user, hash, request.stream())
    return ReplicaStoredResponse(hash=hash, node_id=settings.node_id)


app = create_app(
    title="DFSha · DataNode interno",
    description="API interna del DataNode (puerto 9444, mTLS). Contrato legible en docs/contratos.md.",
    mtls=True,
    lifespan=open_resources,
)
app.include_router(router)
