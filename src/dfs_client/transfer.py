"""Subir y bajar archivos por bloques, en paralelo y con reintentos (tarea 17; DA-02, DA-04, DA-14).

Lo usan dfs put y dfs get (cli.py), y open/read/write del cliente (tarea 18). En memoria hay a
lo más `parallel` bloques a la vez: cada hilo lee o recibe su bloque, lo envía o lo escribe, y
lo suelta. Nunca está el archivo entero en memoria.

Subida (upload_version), docs/contratos.md sección 1, "Cómo se encadenan las rutas":
1. POST /uploads con los bloques nuevos o cambiados: responde present y missing.
2. Envía los faltantes en paralelo. Antes de cada envío llama a keeper.check(): si se perdió el
   lock, se detiene ya. Si un envío falla:
   - reintentable (422 E_CHECKSUM o E_REPLICAS, 503, error de conexión o timeout; DfsApi los
     entrega todos como DFSError): realloc sin el primario que falló, sumado a los excluidos de
     ese bloque, y vuelve a enviar;
   - 401 (pase vencido): realloc con exclude_nodes vacío, que entrega un pase nuevo (sección 7.2);
   - cada bloque se intenta como máximo MAX_TRIES veces. Si se agota, o si el error no es
     reintentable, falla toda la subida.
3. Commit. Si responde 422 E_REPLICAS (se perdió una copia antes de confirmar), vuelve al paso 1
   con la misma lista, como máximo MAX_TRIES rondas. Cualquier otro error, como un 409, se lanza.

Bajada:
- fetch_block prueba las réplicas en orden (primero las de la zona del cliente, DA-11). Ante un
  422, un 404, un 503 o un error de conexión, o si el SHA-256 de lo recibido no es el del bloque,
  pasa a la siguiente. Ante un 401, pide el manifiesto otra vez con la misma versión (DA-14) y
  reintenta el bloque con el pase nuevo, una sola vez. Si ninguna sirve, 503 E_UNAVAILABLE.
- Nodos sospechosos: un DataNode que no responde (E_UNAVAILABLE: error de conexión, timeout o
  503) puede tardar todo el timeout en fallar, y el manifiesto lo sigue listando hasta que se
  pide otro. download_file recuerda esos nodos y, en los bloques siguientes, los prueba al final:
  así un nodo caído cuesta un timeout por cada bloque que ya iba hacia él (a lo más `parallel`),
  no uno por bloque. Un sospechoso no se descarta: si las demás réplicas fallan, se prueba igual.
  Un 404 o un E_CHECKSUM no lo hacen sospechoso: el nodo respondió y el problema es de esa copia.
- download_file escribe en local + ".part", cada bloque en su posición (index × block_size) y
  con un manejador de archivo por hilo. Al final compara el SHA-256 del archivo, si lo hay, y
  renombra con os.replace. Si algo falla, borra el .part y el archivo anterior queda intacto.
"""

import hashlib
import logging
import os
import threading
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

from dfs_client.locks import LockKeeper
from dfs_common.errors import DFSError, ErrorCode
from dfs_common.models import BlockAssignment, BlockLocation, BlockSpec, CommitResponse, FileBlocksResponse

logger = logging.getLogger("dfsha")

MAX_TRIES = 3  # intentos por bloque y rondas de commit
RETRYABLE_PUT = {ErrorCode.E_CHECKSUM, ErrorCode.E_REPLICAS, ErrorCode.E_UNAVAILABLE}
NEXT_REPLICA = {ErrorCode.E_CHECKSUM, ErrorCode.E_NOT_FOUND, ErrorCode.E_UNAVAILABLE}
READ_CHUNK = 1024 * 1024  # para calcular el SHA-256 de un archivo sin cargarlo entero


@dataclass
class UploadStats:
    """Cifras de una subida para dfs put: bloques enviados y los que ya estaban en la primera reserva."""

    sent: int = 0
    present: int = 0


# --- Archivo local ---


