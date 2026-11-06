"""Líder de los MetaServers: elección y reparación de copias (tarea 19, DA-12).

Cada MetaServer corre run_leader en el lifespan de su app pública. Todos hacen campaña en
/election/leader con su meta_id como valor, y etcd deja ganar a uno solo: el que lleva más
tiempo en la fila. El líder revisa el estado cada DFS_LEADER_SCAN_S y ordena lo que falta:

- una orden copy para cada bloque con menos de 3 copias vivas (re-réplica y tercera copia);
- una orden delete para la copia que sobra cuando vuelve un DataNode;
- borrar las órdenes copy atascadas, para que la siguiente vuelta cree otra, y mandarle al
  destino una delete por si alcanzó a recibir la copia sin que quedara registrada.

Además corre la limpieza (metaserver.gc, tarea 20) cada DFS_GC_INTERVAL_S, desde la primera
vuelta. La limpieza no espera DFS_LEADER_GRACE_S: esa espera es solo para la re-réplica.

Las reglas viven en plan_repairs, una función pura que recibe una foto del estado
(Snapshot), la hora y lo que el líder recuerda, y devuelve qué escribir (Plan). El bucle solo
lee etcd, llama a plan_repairs y escribe. Así cada regla se prueba sin etcd y sin esperar.

Elección (run_leader):

1. Pide un lease de DFS_NODE_LEASE_S y lo renueva cada DFS_HEARTBEAT_S.
2. Hace campaña y espera hasta ganar.
3. Al ganar anota won_at y corre una vuelta cada DFS_LEADER_SCAN_S mientras su lease siga vivo.
   Antes de cada vuelta comprueba en etcd que su clave siga siendo la del líder: si el lease
   venció entre dos renovaciones, otro ya pudo ganar, y no debe haber dos líderes ordenando.
4. Si el lease ya no existe, o su clave ya no es la del líder, deja de actuar como líder y
   vuelve al paso 1.
5. Al apagarse hace resign, y gana el siguiente de la fila. Al salir de cada ciclo, también
   al apagarse, revoca su lease: así un candidato que no llegó a ganar no deja su clave en la
   fila hasta que el lease venza solo.

Un líder nuevo empieza con missing_since vacío y no ordena nada durante DFS_LEADER_GRACE_S,
para no copiar los bloques de un DataNode que solo tarda en arrancar.
"""

import asyncio
import contextlib
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from dfs_common.config import Settings
from dfs_common.errors import DFSError
from metaserver import gc
from metaserver.blocks import MAX_TRIES, live_zones
from metaserver.etcd import Etcd, EtcdError, LeaseNotFound, compare, op_delete, op_put
from metaserver.keys import BLOCKS_PREFIX, ELECTION_NAME, ORDERS_PREFIX, block_key, order_key
from metaserver.nodes import Node, by_id, live_nodes

logger = logging.getLogger("dfsha")

COPIES = 3  # copias que debe tener cada bloque (DA-04)

NewId = Callable[[], str]


def new_order_id() -> str:
    return str(uuid.uuid4())


# --- Foto del estado y plan ---


@dataclass(frozen=True)
class Block:
    """Un bloque como está en /blocks/{user}/{hash}; value es el JSON completo."""

    user: str
    hash: str
    value: dict
    mod_revision: int

    @property
    def stored_on(self) -> list[str]:
        return self.value["stored_on"]

    @property
    def size(self) -> int:
        return self.value["size"]


@dataclass(frozen=True)
class PendingOrder:
    """Una orden en /orders/{node_id}/{order_id}."""

    key: str
    node_id: str
    order: dict
    mod_revision: int = 0


@dataclass(frozen=True)
class Snapshot:
    """Lo que el líder lee de etcd en cada vuelta. nodes: los DataNodes vivos, por node_id."""

    nodes: dict[str, Node]
    blocks: list[Block]
    orders: list[PendingOrder]


