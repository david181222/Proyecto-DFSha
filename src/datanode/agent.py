"""El DataNode vivo: registro, latidos y órdenes del líder (tarea 15; DA-02, DA-11, DA-12).

El lifespan de la app pública crea un NodeAgent y corre dos tareas de fondo (nada arranca al
importar):

- heartbeat_loop: en cada vuelta, si no está registrado, POST /internal/register; si ya lo está,
  POST /internal/heartbeat. Después espera el heartbeat_s que respondió el registro (antes del
  primer registro, DFS_HEARTBEAT_S). Si ningún MetaServer responde, lo deja en el log y lo
  intenta en la siguiente vuelta: el DataNode no se cae por eso y sigue sirviendo lecturas.
- order_worker: cumple de a una las órdenes que llegan en la respuesta del latido, sin frenar
  los latidos.

Latido: lleva free_bytes, done_orders (órdenes cumplidas) y bad_blocks (copias que faltaron o
estaban dañadas al leerlas; las anota el almacén). Las dos listas se acumulan aquí y solo se
vacían cuando un latido responde 200, y solo en lo que ese latido envió: así no se pierde nada
si un latido falla. Si el latido responde 404, el lease venció y el nodo se registra de nuevo.

Órdenes (contrato, sección 7.3). El MetaServer reenvía cada orden en todos los latidos hasta que
llega en done_orders, así que seen guarda las que están en cola, en curso o cumplidas sin
reportar, y una orden repetida no se ejecuta dos veces.
- copy: lee el bloque y lo envía con PUT /internal/blocks/{user}/{hash} a target.address, por
  mTLS. Solo se reporta si el destino respondió 201. Si falla, se olvida y se reintenta cuando
  la orden vuelva a llegar. Si la copia local falta o está dañada, store.read ya la anota en
  bad_blocks y la orden no se reporta: el líder elige otro origen.
- delete: borra la copia solo si se escribió antes de written_before_ms. Se reporta cumplida
  haya borrado o no: si la copia es más nueva, conservarla es la regla (DA-02, Figura 1).
- Un tipo de orden desconocido se deja en el log y se ignora.

El disco no bloquea el bucle de eventos: store.read y store.delete_if_older corren en un hilo.

disk_id (contrato, sección 3): el DataNode lo crea la primera vez que arranca con DFS_DATA_DIR
vacía y lo guarda en DFS_DATA_DIR/.disk_id. Si la carpeta tiene bloques pero no .disk_id, no
arranca: alguien tocó el disco a mano y hay que decidir qué hacer con esos bloques.
"""

import asyncio
import logging
import os
import uuid
from pathlib import Path

import httpx
from pydantic import TypeAdapter, ValidationError

from datanode.meta_client import MetaClient
from datanode.primary import send_block
from datanode.storage import BlockStore
from dfs_common.config import Settings
from dfs_common.errors import DFSError, ErrorCode
from dfs_common.models import CopyOrder, DeleteOrder, Order, RegisterResponse

logger = logging.getLogger("dfsha")

REGISTER_PATH = "/api/v1/internal/register"
HEARTBEAT_PATH = "/api/v1/internal/heartbeat"
DISK_ID_FILE = ".disk_id"  # el punto evita chocar con la carpeta de un usuario

_ORDER = TypeAdapter(Order)


def load_disk_id(data_dir: Path) -> str:
    """Lee DFS_DATA_DIR/.disk_id, o lo crea si la carpeta no tiene bloques."""
    path = Path(data_dir) / DISK_ID_FILE
    if path.exists():
        disk_id = path.read_text(encoding="utf-8").strip()
        if not disk_id:
            raise RuntimeError(f"{path} está vacío: el DataNode no arranca")
        return disk_id
    if any(block.is_file() for block in Path(data_dir).glob("*/*")):
        raise RuntimeError(
            f"{data_dir} tiene bloques pero no {DISK_ID_FILE}: el DataNode no arranca. Alguien tocó el disco a "
            "mano; hay que decidir qué hacer con esos bloques (en local, docker compose down -v los borra)"
        )
    disk_id = str(uuid.uuid4())
    # Se escribe en un temporal y se renombra, para que nunca quede un .disk_id a medias.
    tmp = path.with_name(f"{DISK_ID_FILE}.tmp")
    with open(tmp, "w", encoding="utf-8") as out:
        out.write(disk_id)
        out.flush()
        os.fsync(out.fileno())
    os.replace(tmp, path)
    return disk_id


def parse_order(raw: dict) -> CopyOrder | DeleteOrder | None:
    """Una orden de la respuesta del latido, o None si su tipo es desconocido o está mal formada.

    Se valida orden por orden: una desconocida no invalida a las demás.
    """
    try:
        return _ORDER.validate_python(raw)
    except ValidationError:
        logger.warning("orden desconocida o mal formada; se ignora: %r", raw)
        return None


