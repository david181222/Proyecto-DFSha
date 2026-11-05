"""FakeDfsApi: un MetaServer y sus DataNodes en memoria, con la interfaz de DfsApi (tarea 17).

La usan las pruebas de put y get (tarea 17) y las del cliente con open/read/write (tarea 18).
Se importa con `from fakes_client import FakeDfsApi` (pytest pone tests/ en sys.path).

Imita lo que el cliente ve del contrato:
- locks por ruta (423 si otro lo tiene, 409 E_LOCK_LOST al renovar uno que ya no existe);
- reservas: present si el bloque ya está guardado, y para cada faltante un primario por turnos
  sin los nodos excluidos; el primario guarda el bloque en él y en los 2 nodos siguientes;
- commit con base_version, lock y que todos los bloques estén guardados (si no, 422 E_REPLICAS);
- manifiestos con réplicas y pases, por versión.

Los DataNodes se llaman dn1..dn4 y su dirección es "dnN:9443". Los pases son textos sin firma:
el fake no los verifica.

Para provocar fallos:
- fail_call[metodo]: lista de errores para las siguientes llamadas a ese método, en orden.
  Cada llamada saca el primero; si es None, la llamada funciona. Un elemento también puede ser
  una función sin argumentos que devuelve el error (o None), para cambiar el estado a la vez.
- on_put(node_id, hash, data): lo que pasa al enviar un bloque a un primario. Devuelve un
  DFSError para que el envío falle, o None para que funcione.
- on_get(node_id, hash): lo que responde una réplica. Devuelve un DFSError, otros bytes (un
  contenido equivocado), o None para entregar el bloque guardado.
- forget_block(hash): borra todas las copias de un bloque (por ejemplo, para que el commit
  responda E_REPLICAS).

calls anota cada llamada como (método, argumentos...).
"""

import hashlib
import threading
from collections.abc import Callable

from dfs_common.config import Settings
from dfs_common.errors import DFSError, ErrorCode
from dfs_common.models import (
    BlockAssignment,
    BlockLocation,
    BlockSpec,
    BlockStoredResponse,
    CommitResponse,
    FileBlocksResponse,
    LockResponse,
    NodeAddress,
    OkResponse,
    ReallocResponse,
    StatResponse,
    UploadResponse,
)

NODES = ["dn1", "dn2", "dn3", "dn4"]
COPIES = 3


def address_of(node_id: str) -> str:
    return f"{node_id}:9443"