@dataclass(frozen=True)
class Trim:
    """Quitar node_id de stored_on de block y mandarle la orden delete, en una sola transacción."""

    block: Block
    node_id: str
    order: dict


@dataclass(frozen=True)
class StaleOrder:
    """Una orden copy atascada que se borra, con la delete para su destino si hace falta.

    block es el bloque como se leyó (None si ya no existe). cleanup va a target cuando el destino
    no está en stored_on: si el DataNode origen alcanzó a enviarle la copia pero la orden se borra
    antes de que la reporte cumplida, esa copia no queda en stored_on; la delete la borra con la
    regla del instante T, y si no hay copia no hace nada.
    """

    pending: PendingOrder
    block: Block | None
    target: str
    cleanup: dict | None


@dataclass
class Plan:
    copies: list[tuple[str, dict]] = field(default_factory=list)  # (nodo origen, orden copy)
    trims: list[Trim] = field(default_factory=list)
    stale_orders: list[StaleOrder] = field(default_factory=list)  # órdenes copy que se borran
    missing_since: dict[str, float] = field(default_factory=dict)
    returned: set[str] = field(default_factory=set)  # nodos que volvieron en esta vuelta


@dataclass
class LeaderState:
    """Lo que el líder recuerda entre vueltas, solo en memoria: un líder nuevo empieza de cero."""

    won_at: float
    missing_since: dict[str, float] = field(default_factory=dict)
    last_gc: float | None = None  # cuándo corrió la última limpieza; None: todavía no corrió


# --- Reglas (funciones puras) ---


def plan_repairs(
    snapshot: Snapshot,
    now: float,
    settings: Settings,
    missing_since: dict[str, float],
    won_at: float,
    new_id: NewId = new_order_id,
) -> Plan:
    """Decide qué órdenes escribir. now y won_at en segundos desde epoch.

    1. Durante DFS_LEADER_GRACE_S desde won_at no ordena nada; missing_since sí se actualiza.
    2. missing_since: cuándo vio este líder ausente por primera vez cada nodo de algún stored_on.
    3. Un bloque necesita copia si tiene menos de 3 copias vivas y, además, su stored_on tiene
       menos de 3 nodos o alguno lleva ausente al menos DFS_REREPLICATE_AFTER_S.
    4. Se salta un bloque con una orden copy pendiente. Una copy cuyo origen o destino ya no
       está vivo, o cuyo origen ya no tiene el bloque, está atascada: se borra (StaleOrder) y
       la siguiente vuelta crea otra con nodos vivos (docs/contratos.md, sección 7.3).
    5. Origen: el primer nodo vivo de stored_on. Destino: un nodo vivo sin el bloque y con
       espacio, de preferencia de una zona que las copias vivas no cubren; si empatan, el menor
       node_id. Una orden por bloque y por vuelta; sin destino posible, nada.
    7. Más de 3 copias vivas: se quita de stored_on el nodo que volvió, o si no se sabe cuál,
       el último cuya salida deje 2 zonas (Trim).
    8. Los nodos caídos se quedan en stored_on: si vuelven, sus copias cuentan otra vez.
    """
    nodes = snapshot.nodes
    in_stored_on = {node_id for block in snapshot.blocks for node_id in block.stored_on}
    absent = in_stored_on - set(nodes)
    plan = Plan(
        missing_since={node_id: missing_since.get(node_id, now) for node_id in sorted(absent)},
        returned={node_id for node_id in missing_since if node_id in nodes},
    )
    if now - won_at < settings.leader_grace_s:
        return plan

    blocks = {(block.user, block.hash): block for block in snapshot.blocks}
    with_copy_order = set()
    for pending in snapshot.orders:
        if pending.order["type"] != "copy":
            continue
        block = blocks.get((pending.order["user"], pending.order["hash"]))
        stuck = (
            block is None
            or pending.node_id not in nodes
            or pending.order["target"]["node_id"] not in nodes
            or pending.node_id not in block.stored_on
        )
        if stuck:
            plan.stale_orders.append(_stale(pending, block, now, settings, new_id))
        if block is not None:
            with_copy_order.add((block.user, block.hash))

    for block in snapshot.blocks:
        alive = [node_id for node_id in block.stored_on if node_id in nodes]
        if len(alive) > COPIES:
            trim = plan_trim(block, nodes, plan.returned, now, settings, new_id)
            if trim is not None:
                plan.trims.append(trim)
            continue
        if len(alive) == COPIES or not alive or (block.user, block.hash) in with_copy_order:
            continue
        overdue = any(
            now - plan.missing_since[node_id] >= settings.rereplicate_after_s
            for node_id in block.stored_on
            if node_id in absent
        )
        if len(block.stored_on) >= COPIES and not overdue:
            continue
        target = _choose_target(block, alive, nodes)
        if target is None:
            continue
        order = {
            "order_id": new_id(),
            "type": "copy",
            "user": block.user,
            "hash": block.hash,
            "target": {"node_id": target.node_id, "address": target.internal_address},
        }
        plan.copies.append((alive[0], order))
    return plan