def hash_file(path: Path, block_size: int) -> tuple[list[BlockSpec], list[str], str, int]:
    """Una pasada por el archivo, bloque a bloque: (bloques, hashes, SHA-256 del archivo, tamaño)."""
    blocks, hashes = [], []
    whole = hashlib.sha256()
    size = 0
    with open(path, "rb") as source:
        while data := source.read(block_size):
            hash = hashlib.sha256(data).hexdigest()
            blocks.append(BlockSpec(index=len(blocks), hash=hash, size=len(data)))
            hashes.append(hash)
            whole.update(data)
            size += len(data)
    return blocks, hashes, whole.hexdigest(), size


def read_block(path: Path, block_size: int, index: int) -> bytes:
    with open(path, "rb") as source:
        source.seek(index * block_size)
        return source.read(block_size)


# --- Subida ---


def upload_version(
    api,
    *,
    path: str,
    mode: str,
    base_version: int,
    lock_id: str,
    block_size: int,
    blocks: Iterable[BlockSpec | tuple[int, str, int]],
    hashes: list[str],
    size: int,
    file_sha256: str | None,
    get_block_bytes: Callable[[int], bytes],
    parallel: int,
    keeper: LockKeeper | None = None,
    stats: UploadStats | None = None,
) -> CommitResponse:
    """Publica una versión: reserva, envía los bloques que faltan y confirma.

    blocks: los bloques nuevos o cambiados; hashes: todos los de la versión nueva, en orden.
    get_block_bytes(index) lee el contenido de un bloque cuando hace falta enviarlo.
    keeper: el LockKeeper del lock, para detenerse si se pierde. stats: se llena con las cifras.
    """
    blocks = list(blocks)
    stats = stats if stats is not None else UploadStats()
    counting = threading.Lock()  # los hilos de envío suman a stats.sent a la vez
    for commit_round in range(1, MAX_TRIES + 1):
        reservation = api.uploads(
            path=path, mode=mode, base_version=base_version, block_size=block_size, lock_id=lock_id, blocks=blocks
        )
        if commit_round == 1:
            stats.present = len(reservation.present)

        def send(assignment: BlockAssignment) -> None:
            _send_block(api, reservation.upload_id, assignment, get_block_bytes, keeper)
            with counting:
                stats.sent += 1

        run_parallel(send, reservation.missing, parallel)
        _check(keeper)
        try:
            return api.commit(
                reservation.upload_id,
                base_version=base_version,
                lock_id=lock_id,
                hashes=hashes,
                size=size,
                file_sha256=file_sha256,
            )
        except DFSError as error:
            if error.code != ErrorCode.E_REPLICAS or commit_round == MAX_TRIES:
                raise
            logger.warning("el commit de %s respondió E_REPLICAS; se reserva de nuevo (ronda %s)", path, commit_round + 1)
    raise AssertionError("no se llega aquí")  # el bucle siempre devuelve o lanza


def _send_block(api, upload_id: str, assignment: BlockAssignment, get_block_bytes, keeper) -> None:
    """Envía un bloque a su primario; si falla de forma reintentable, pide otro destino (realloc)."""
    data = get_block_bytes(assignment.index)
    excluded: list[str] = []
    for attempt in range(1, MAX_TRIES + 1):
        _check(keeper)
        try:
            api.put_block(assignment.primary.address, assignment.hash, assignment.pass_, data)
            return
        except DFSError as error:
            if error.code not in RETRYABLE_PUT and error.code != ErrorCode.E_AUTH:
                raise
            if attempt == MAX_TRIES:
                if error.code == ErrorCode.E_AUTH:
                    # Es el pase del bloque, no el JWT: que la CLI no pida iniciar sesión otra vez.
                    raise DFSError(ErrorCode.E_UNAVAILABLE, f"el pase del bloque {assignment.index} siguió vencido") from None
                raise
            if error.code == ErrorCode.E_AUTH:
                exclude = []  # pase vencido: el mismo realloc entrega uno nuevo (sección 7.2)
            else:
                excluded.append(assignment.primary.node_id)
                exclude = excluded
            logger.warning(
                "el bloque %s falló en %s (%s); se pide otro destino", assignment.index, assignment.primary.node_id,
                error.code.value,
            )
            [assignment] = api.realloc(upload_id, [assignment.index], exclude).missing


