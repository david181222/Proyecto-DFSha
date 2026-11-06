"""open, read, write y close del cliente (tarea 18, RF3 y DA-14).

Semántica que declara DFSha (RF3):

| Modo | Al abrir                                              | Lock |
|------|-------------------------------------------------------|------|
| r    | lee la versión que había al abrir; la versión queda fija | no   |
| w    | crea o reemplaza: empieza vacío                       | sí   |
| a    | anexa: cada write va al final, sin importar el offset | sí   |
| rw   | modifica partes de la versión actual                  | sí   |

- read(offset, size) funciona en los cuatro modos y ve los cambios propios que todavía no se
  publicaron. Un lector nunca ve una mezcla de versiones (G2).
- write(offset, datos) acepta offsets entre 0 y el tamaño actual: un offset mayor dejaría un
  hueco y es un error. Reescribe completos los bloques que toca (DA-14).
- close() publica la versión nueva de una sola vez: lo escrito se ve después del close, en todo
  open posterior (visibilidad close-to-open, G3).

El lock no es una operación aparte del cliente: lo toma open en w, a y rw, lo renueva un
LockKeeper mientras el archivo está abierto, y lo suelta close. El numeral 7 del enunciado
permite un mecanismo equivalente si se documenta: es este, un escritor por archivo (DA-08). La
versión base se lee con el lock tomado; si al publicar el lock ya venció u otro publicó antes,
el commit responde 409 y los cambios no se publican (G4).

Limitación aceptada: los bloques modificados viven en memoria hasta el close (como mucho
DFS_BLOCK_MIB MiB cada uno), igual que los bloques leídos, que se guardan para no volver a
pedirlos.
"""

import hashlib
import logging
import math

from dfs_client.locks import LockKeeper
from dfs_client.transfer import fetch_block, upload_version
from dfs_common.errors import DFSError, ErrorCode
from dfs_common.models import CommitResponse, FileBlocksResponse

logger = logging.getLogger("dfsha")

MODES = ("r", "w", "a", "rw")
WRITERS = ("w", "a", "rw")
MIB = 1024 * 1024
PARALLEL = 4  # bloques que viajan a la vez en el close, como dfs put
LOST_MESSAGE = "perdiste el lock o alguien publicó otra versión; tus cambios no se publicaron"