def plan_trim(
    block: Block, nodes: dict[str, Node], returned: set[str], now: float, settings: Settings, new_id: NewId = new_order_id
) -> Trim | None:
    """La copia que sobra de un bloque con más de 3 copias vivas (decisión 4 de la tarea 02c).

    Se quita un nodo que volvió en esta vuelta; si no se sabe cuál, el último de stored_on. En
    los dos casos, solo si las copias vivas que quedan cubren al menos 2 zonas. La orden delete
    lleva written_before_ms = ahora menos DFS_GC_CLOCK_MARGIN_S, así que una copia que ese nodo
    reciba después no se borra. None si el bloque ya no tiene copias de más.
    """
    alive = [node_id for node_id in block.stored_on if node_id in nodes]
    if len(alive) <= COPIES:
        return None

    def leaves_two_zones(node_id: str) -> bool:
        return len(live_zones([other for other in alive if other != node_id], nodes)) >= 2

    choices = [node_id for node_id in alive if node_id in returned] + list(reversed(alive))
    removed = next((node_id for node_id in choices if leaves_two_zones(node_id)), None)
    if removed is None:
        return None
    order = {
        "order_id": new_id(),
        "type": "delete",
        "user": block.user,
        "hash": block.hash,
        "written_before_ms": int(now * 1000) - settings.gc_clock_margin_s * 1000,
    }
    return Trim(block=block, node_id=removed, order=order)


def _stale(pending: PendingOrder, block: Block | None, now: float, settings: Settings, new_id: NewId) -> StaleOrder:
    target = pending.order["target"]["node_id"]
    cleanup = None
    if block is None or target not in block.stored_on:
        cleanup = {
            "order_id": new_id(),
            "type": "delete",
            "user": pending.order["user"],
            "hash": pending.order["hash"],
            "written_before_ms": int(now * 1000) - settings.gc_clock_margin_s * 1000,
        }
    return StaleOrder(pending=pending, block=block, target=target, cleanup=cleanup)


def _choose_target(block: Block, alive: list[str], nodes: dict[str, Node]) -> Node | None:
    covered = live_zones(alive, nodes)
    candidates = [
        node
        for node_id, node in sorted(nodes.items())
        if node_id not in block.stored_on and node.free_bytes >= block.size
    ]
    # min es estable: entre los de una zona nueva (False < True), el primero en orden de node_id.
    return min(candidates, key=lambda node: node.zone in covered, default=None)


# --- Entrada y salida con etcd ---


def _block(kv) -> Block:
    user, hash = kv.key.removeprefix(BLOCKS_PREFIX).split("/")
    return Block(user=user, hash=hash, value=kv.json(), mod_revision=kv.mod_revision)


