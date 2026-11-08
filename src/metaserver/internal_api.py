"""MetaServer · API interna (puerto 8444, mTLS).

Solo la usan los DataNodes, con certificado de la CA propia (DA-06, DA-11):
registro, latidos con sus órdenes y confirmación de bloques. Prefijo /api/v1/internal.

Como todos los nodos tienen un certificado de la misma CA, cada llamada se ata además a su
nodo por la IP de origen de la conexión (docs/contratos.md, sección 3): si no es la del
nodo, 403 E_FORBIDDEN.

Rutas: register, heartbeat y POST /blocks/confirm.

Al arrancar, el lifespan abre sus propios recursos, entre ellos su cliente de etcd
(metaserver.deps). Las tareas de fondo del proceso van en el lifespan de la app pública.
"""

from fastapi import APIRouter, Request

from dfs_common.app import create_app
from dfs_common.errors import ErrorCode, error_responses
from dfs_common.models import (
    BlockConfirmRequest,
    HeartbeatRequest,
    HeartbeatResponse,
    OkResponse,
    RegisterRequest,
    RegisterResponse,
)
from metaserver import blocks, datanodes
from metaserver.deps import EtcdDep, SettingsDep, open_resources
from metaserver.nodes import by_id, check_source_ip, live_nodes

router = APIRouter(prefix="/api/v1/internal", tags=["internal"])


def source_ip(request: Request) -> str:
    """La IP de origen de la conexión, la que compara check_source_ip."""
    return request.client.host if request.client else ""


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
async def register(body: RegisterRequest, request: Request, etcd: EtcdDep, settings: SettingsDep):
    return await datanodes.register_datanode(etcd, body, source_ip(request), settings)


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
async def heartbeat(body: HeartbeatRequest, request: Request, etcd: EtcdDep):
    return await datanodes.heartbeat(etcd, body, source_ip(request))


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
async def confirm_block(body: BlockConfirmRequest, request: Request, etcd: EtcdDep):
    nodes = by_id(await live_nodes(etcd))
    primary = body.stored_on[0] if body.stored_on else ""  # el primario es el primero de stored_on
    check_source_ip(source_ip(request), nodes[primary].ip if primary in nodes else None, primary)
    await blocks.confirm_block(etcd, body.user, body.hash, body.size, body.stored_on, nodes)
    return OkResponse(ok=True)


app = create_app(
    title="DFSha · MetaServer interno",
    description="API interna del MetaServer (puerto 8444, mTLS). Contrato legible en docs/contratos.md.",
    mtls=True,
    lifespan=open_resources,
)
app.include_router(router)
