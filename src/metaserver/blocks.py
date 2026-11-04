"""Claves de bloque /blocks/{user}/{hash} = {size, stored_on, touched_at} (DA-02, DA-04).

stored_on dice en qué DataNodes hay copia. Una copia cuenta solo si su DataNode está vivo,
y un bloque está bien guardado si sus copias vivas cubren al menos 2 zonas (G1). Esa regla
la usan la reserva (present), el commit y la confirmación del primario.

Varios MetaServers pueden escribir la misma clave a la vez: la confirmación de una subida,
el latido que reporta una copia, el líder que ordena copias. Por eso toda escritura lee la
clave y la escribe con una transacción que compara la mod_revision leída; si otra escritura
se cruzó, la transacción falla y se intenta de nuevo con el valor nuevo, hasta MAX_TRIES veces.
"""

from collections.abc import Callable, Iterable

from dfs_common.errors import DFSError, ErrorCode
from metaserver.etcd import Etcd, compare, op_put
from metaserver.fs import now_ms
from metaserver.keys import block_key
from metaserver.nodes import Node

MAX_TRIES = 3  # un número de reintentos, no un tiempo


def live_zones(stored_on: Iterable[str], nodes: dict[str, Node]) -> set[str]:
    """Zonas que cubren las copias en DataNodes vivos. nodes: los vivos, por node_id."""
    return {nodes[node_id].zone for node_id in stored_on if node_id in nodes}


def in_two_live_zones(stored_on: Iterable[str], nodes: dict[str, Node]) -> bool:
    return len(live_zones(stored_on, nodes)) >= 2


def _union(first: Iterable[str], second: Iterable[str]) -> list[str]:
    """Los node_id de las dos listas, sin repetir y en el orden en que aparecen."""
    return list(dict.fromkeys([*first, *second]))


async def confirm_block(etcd: Etcd, user: str, hash: str, size: int, stored_on: list[str], nodes: dict[str, Node]) -> None:
    """El primario confirma dónde quedó un bloque (POST /internal/blocks/confirm).

    Si las copias vivas no cubren 2 zonas: 422 E_REPLICAS, sin escribir nada. Si no, crea la
    clave o une stored_on con la que ya había; touched_at queda en este instante.
    """
    if not in_two_live_zones(stored_on, nodes):
        raise DFSError(ErrorCode.E_REPLICAS, "las copias vivas del bloque no cubren 2 zonas")

    def confirmed(old: dict | None) -> dict:
        if old is None:
            return {"size": size, "stored_on": _union(stored_on, []), "touched_at": now_ms()}
        return {**old, "stored_on": _union(old["stored_on"], stored_on), "touched_at": now_ms()}

    await _update(etcd, block_key(user, hash), confirmed)


async def update_stored_on(
    etcd: Etcd, user: str, hash: str, add: Iterable[str] = (), remove: Iterable[str] = ()
) -> bool:
    """Agrega y quita nodos de stored_on de un bloque que ya existe. Devuelve si la clave existía.

    La usan los latidos (copias cumplidas y bloques dañados, tarea 13) y el líder (tarea 19).
    """
    removed = set(remove)

    def changed(old: dict | None) -> dict | None:
        if old is None:
            return None
        return {**old, "stored_on": [node_id for node_id in _union(old["stored_on"], add) if node_id not in removed]}

    return await _update(etcd, block_key(user, hash), changed)


async def touch_block(etcd: Etcd, user: str, hash: str) -> bool:
    """Pone touched_at en este instante, para que la limpieza no borre un bloque que una reserva reutiliza (DA-02)."""
    return await _update(etcd, block_key(user, hash), lambda old: None if old is None else {**old, "touched_at": now_ms()})


async def _update(etcd: Etcd, key: str, change: Callable[[dict | None], dict | None]) -> bool:
    """Lee key, calcula el valor nuevo con change y lo escribe si nadie cambió la clave mientras tanto.

    change recibe el valor actual (None si la clave no existe) y devuelve el nuevo, o None
    para no escribir. Devuelve si escribió. Si otra escritura se cruza MAX_TRIES veces
    seguidas, 503 E_UNAVAILABLE: quien llama puede reintentar.
    """
    for _ in range(MAX_TRIES):
        kv = await etcd.get(key)
        value = change(kv.json() if kv else None)
        if value is None:
            return False
        if kv is None:
            unchanged = compare(key, "CREATE", "EQUAL", 0)  # sigue sin existir
        else:
            unchanged = compare(key, "MOD", "EQUAL", kv.mod_revision)  # nadie la tocó desde que se leyó
        if (await etcd.txn([unchanged], [op_put(key, value)], [])).succeeded:
            return True
    raise DFSError(ErrorCode.E_UNAVAILABLE, f"{key} cambió {MAX_TRIES} veces mientras se escribía; se puede reintentar")