async def read_snapshot(etcd: Etcd) -> Snapshot:
    blocks = [_block(kv) for kv in await etcd.get_prefix(BLOCKS_PREFIX)]
    orders = [
        PendingOrder(
            key=kv.key, node_id=kv.key.removeprefix(ORDERS_PREFIX).split("/")[0], order=kv.json(), mod_revision=kv.mod_revision
        )
        for kv in await etcd.get_prefix(ORDERS_PREFIX)
    ]
    return Snapshot(nodes=by_id(await live_nodes(etcd)), blocks=blocks, orders=orders)


async def apply_plan(etcd: Etcd, plan: Plan, nodes: dict[str, Node], now: float, settings: Settings) -> None:
    """Escribe el plan: borra las órdenes atascadas, escribe las copy y aplica cada Trim.

    Una orden atascada se borra en una transacción que compara la mod_revision de la orden y la
    del bloque, y que pone la delete del destino. Si un latido justo cumplió la orden (la borra
    y agrega el destino a stored_on), la transacción falla y no se manda una delete que borraría
    una copia registrada.

    Un Trim es una transacción que compara la mod_revision del bloque: escribe el stored_on
    sin ese nodo y la orden delete. Si falla porque el bloque cambió, se lee de nuevo, se
    recalcula con plan_trim y se intenta otra vez, hasta MAX_TRIES veces; si no, queda para
    la siguiente vuelta.
    """
    for stale in plan.stale_orders:
        await _delete_stale(etcd, stale)
    for origin, order in plan.copies:
        await etcd.put(order_key(origin, order["order_id"]), order)
        logger.info("líder: copia de %s/%s de %s a %s", order["user"], order["hash"][:12], origin, order["target"]["node_id"])
    for trim in plan.trims:
        await _apply_trim(etcd, trim, nodes, plan.returned, now, settings)


async def _delete_stale(etcd: Etcd, stale: StaleOrder) -> None:
    pending, block = stale.pending, stale.block
    key = block_key(pending.order["user"], pending.order["hash"])
    conditions = [compare(pending.key, "MOD", "EQUAL", pending.mod_revision)]
    if block is None:
        conditions.append(compare(key, "CREATE", "EQUAL", 0))  # sigue sin existir
    else:
        conditions.append(compare(key, "MOD", "EQUAL", block.mod_revision))
    success = [op_delete(pending.key)]
    if stale.cleanup is not None:
        success.append(op_put(order_key(stale.target, stale.cleanup["order_id"]), stale.cleanup))
    if (await etcd.txn(conditions, success, [])).succeeded:
        logger.info("líder: orden copy atascada %s borrada", pending.key)


async def _apply_trim(
    etcd: Etcd, trim: Trim | None, nodes: dict[str, Node], returned: set[str], now: float, settings: Settings
) -> None:
    for _ in range(MAX_TRIES):
        if trim is None:
            return
        block = trim.block
        key = block_key(block.user, block.hash)
        stored_on = [node_id for node_id in block.stored_on if node_id != trim.node_id]
        success = [
            op_put(key, {**block.value, "stored_on": stored_on}),
            op_put(order_key(trim.node_id, trim.order["order_id"]), trim.order),
        ]
        if (await etcd.txn([compare(key, "MOD", "EQUAL", block.mod_revision)], success, [])).succeeded:
            logger.info("líder: sobra la copia de %s/%s en %s", block.user, block.hash[:12], trim.node_id)
            return
        current = await etcd.get(key)
        trim = None if current is None else plan_trim(_block(current), nodes, returned, now, settings)
    logger.warning("líder: el bloque de una copia que sobra cambió %s veces; queda para la siguiente vuelta", MAX_TRIES)


async def leader_round(etcd: Etcd, settings: Settings, state: LeaderState, now: float) -> None:
    """Una vuelta completa del líder: la reparación de copias y, si ya le toca, la limpieza."""
    await repair_round(etcd, settings, state, now)
    if state.last_gc is None or now - state.last_gc >= settings.gc_interval_s:
        await gc.gc_round(etcd, settings, now)
        state.last_gc = now