def _check(keeper: LockKeeper | None) -> None:
    if keeper is not None:
        keeper.check()


# --- Bajada ---


def fetch_block(api, block: BlockLocation, *, path: str, version: int, suspects: set[str] | None = None) -> bytes:
    """Un bloque de alguna de sus réplicas, verificado por su SHA-256.

    suspects: node_id que no respondieron en bloques anteriores; se prueban al final, y aquí se
    agregan los que no respondan. Sin él, las réplicas se prueban en el orden del manifiesto.
    """
    refreshed = False
    while True:
        expired = False
        for replica in _suspects_last(block.replicas, suspects):
            try:
                data = api.get_block(replica.address, block.hash, block.pass_)
            except DFSError as error:
                if error.code == ErrorCode.E_AUTH:
                    expired = True
                    break
                if error.code == ErrorCode.E_UNAVAILABLE and suspects is not None:
                    suspects.add(replica.node_id)
                if error.code in NEXT_REPLICA:
                    continue
                raise
            if hashlib.sha256(data).hexdigest() == block.hash:
                return data
            logger.warning("el bloque %s llegó de %s con otro hash; se prueba otra réplica", block.index, replica.node_id)
        if expired and not refreshed:
            # El pase venció: pases nuevos para la misma versión que se abrió (DA-14).
            manifest = api.files_blocks(path, version)
            block = next(new for new in manifest.blocks if new.index == block.index)
            refreshed = True
            continue
        reason = "no tiene réplicas vivas" if not block.replicas else "no lo entregó ninguna réplica"
        raise DFSError(ErrorCode.E_UNAVAILABLE, f"el bloque {block.index} {reason}; intenta más tarde")


def _suspects_last(replicas: list, suspects: set[str] | None) -> list:
    """Las réplicas en el mismo orden, con las de nodos sospechosos al final."""
    if not suspects:
        return replicas
    return sorted(replicas, key=lambda replica: replica.node_id in suspects)  # sorted es estable


def download_file(api, manifest: FileBlocksResponse, local_path: Path, parallel: int) -> None:
    """Baja la versión del manifiesto a local_path, pasando por local_path + ".part"."""
    local_path = Path(local_path)
    part = local_path.with_name(local_path.name + ".part")
    suspects: set[str] = set()  # compartido por los hilos; agregar a un set es seguro con el GIL
    try:
        with open(part, "wb") as out:
            out.truncate(manifest.size)

        def fetch_and_write(block: BlockLocation) -> None:
            data = fetch_block(api, block, path=manifest.path, version=manifest.version, suspects=suspects)
            with open(part, "r+b") as out:  # cada hilo, su propio manejador
                out.seek(block.index * manifest.block_size)
                out.write(data)

        run_parallel(fetch_and_write, manifest.blocks, parallel)
        if manifest.file_sha256 is not None and file_sha256(part) != manifest.file_sha256:
            raise DFSError(ErrorCode.E_CHECKSUM, "el archivo descargado no coincide con su SHA-256")
        os.replace(part, local_path)
    except BaseException:
        part.unlink(missing_ok=True)
        raise


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        while chunk := source.read(READ_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


# --- Paralelismo ---


def run_parallel(task: Callable, items: list, parallel: int) -> None:
    """Corre task(item) para cada item con `parallel` hilos. Si uno falla, cancela los que no
    empezaron, espera a los que están en curso y lanza ese error."""
    pool = ThreadPoolExecutor(max_workers=parallel)
    try:
        for future in as_completed([pool.submit(task, item) for item in items]):
            future.result()
    finally:
        pool.shutdown(wait=True, cancel_futures=True)