class NodeAgent:
    def __init__(
        self, settings: Settings, store: BlockStore, meta: MetaClient, peers: httpx.AsyncClient, *, disk_id: str
    ) -> None:
        """peers: el cliente mTLS hacia la API interna de otros DataNodes (deps.open_clients)."""
        if not settings.node_id or settings.node_ip is None:
            # Como en el MetaServer (metaservers.register_self): python -m datanode ya las exige
            # (Settings.require); esto cubre el lifespan, donde un SystemExit se confundiría con
            # una cancelación.
            raise ValueError("el DataNode no arranca sin DFS_NODE_ID y DFS_NODE_IP: las necesita para registrarse")
        self.settings, self.store, self.meta, self.peers, self.disk_id = settings, store, meta, peers, disk_id
        self.lease_id: str | None = None  # None: todavía no está registrado
        self.heartbeat_s: float = settings.heartbeat_s
        self.done_orders: list[str] = []
        self.bad_blocks: list[tuple[str, str]] = []  # (user, hash), sin repetir
        self.seen: set[str] = set()  # order_id en cola, en curso o cumplidos sin reportar
        self.queue: asyncio.Queue = asyncio.Queue()

    # --- Tareas de fondo ---

    async def heartbeat_loop(self) -> None:
        while True:
            await asyncio.sleep(await self.beat_once())

    async def order_worker(self) -> None:
        """Cumple las órdenes de a una. Un error inesperado queda en el log y no detiene al trabajador."""
        while True:
            order = await self.queue.get()
            try:
                await self.run_order(order)
            except Exception:
                logger.exception("la orden %s falló; se reintenta si vuelve a llegar", order.order_id)
                self.seen.discard(order.order_id)

    # --- Registro y latido ---

    async def beat_once(self) -> float:
        """Una vuelta: registro o latido. Devuelve los segundos que hay que esperar hasta la siguiente."""
        try:
            if self.lease_id is None:
                await self._register()
            else:
                await self._heartbeat()
        except (DFSError, httpx.HTTPError, ValueError) as exc:
            logger.warning("%s: no se pudo hablar con los MetaServers; se intenta en la siguiente vuelta: %s",
                           self.settings.node_id, exc)
        return self.heartbeat_s

    async def _register(self) -> None:
        settings = self.settings
        body = {
            "node_id": settings.node_id,
            "disk_id": self.disk_id,
            "ip": str(settings.node_ip),
            "public_port": settings.dn_public_port,
            "internal_port": settings.dn_internal_port,
            "zone": settings.zone,
            "free_bytes": self.store.free_bytes(),
        }
        answer = RegisterResponse.model_validate(await self.meta.post(REGISTER_PATH, body))
        self.lease_id, self.heartbeat_s = answer.lease_id, answer.heartbeat_s
        logger.info("%s registrado; latido cada %s s", settings.node_id, answer.heartbeat_s)

    async def _heartbeat(self) -> None:
        for bad in self.store.pop_bad_blocks():
            if bad not in self.bad_blocks:
                self.bad_blocks.append(bad)
        done, bad_blocks = list(self.done_orders), list(self.bad_blocks)
        body = {
            "node_id": self.settings.node_id,
            "lease_id": self.lease_id,
            "free_bytes": self.store.free_bytes(),
            "done_orders": done,
            "bad_blocks": [{"user": user, "hash": hash} for user, hash in bad_blocks],
        }
        try:
            answer = await self.meta.post(HEARTBEAT_PATH, body)
        except DFSError as error:
            if error.code != ErrorCode.E_NOT_FOUND:
                raise
            logger.warning("%s: el lease venció; se registra de nuevo", self.settings.node_id)
            self.lease_id = None
            await self._register()
            return
        # Respondió 200: se quita solo lo que este latido envió.
        self.done_orders = [order_id for order_id in self.done_orders if order_id not in done]
        self.bad_blocks = [bad for bad in self.bad_blocks if bad not in bad_blocks]
        self.seen.difference_update(done)  # el MetaServer ya las borró: no vuelven
        for raw in answer.get("orders", []):
            order = parse_order(raw)
            # Una orden que este latido acaba de reportar no se vuelve a cumplir (el MetaServer
            # la borra antes de responder, así que no debería venir; es solo una defensa).
            if order is not None and order.order_id not in self.seen and order.order_id not in done:
                self.seen.add(order.order_id)
                self.queue.put_nowait(order)

    # --- Órdenes ---

    async def run_order(self, order: CopyOrder | DeleteOrder) -> None:
        if order.type == "delete":
            await asyncio.to_thread(self.store.delete_if_older, order.user, order.hash, order.written_before_ms)
            self.done_orders.append(order.order_id)
            return
        try:
            data = await asyncio.to_thread(self.store.read, order.user, order.hash)
        except DFSError as error:
            # Falta o está dañada: store.read ya la anotó en bad_blocks.
            logger.warning("copy %s: la copia local no sirve (%s)", order.order_id, error.code.value)
            self.seen.discard(order.order_id)
            return
        if await send_block(self.peers, order.target.address, order.user, order.hash, data) is None:
            self.seen.discard(order.order_id)  # se reintenta cuando la orden vuelva a llegar
            return
        self.done_orders.append(order.order_id)
