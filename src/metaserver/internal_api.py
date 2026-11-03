"""MetaServer · API interna (puerto 8444, mTLS).

Solo la usan los DataNodes, con certificado de la CA propia (DA-06, DA-11):
registro, latidos con sus órdenes y confirmación de bloques. Prefijo /api/v1/internal.

Por ahora son stubs que responden 501 E_NOT_IMPLEMENTED.
"""

from fastapi import APIRouter

from dfs_common.app import create_app
from dfs_common.errors import DFSError, ErrorCode, error_responses
from dfs_common.models import (
    BlockConfirmRequest,
    HeartbeatRequest,
    HeartbeatResponse,
    OkResponse,
    RegisterRequest,
    RegisterResponse,
)

router = APIRouter(prefix="/api/v1/internal", tags=["internal"])


@router.post(
    "/register",
    response_model=RegisterResponse,
    summary="Registrar un DataNode",
    description=(
        "El DataNode se registra al arrancar con su IP privada (DFS_NODE_IP), puertos, zona y espacio libre. "
        "El MetaServer guarda /nodes/dn/{node_id} atada a un lease de DFS_NODE_LEASE_S y responde "
        "cada cuánto enviar el latido. Si disk_id no es el que tenía guardado en /nodes/disk/{node_id}, "
        "el disco es nuevo: primero quita el nodo de todos los stored_on y después guarda el disk_id "
        "nuevo. La IP de origen debe ser la del cuerpo y, si node_id ya está vivo, la que tiene "
        "registrada; si no, 403 E_FORBIDDEN. DA-11, DA-12."
    ),
    responses=error_responses(ErrorCode.E_BAD_REQUEST, ErrorCode.E_FORBIDDEN, ErrorCode.E_UNAVAILABLE),
)
def register(body: RegisterRequest):
    raise DFSError(ErrorCode.E_NOT_IMPLEMENTED)


@router.post(
    "/heartbeat",
    response_model=HeartbeatResponse,
    summary="Latido de un DataNode",
    description=(
        "Renueva el lease del nodo, borra las órdenes cumplidas (done_orders) y responde las "
        "pendientes de /orders/{node_id}/. Por cada copy cumplida agrega target.node_id a stored_on; "
        "por cada bloque de bad_blocks (copia dañada o que no encontró) quita este nodo de stored_on, "
        "y la re-réplica repone la copia. "
        "Si el lease ya venció, 404 E_NOT_FOUND y el DataNode se registra de nuevo. La IP de origen "
        "debe ser la registrada para node_id; si no, 403 E_FORBIDDEN. DA-12."
    ),
    responses=error_responses(
        ErrorCode.E_BAD_REQUEST, ErrorCode.E_FORBIDDEN, ErrorCode.E_NOT_FOUND, ErrorCode.E_UNAVAILABLE
    ),
)
def heartbeat(body: HeartbeatRequest):
    raise DFSError(ErrorCode.E_NOT_IMPLEMENTED)


@router.post(
    "/blocks/confirm",
    response_model=OkResponse,
    summary="Confirmar dónde quedó un bloque",
    description=(
        "Lo llama el DataNode primario cuando el bloque quedó en W nodos, antes de responderle al "
        "cliente. El MetaServer busca en /nodes/dn/ la zona de cada nodo de stored_on (un nodo sin "
        "clave, caído, no cuenta). Si las copias no cubren 2 zonas, responde 422 E_REPLICAS y no "
        "escribe nada; el primario le pasa ese error al cliente. Si no, escribe /blocks/{user}/{hash}, "
        "y si ya existía une las dos listas stored_on sin repetir. La IP de origen debe ser la del "
        "primario (el primer nodo de stored_on); si no, 403 E_FORBIDDEN. DA-04, DA-05."
    ),
    responses=error_responses(
        ErrorCode.E_BAD_REQUEST, ErrorCode.E_FORBIDDEN, ErrorCode.E_REPLICAS, ErrorCode.E_UNAVAILABLE
    ),
)
def confirm_block(body: BlockConfirmRequest):
    raise DFSError(ErrorCode.E_NOT_IMPLEMENTED)


app = create_app(
    title="DFSha · MetaServer interno",
    description="API interna del MetaServer (puerto 8444, mTLS). Contrato legible en docs/contratos.md.",
    mtls=True,
)
app.include_router(router)
