"""El DataNode como primario de un bloque: guardar, reenviar a los secundarios y confirmar (tarea 14, DA-04).

PUT /blocks/{hash} hace, en este orden:

1. guarda el bloque con store.write: verifica el hash, lo cifra, hace fsync y lo publica. Si
   falla, responde ese error y no reenvía nada;
2. lee el bloque guardado y lo envía a la vez a cada secundario del pase, con PUT
   /internal/blocks/{user}/{hash} por mTLS; espera como máximo DFS_PRIMARY_WAIT_S en total y
   cuenta los que respondieron 201;
3. stored_on es este nodo más esos secundarios. Si son menos de DFS_W: 422 E_REPLICAS, sin
   confirmar, y el cliente pide realloc;
4. confirma al MetaServer, que comprueba las 2 zonas (422 E_REPLICAS si no las cubren) y escribe
   /blocks/{user}/{hash}. Cualquier otro error de la confirmación, o ningún MetaServer que
   responda: 503 E_UNAVAILABLE, y el error original queda en el log.

Copias huérfanas: las copias de un bloque que no se confirmó quedan en disco sin clave en etcd,
y la limpieza, que recorre etcd, no las ve. Es una limitación aceptada que cuesta solo espacio
en disco; si el cliente vuelve a enviar el bloque, se reescriben.
"""

import asyncio
import logging

import httpx
from fastapi.concurrency import run_in_threadpool

from datanode.meta_client import MetaClient
from datanode.storage import BlockStore
from dfs_common.config import Settings
from dfs_common.errors import DFSError, ErrorCode
from dfs_common.models import BlockPass, BlockStoredResponse

logger = logging.getLogger("dfsha")

CONFIRM_PATH = "/api/v1/internal/blocks/confirm"


async def store_block(
    store: BlockStore,
    peers: httpx.AsyncClient,
    meta: MetaClient,
    settings: Settings,
    block_pass: BlockPass,
    hash: str,
    chunks,
) -> BlockStoredResponse:
    user = block_pass.user
    await store.write(user, hash, chunks)
    # read descifra desde el disco: corre en otro hilo para no bloquear el bucle de eventos.
    data = await run_in_threadpool(store.read, user, hash)
    copies = await send_to_secondaries(peers, block_pass.secondaries or [], user, hash, data, settings.primary_wait_s)
    stored_on = [settings.node_id, *copies]
    if len(stored_on) < settings.w:
        raise DFSError(ErrorCode.E_REPLICAS, f"el bloque quedó en {len(stored_on)} nodos y DFS_W es {settings.w}")
    await _confirm(meta, {"user": user, "hash": hash, "size": len(data), "stored_on": stored_on})
    return BlockStoredResponse(hash=hash, stored_on=stored_on)


async def _confirm(meta: MetaClient, block: dict) -> None:
    """POST /internal/blocks/confirm. Un 422 E_REPLICAS (las copias no cubren 2 zonas) pasa tal cual.

    Cualquier otro error (un 403 porque DFS_NODE_IP de este nodo está mal, un 500, ningún MetaServer
    que responda) queda en el log con su código y su mensaje, y llega al cliente como 503
    E_UNAVAILABLE: el cliente no puede corregirlo, pero sí reintentar con otros destinos (realloc
    sin este primario). En el DataNode, un 403 quiere decir que el pase es inválido (sección 6).
    """
    try:
        await meta.post(CONFIRM_PATH, block)
    except DFSError as error:
        if error.code == ErrorCode.E_REPLICAS:
            raise
        logger.error("confirm del bloque %s falló con %s: %s", block["hash"], error.code.value, error.message)
        raise DFSError(ErrorCode.E_UNAVAILABLE, "no se pudo confirmar el bloque; pide otros destinos") from None


async def send_to_secondaries(
    peers: httpx.AsyncClient, addresses: list[str], user: str, hash: str, data: bytes, wait_s: float
) -> list[str]:
    """Envía el bloque a la vez a cada dirección y espera hasta wait_s en total.

    Devuelve el node_id de los que respondieron 201 a tiempo, en el orden del pase. Los que
    fallan o tardan más no cuentan; los que siguen en curso se cancelan.
    """
    if not addresses:
        return []
    sends = [asyncio.create_task(_send(peers, address, user, hash, data)) for address in addresses]
    done, pending = await asyncio.wait(sends, timeout=wait_s)
    for address, send in zip(addresses, sends):
        if send in pending:
            logger.warning("el secundario %s no respondió en %s s", address, wait_s)
            send.cancel()
    # Se espera a que terminen de cancelarse, para no dejar envíos en curso después de responder.
    await asyncio.gather(*pending, return_exceptions=True)
    return [node_id for send in sends if send in done and (node_id := send.result()) is not None]


async def _send(peers: httpx.AsyncClient, address: str, user: str, hash: str, data: bytes) -> str | None:
    """PUT del bloque a un secundario. Devuelve su node_id si respondió 201, o None."""
    url = f"https://{address}/api/v1/internal/blocks/{user}/{hash}"
    try:
        response = await peers.put(url, content=data)
        if response.status_code == 201:
            return response.json()["node_id"]
        logger.warning("el secundario %s respondió HTTP %s", address, response.status_code)
    except (httpx.HTTPError, ValueError, KeyError) as exc:
        logger.warning("no se pudo enviar el bloque al secundario %s: %r", address, exc)
    return None