class FakeDfsApi:
    def __init__(self, settings: Settings | None = None, nodes: list[str] = NODES) -> None:
        self.settings = settings or Settings(_env_file=None, block_mib=1, zone="us-east-1a")
        self.nodes = list(nodes)
        self.token = "jwt"
        self.calls: list[tuple] = []
        self.fail_call: dict[str, list] = {}
        self.on_put: Callable[[str, str, bytes], DFSError | None] = lambda node_id, hash, data: None
        self.on_get: Callable[[str, str], DFSError | bytes | None] = lambda node_id, hash: None
        # Estado del "clúster"
        self.blocks: dict[str, bytes] = {}  # hash -> contenido
        self.stored_on: dict[str, list[str]] = {}  # hash -> nodos con copia
        self.dirs: set[str] = {"/"}
        self.files: dict[str, list[dict]] = {}  # ruta -> manifiestos, el último es la versión actual
        self.locks: dict[str, str] = {}  # lock_id -> ruta
        self.uploads_by_id: dict[str, dict] = {}
        self._next = 0  # para lock_id, upload_id y el turno de los primarios
        self._mutex = threading.Lock()  # put y get llegan desde varios hilos

    # --- Ayudas para preparar y revisar las pruebas ---

    def add_file(self, path: str, data_blocks: list[bytes], *, block_size: int | None = None) -> None:
        """Guarda un archivo ya publicado, con sus bloques en los 3 primeros nodos."""
        hashes = []
        for data in data_blocks:
            hash = hashlib.sha256(data).hexdigest()
            self.blocks[hash] = data
            self.stored_on[hash] = self.nodes[:COPIES]
            hashes.append(hash)
        whole = b"".join(data_blocks)
        self._publish(path, hashes, len(whole), block_size or self.block_size, hashlib.sha256(whole).hexdigest())

    def forget_block(self, hash: str) -> None:
        self.stored_on.pop(hash, None)

    def names(self) -> list[str]:
        """Los métodos llamados, en orden."""
        return [call[0] for call in self.calls]

    def calls_to(self, name: str) -> list[tuple]:
        return [call[1:] for call in self.calls if call[0] == name]

    @property
    def block_size(self) -> int:
        return self.settings.block_mib * 1024 * 1024

    # --- MetaServer ---

    def stat(self, path: str) -> StatResponse:
        self._record("stat", path)
        if path in self.dirs:
            return StatResponse(path=path, type="dir", size=0, version=0, modified_at=0)
        if path in self.files:
            current = self.files[path][-1]
            return StatResponse(path=path, type="file", size=current["size"], version=current["version"], modified_at=1)
        raise DFSError(ErrorCode.E_NOT_FOUND, f"{path} no existe")

    def lock(self, path: str, wait_s: int = 30) -> LockResponse:
        self._record("lock", path, wait_s)
        if path in self.dirs:
            raise DFSError(ErrorCode.E_BAD_REQUEST, f"{path} es un directorio")
        if path in self.locks.values():
            raise DFSError(ErrorCode.E_LOCKED, "lock ocupado y la espera se agotó o era 0")
        lock_id = f"{self._new_id():x}"
        self.locks[lock_id] = path
        return LockResponse(lock_id=lock_id, lease_s=30)

    def renew_lock(self, lock_id: str) -> LockResponse:
        self._record("renew_lock", lock_id)
        if lock_id not in self.locks:
            raise DFSError(ErrorCode.E_LOCK_LOST, "el lock venció")
        return LockResponse(lock_id=lock_id, lease_s=30)

    def unlock(self, lock_id: str) -> OkResponse:
        self._record("unlock", lock_id)
        self.locks.pop(lock_id, None)
        return OkResponse(ok=True)

    def uploads(self, *, path, mode, base_version, block_size, lock_id, blocks) -> UploadResponse:
        specs = [
            block if isinstance(block, BlockSpec) else BlockSpec(index=block[0], hash=block[1], size=block[2])
            for block in blocks
        ]
        self._record("uploads", path, mode, base_version, block_size, lock_id, [(s.index, s.hash, s.size) for s in specs])
        self._check_lock(lock_id, path)
        upload_id = f"subida-{self._new_id()}"
        self.uploads_by_id[upload_id] = {"path": path, "block_size": block_size, "blocks": {s.index: s for s in specs}}
        present = [s.index for s in specs if self.stored_on.get(s.hash)]
        missing = [self._assign(s, exclude=[]) for s in specs if not self.stored_on.get(s.hash)]
        return UploadResponse(upload_id=upload_id, present=present, missing=missing)

    def realloc(self, upload_id: str, indices, exclude_nodes=()) -> ReallocResponse:
        self._record("realloc", upload_id, list(indices), list(exclude_nodes))
        upload = self.uploads_by_id[upload_id]
        return ReallocResponse(missing=[self._assign(upload["blocks"][i], exclude=list(exclude_nodes)) for i in indices])

    def commit(self, upload_id, *, base_version, lock_id, hashes, size, file_sha256) -> CommitResponse:
        self._record("commit", upload_id, base_version, lock_id, list(hashes), size, file_sha256)
        upload = self.uploads_by_id[upload_id]
        path = upload["path"]
        self._check_lock(lock_id, path)
        current = self.files[path][-1]["version"] if path in self.files else 0
        if base_version != current:
            raise DFSError(ErrorCode.E_VERSION_CONFLICT, f"la versión base {base_version} ya no es la actual ({current})")
        if any(len(self.stored_on.get(hash, [])) < 2 for hash in hashes):
            raise DFSError(ErrorCode.E_REPLICAS, "un bloque no tiene copias vivas en 2 zonas")
        version = self._publish(path, list(hashes), size, upload["block_size"], file_sha256)
        return CommitResponse(path=path, version=version)

    def files_blocks(self, path: str, version: int | None = None) -> FileBlocksResponse:
        self._record("files_blocks", path, version)
        if path in self.dirs:
            raise DFSError(ErrorCode.E_BAD_REQUEST, f"{path} es un directorio")
        manifests = self.files.get(path, [])
        chosen = [m for m in manifests if version is None or m["version"] == version]
        if not chosen:
            raise DFSError(ErrorCode.E_NOT_FOUND, f"{path} no existe en esa versión")
        manifest = chosen[-1]
        self._next += 1  # cada pedido entrega pases nuevos
        blocks = []
        for index, hash in enumerate(manifest["hashes"]):
            size = min(manifest["block_size"], manifest["size"] - index * manifest["block_size"])
            nodes = self.stored_on.get(hash, [])
            blocks.append(
                BlockLocation(
                    index=index,
                    hash=hash,
                    size=size,
                    stored_on=list(nodes),
                    replicas=[NodeAddress(node_id=node, address=address_of(node)) for node in nodes],
                    pass_=f"r-{hash[:8]}-{self._next}",
                )
            )
        return FileBlocksResponse(
            path=path,
            version=manifest["version"],
            size=manifest["size"],
            block_size=manifest["block_size"],
            file_sha256=manifest["file_sha256"],
            blocks=blocks,
        )

    # --- DataNodes ---

    def put_block(self, address: str, hash: str, pase: str, data: bytes) -> BlockStoredResponse:
        node_id = address.split(":")[0]
        self._record("put_block", node_id, hash, pase)
        error = self.on_put(node_id, hash, data)
        if error is not None:
            raise error
        if hashlib.sha256(data).hexdigest() != hash:
            raise DFSError(ErrorCode.E_CHECKSUM, "el contenido no coincide con el hash")
        start = self.nodes.index(node_id)
        stored_on = [self.nodes[(start + step) % len(self.nodes)] for step in range(COPIES)]
        with self._mutex:
            self.blocks[hash] = bytes(data)
            self.stored_on[hash] = stored_on
        return BlockStoredResponse(hash=hash, stored_on=stored_on)

    def get_block(self, address: str, hash: str, pase: str) -> bytes:
        node_id = address.split(":")[0]
        self._record("get_block", node_id, hash, pase)
        answer = self.on_get(node_id, hash)
        if isinstance(answer, DFSError):
            raise answer
        if isinstance(answer, bytes):
            return answer
        if node_id not in self.stored_on.get(hash, []):
            raise DFSError(ErrorCode.E_NOT_FOUND, "el bloque no está en este DataNode")
        return self.blocks[hash]

    # --- Auxiliares ---

    def _record(self, name: str, *args) -> None:
        with self._mutex:
            self.calls.append((name, *args))
            planned = self.fail_call.get(name)
            failure = planned.pop(0) if planned else None
        if callable(failure):
            failure = failure()
        if failure is not None:
            raise failure

    def _new_id(self) -> int:
        with self._mutex:
            self._next += 1
            return self._next

    def _check_lock(self, lock_id: str, path: str) -> None:
        if self.locks.get(lock_id) != path:
            raise DFSError(ErrorCode.E_LOCK_LOST, "el lock venció o es de otro archivo")

    def _assign(self, spec: BlockSpec, exclude: list[str]) -> BlockAssignment:
        candidates = [node for node in self.nodes if node not in exclude]
        if not candidates:
            raise DFSError(ErrorCode.E_UNAVAILABLE, "no quedan DataNodes")
        primary = candidates[self._new_id() % len(candidates)]
        return BlockAssignment(
            index=spec.index,
            hash=spec.hash,
            primary=NodeAddress(node_id=primary, address=address_of(primary)),
            pass_=f"w-{spec.hash[:8]}-{self._next}",
        )

    def _publish(self, path, hashes, size, block_size, file_sha256) -> int:
        manifests = self.files.setdefault(path, [])
        version = manifests[-1]["version"] + 1 if manifests else 1
        manifests.append(
            {"version": version, "hashes": hashes, "size": size, "block_size": block_size, "file_sha256": file_sha256}
        )
        return version