async def repair_round(etcd: Etcd, settings: Settings, state: LeaderState, now: float) -> Plan:
    """Una vuelta del líder: lee, decide con plan_repairs y escribe."""
    snapshot = await read_snapshot(etcd)
    plan = plan_repairs(snapshot, now, settings, state.missing_since, state.won_at)
    state.missing_since = plan.missing_since
    await apply_plan(etcd, plan, snapshot.nodes, now, settings)
    return plan


# --- Elección ---

Sleep = Callable[[float], Awaitable[None]]


async def run_leader(
    etcd: Etcd, settings: Settings, *, clock: Callable[[], float] = time.time, sleep: Sleep = asyncio.sleep
) -> None:
    """Hace campaña y, mientras sea líder, corre las vueltas. No termina hasta que la cancelen.

    clock y sleep existen para las pruebas: con un reloj falso, las esperas del sistema no se
    esperan de verdad. Si etcd falla, queda en el log y se intenta de nuevo.
    """
    while True:
        try:
            await _lead_while_lease_lives(etcd, settings, clock, sleep)
        except (DFSError, EtcdError) as exc:
            logger.warning("líder: etcd no respondió; se intenta de nuevo: %s", exc)
            await sleep(settings.heartbeat_s)


async def _lead_while_lease_lives(etcd: Etcd, settings: Settings, clock: Callable[[], float], sleep: Sleep) -> None:
    """Un ciclo: lease nuevo, campaña y vueltas de líder hasta que el lease deja de existir."""
    lease_id = await etcd.lease_grant(settings.node_lease_s)
    lost = asyncio.Event()
    keeper = asyncio.create_task(_keep_lease(etcd, lease_id, settings.heartbeat_s, lost, sleep))
    campaign = asyncio.create_task(etcd.campaign(ELECTION_NAME, lease_id, settings.node_id.encode("utf-8")))
    lost_wait = asyncio.create_task(lost.wait())
    try:
        await asyncio.wait({campaign, lost_wait}, return_when=asyncio.FIRST_COMPLETED)
        if not campaign.done():
            return  # el lease venció antes de ganar: se empieza de nuevo
        leader_key = campaign.result()
        state = LeaderState(won_at=clock())
        logger.info("líder: %s ganó la elección", settings.node_id)
        try:
            while not lost.is_set() and await _still_leader(etcd, leader_key):
                try:
                    await leader_round(etcd, settings, state, clock())
                except (DFSError, EtcdError) as exc:
                    logger.warning("líder: la vuelta falló; se intenta en la siguiente: %s", exc)
                await sleep(settings.leader_scan_s)
            logger.warning("líder: %s perdió su lease o ya no es el líder; vuelve a hacer campaña", settings.node_id)
        except asyncio.CancelledError:
            with contextlib.suppress(DFSError, EtcdError):
                await etcd.resign(leader_key)
            raise
    finally:
        for task in (keeper, campaign, lost_wait):
            task.cancel()
        await asyncio.gather(keeper, campaign, lost_wait, return_exceptions=True)
        with contextlib.suppress(DFSError, EtcdError):  # LeaseNotFound es un EtcdError: ya había vencido
            await etcd.lease_revoke(lease_id)


async def _still_leader(etcd: Etcd, leader_key) -> bool:
    current = await etcd.leader(ELECTION_NAME)
    return current is not None and current.key == leader_key.key


async def _keep_lease(etcd: Etcd, lease_id: int, every_s: float, lost: asyncio.Event, sleep: Sleep) -> None:
    """Renueva el lease cada every_s. Si ya no existe, avisa con lost y termina."""
    while True:
        await sleep(every_s)
        try:
            await etcd.lease_keepalive(lease_id)
        except LeaseNotFound:
            lost.set()
            return
        except (DFSError, EtcdError) as exc:
            logger.warning("líder: no se pudo renovar el lease; se intenta en la siguiente vuelta: %s", exc)