class DfsFile:
    """Un archivo abierto. Se crea con DfsFile.open; también sirve como context manager:
    al salir del with sin errores hace close, y con un error suelta el lock sin publicar."""

    def __init__(self, api, path: str, mode: str) -> None:
        self.api = api
        self.path = path
        self.mode = mode
        self.version = 0  # en r, la versión fija; en w, a y rw, la versión base
        self.size = 0
        self.block_size = api.settings.block_mib * MIB
        self.base_hashes: list[str] = []
        self.manifest: FileBlocksResponse | None = None
        self.modified: dict[int, bytearray] = {}  # índice -> bloque cambiado, sin publicar
        self.fetched: dict[int, bytes] = {}  # índice -> bloque de la versión fija, ya descargado
        self.lock_id: str | None = None
        self.keeper: LockKeeper | None = None
        self.closed = False

    # --- Abrir ---

    @classmethod
    def open(cls, api, path: str, mode: str, wait_s: int = 30, renew: bool = True) -> "DfsFile":
        """Abre path. En w, a y rw toma el lock (esperando hasta wait_s) y lo renueva si renew."""
        if mode not in MODES:
            raise DFSError(ErrorCode.E_BAD_REQUEST, f"modo {mode!r} inválido: usa r, w, a o rw")
        file = cls(api, path, mode)
        if mode == "r":
            file._load(api.files_blocks(path))
            return file
        lock = api.lock(path, wait_s)
        file.lock_id = lock.lock_id
        file.keeper = LockKeeper(api, lock.lock_id, renew).__enter__()
        try:
            file._load_base()
        except BaseException:
            file._release()
            raise
        return file

    def _load_base(self) -> None:
        """Con el lock tomado: la versión actual es la base (0 si el archivo no existe)."""
        try:
            info = self.api.stat(self.path)
        except DFSError as error:
            if error.code != ErrorCode.E_NOT_FOUND:
                raise
            return  # archivo nuevo: base 0, vacío
        if info.type == "dir":
            raise DFSError(ErrorCode.E_BAD_REQUEST, f"{self.path} es un directorio")
        self.version = info.version
        if self.mode in ("a", "rw"):
            self._load(self.api.files_blocks(self.path, info.version))
        # w empieza vacío, con el tamaño de bloque de la configuración

    def _load(self, manifest: FileBlocksResponse) -> None:
        self.manifest = manifest
        self.version = manifest.version
        self.size = manifest.size
        self.block_size = manifest.block_size
        self.base_hashes = [block.hash for block in manifest.blocks]

    # --- Leer y escribir ---

    def read(self, offset: int, size: int) -> bytes:
        """Hasta size bytes desde offset; menos si llega al final. Ve los cambios propios sin publicar."""
        self._check_open()
        if offset < 0 or size < 0:
            raise DFSError(ErrorCode.E_BAD_REQUEST, "offset y tamaño no pueden ser negativos")
        end = min(offset + size, self.size)
        if offset >= end:
            return b""
        parts = []
        for index in range(offset // self.block_size, (end - 1) // self.block_size + 1):
            start = index * self.block_size
            block = self._block(index)
            parts.append(block[max(offset - start, 0) : end - start])
        return b"".join(parts)

    def write(self, offset: int, data: bytes) -> int:
        """Escribe data en offset (en a, al final) y devuelve cuántos bytes escribió.

        Carga los bloques que toca, si no están ya modificados, aplica el cambio y puede agregar
        bloques al final. Un offset mayor que el tamaño actual dejaría un hueco: 400.
        """
        self._check_open()
        if self.mode == "r":
            raise DFSError(ErrorCode.E_BAD_REQUEST, f"{self.path} está abierto en modo r")
        if self.mode == "a":
            offset = self.size
        if not 0 <= offset <= self.size:
            raise DFSError(ErrorCode.E_BAD_REQUEST, f"offset {offset} fuera de 0..{self.size}: no se admiten huecos")
        end = offset + len(data)
        for index in range(offset // self.block_size, math.ceil(end / self.block_size)):
            start = index * self.block_size
            block = self.modified.get(index)
            if block is None:
                block = bytearray(self._block(index)) if start < self.size else bytearray()
            begin = max(offset - start, 0)
            piece = data[start + begin - offset : min(end, start + self.block_size) - offset]
            block[begin : begin + len(piece)] = piece
            self.modified[index] = block
        self.size = max(self.size, end)
        return len(data)

    def _block(self, index: int) -> bytes:
        """El bloque index como lo ve este archivo abierto: el cambiado, o el de la versión fija."""
        if index in self.modified:
            return bytes(self.modified[index])
        if index in self.fetched:
            return self.fetched[index]
        if self.manifest is None or index >= len(self.manifest.blocks):
            return b""
        try:
            data = fetch_block(self.api, self.manifest.blocks[index], path=self.path, version=self.version)
        except DFSError as error:
            if error.code == ErrorCode.E_NOT_FOUND:
                # El pase venció y la versión ya no está retenida (DA-14).
                message = f"la versión {self.version} de {self.path} ya no está retenida; reabre el archivo"
                raise DFSError(ErrorCode.E_NOT_FOUND, message) from None
            raise
        self.fetched[index] = data
        return data

    # --- Cerrar ---

    def close(self) -> CommitResponse | None:
        """Cierra. Un escritor publica la versión nueva y suelta el lock; devuelve el commit, o None
        si no publicó nada (r, o a y rw sin cambios). En w, cerrar sin escribir publica un archivo
        vacío: es lo que quiere decir "crear o reemplazar".
        """
        self._check_open()
        self.closed = True
        if self.mode == "r":
            return None
        try:
            self.keeper.check()
            if not self.modified and self.mode != "w":
                return None
            return self._publish()
        except DFSError as error:
            if error.code in (ErrorCode.E_LOCK_LOST, ErrorCode.E_VERSION_CONFLICT):
                raise DFSError(error.code, LOST_MESSAGE) from None
            raise
        finally:
            self._release()

    def abort(self) -> None:
        """Cierra sin publicar: los cambios se descartan y el lock se suelta."""
        if not self.closed:
            self.closed = True
            self._release()

    def _publish(self) -> CommitResponse:
        count = math.ceil(self.size / self.block_size)
        hashes = [
            hashlib.sha256(self.modified[index]).hexdigest() if index in self.modified else self.base_hashes[index]
            for index in range(count)
        ]
        blocks = [(index, hashes[index], len(self.modified[index])) for index in sorted(self.modified)]
        # En w todo el contenido está en memoria; en a y rw no, y el contrato acepta null (tarea 12).
        whole = None
        if self.mode == "w":
            whole = hashlib.sha256(b"".join(self.modified[index] for index in range(count))).hexdigest()
        return upload_version(
            self.api,
            path=self.path,
            mode=self.mode,
            base_version=self.version,
            lock_id=self.lock_id,
            block_size=self.block_size,
            blocks=blocks,
            hashes=hashes,
            size=self.size,
            file_sha256=whole,
            get_block_bytes=lambda index: bytes(self.modified[index]),
            parallel=PARALLEL,
            keeper=self.keeper,
        )

    def _release(self) -> None:
        """Detiene la renovación y suelta el lock. Si unlock falla, el lease vence solo."""
        if self.keeper is not None:
            self.keeper.stop()
        if self.lock_id is not None:
            try:
                self.api.unlock(self.lock_id)
            except DFSError as error:
                logger.warning("no se pudo liberar el lock de %s (%s); vence solo", self.path, error.code.value)

    def _check_open(self) -> None:
        if self.closed:
            raise DFSError(ErrorCode.E_BAD_REQUEST, f"{self.path} ya está cerrado")

    # --- Context manager ---

    def __enter__(self) -> "DfsFile":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        if exc_type is None:
            if not self.closed:
                self.close()
        else:
            self.abort()
