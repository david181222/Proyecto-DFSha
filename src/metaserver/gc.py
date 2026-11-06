"""Limpieza de bloques con la regla del instante T (tarea 20, DA-02 y DA-14).

La corre el líder en su bucle, cada DFS_GC_INTERVAL_S. A diferencia de la re-réplica, no
espera DFS_LEADER_GRACE_S: sus reglas solo dependen de lo que hay en etcd y de la hora.

plan_gc es una función pura: recibe los manifiestos y los bloques leídos de etcd y la hora, y
devuelve qué borrar. gc_round lee etcd, llama a plan_gc y escribe.

1. Manifiestos vencidos: los que tienen replaced_at y llevan al menos DFS_RETENTION_S
   retenidos. Se borran primero, cada uno con una transacción que compara su mod_revision. El
   manifiesto actual de un archivo no tiene replaced_at, así que nunca vence.
2. Bloques en uso: los (user, hash) de todos los manifiestos que quedan, actuales y retenidos.
   El manifiesto guarda user (docs/contratos.md, sección 8), así que cada versión protege las
   claves /blocks/{user}/{hash} de su usuario, también después de rm.
3. Candidatos: los bloques que no están en uso y cuyo touched_at tiene al menos
   DFS_GC_MIN_AGE_S. Esa edad protege las subidas en curso, porque la confirmación y la
   reserva tocan la clave.

Borrado de un candidato (Figura 1 de docs/decisiones.md): T es la hora justo antes de escribir.
Una sola transacción compara que la mod_revision siga siendo la leída, borra la clave y pone
una orden delete para cada nodo de stored_on, también los caídos (la reciben al volver), con
written_before_ms = T - DFS_GC_CLOCK_MARGIN_S. Si falla, alguien tocó el bloque (por ejemplo,
una reserva que lo reutiliza): se salta y se revisa en la siguiente vuelta. Borrar la clave y
crear las órdenes juntas hace que una caída del líder no deje un bloque sin clave y sin orden.
"""

import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass

from dfs_common.config import Settings
from metaserver.etcd import Etcd, compare, op_delete, op_put
from metaserver.keys import BLOCKS_PREFIX, FILES_PREFIX, block_key, order_key

logger = logging.getLogger("dfsha")


@dataclass(frozen=True)
class Manifest:
    """Un manifiesto /files/{file_id}/v/{n}, con su JSON completo."""

    key: str
    value: dict
    mod_revision: int


@dataclass(frozen=True)
class StoredBlock:
    """Un bloque /blocks/{user}/{hash}, con su JSON completo."""

    user: str
    hash: str
    value: dict
    mod_revision: int


@dataclass(frozen=True)
class GcPlan:
    expired: list[Manifest]
    candidates: list[StoredBlock]


def plan_gc(manifests: list[Manifest], blocks: list[StoredBlock], now: float, settings: Settings) -> GcPlan:
    """Manifiestos vencidos y bloques candidatos a borrar. now en segundos desde epoch."""
    now_ms = int(now * 1000)
    retention_ms = settings.retention_s * 1000
    expired = [
        manifest
        for manifest in manifests
        if manifest.value["replaced_at"] is not None and now_ms - manifest.value["replaced_at"] >= retention_ms
    ]
    expired_keys = {manifest.key for manifest in expired}
    in_use = {
        (manifest.value["user"], hash)
        for manifest in manifests
        if manifest.key not in expired_keys
        for hash in manifest.value["hashes"]
    }
    min_age_ms = settings.gc_min_age_s * 1000
    candidates = [
        block
        for block in blocks
        if (block.user, block.hash) not in in_use and now_ms - block.value["touched_at"] >= min_age_ms
    ]
    return GcPlan(expired=expired, candidates=candidates)


def delete_orders(block: StoredBlock, t_ms: int, settings: Settings, new_id: Callable[[], str]) -> list[tuple[str, dict]]:
    """(node_id, orden delete) para cada nodo de stored_on, con written_before_ms = T - margen."""
    written_before_ms = t_ms - settings.gc_clock_margin_s * 1000
    return [
        (
            node_id,
            {
                "order_id": new_id(),
                "type": "delete",
                "user": block.user,
                "hash": block.hash,
                "written_before_ms": written_before_ms,
            },
        )
        for node_id in block.value["stored_on"]
    ]


async def gc_round(
    etcd: Etcd,
    settings: Settings,
    now: float,
    *,
    clock: Callable[[], float] = time.time,
    new_id: Callable[[], str] = lambda: str(uuid.uuid4()),
) -> GcPlan:
    """Una vuelta de la limpieza: lee, decide con plan_gc y borra.

    now es la hora de la vuelta, con la que se decide; clock da T justo antes de cada borrado.
    Si un manifiesto vencido no se pudo borrar (cambió), esta vuelta no borra bloques: ese
    manifiesto todavía protege los suyos, y la siguiente vuelta decide con lo que haya.
    """
    manifests = [Manifest(kv.key, kv.json(), kv.mod_revision) for kv in await etcd.get_prefix(FILES_PREFIX)]
    blocks = []
    for kv in await etcd.get_prefix(BLOCKS_PREFIX):
        user, hash = kv.key.removeprefix(BLOCKS_PREFIX).split("/")
        blocks.append(StoredBlock(user, hash, kv.json(), kv.mod_revision))
    plan = plan_gc(manifests, blocks, now, settings)

    all_expired_deleted = True
    for manifest in plan.expired:
        unchanged = compare(manifest.key, "MOD", "EQUAL", manifest.mod_revision)
        all_expired_deleted &= (await etcd.txn([unchanged], [op_delete(manifest.key)], [])).succeeded
    if not all_expired_deleted:
        logger.warning("limpieza: un manifiesto vencido cambió; los bloques se revisan en la siguiente vuelta")
        return plan

    for block in plan.candidates:
        key = block_key(block.user, block.hash)
        t_ms = int(clock() * 1000)  # el instante T, justo antes de escribir
        orders = delete_orders(block, t_ms, settings, new_id)
        success = [op_delete(key)] + [op_put(order_key(node_id, order["order_id"]), order) for node_id, order in orders]
        result = await etcd.txn([compare(key, "MOD", "EQUAL", block.mod_revision)], success, [])
        if result.succeeded:
            logger.info("limpieza: borrado %s/%s de %s", block.user, block.hash[:12], ", ".join(block.value["stored_on"]))
    return plan
