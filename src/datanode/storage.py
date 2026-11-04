"""Almacén de bloques del DataNode: cifrado en disco, escritura atómica y borrado por fecha.

Cada bloque vive en {data_dir}/{user}/{hash} (DA-02) con este contenido:
nonce (12 bytes) + texto cifrado + tag (16 bytes). Se cifra con AES-256-GCM, con la
clave de este nodo y "{user}/{hash}" como dato asociado: un archivo copiado con
otro nombre no se descifra (DA-09).

El bloque se escribe por partes en un temporal de la misma carpeta y se renombra al
final, así que nunca queda publicado a medias y el texto en claro nunca toca el disco.
"""

import errno
import hashlib
import os
import re
import shutil
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers import Cipher
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.ciphers.algorithms import AES
from cryptography.hazmat.primitives.ciphers.modes import GCM

from dfs_common.errors import DFSError, ErrorCode
from dfs_common.models import SHA256_PATTERN, USER_PATTERN

NONCE_BYTES = 12
TAG_BYTES = 16
TMP_MARK = ".tmp-"


class BlockStore:
    def __init__(self, data_dir: Path, node_key: bytes, max_block_bytes: int) -> None:
        self.data_dir = Path(data_dir)
        self.node_key = node_key
        self.max_block_bytes = max_block_bytes
        # Bloques que faltaron o estaban dañados al leerlos; el latido los manda en bad_blocks.
        # Un dict sin valores guarda el orden de llegada y no repite (user, hash).
        self._bad_blocks: dict[tuple[str, str], None] = {}
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._remove_temporaries()

    # --- Operaciones ---

    async def write(self, user: str, hash: str, chunks: AsyncIterator[bytes]) -> None:
        """Recibe el bloque sin cifrar por partes, lo verifica, lo cifra y lo publica.

        Siempre reescribe, aunque el bloque ya exista: la fecha de modificación nueva es el
        instante de escritura que usa la regla del instante T (DA-02).
        400 si pasa de max_block_bytes; 422 si el hash no coincide; 507 si el disco se llenó.
        """
        path = self._path(user, hash)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{hash}{TMP_MARK}{uuid.uuid4().hex}")
        nonce = os.urandom(NONCE_BYTES)
        encryptor = Cipher(AES(self.node_key), GCM(nonce)).encryptor()
        encryptor.authenticate_additional_data(self._associated_data(user, hash))
        digest = hashlib.sha256()
        size = 0
        try:
            with open(tmp, "wb") as out:
                out.write(nonce)
                async for chunk in chunks:
                    size += len(chunk)
                    if size > self.max_block_bytes:
                        raise DFSError(ErrorCode.E_BAD_REQUEST, f"el bloque pasa de {self.max_block_bytes} bytes")
                    digest.update(chunk)
                    out.write(encryptor.update(chunk))
                if digest.hexdigest() != hash:
                    raise DFSError(ErrorCode.E_CHECKSUM, "el contenido no coincide con el hash")
                out.write(encryptor.finalize())
                out.write(encryptor.tag)
                out.flush()
                os.fsync(out.fileno())
            os.replace(tmp, path)
        except BaseException as exc:
            tmp.unlink(missing_ok=True)
            if isinstance(exc, OSError) and exc.errno == errno.ENOSPC:
                raise DFSError(ErrorCode.E_NO_SPACE, "el disco del DataNode está lleno") from None
            raise
        self._fsync_dir(path.parent)

    def read(self, user: str, hash: str) -> bytes:
        """Devuelve el bloque descifrado. 404 si no está; 422 si está dañado, y entonces lo borra.

        En los dos casos lo anota en los bloques dañados: el MetaServer creía que estaba aquí.
        """
        path = self._path(user, hash)
        try:
            stored = path.read_bytes()
        except FileNotFoundError:
            self._bad_blocks[(user, hash)] = None
            raise DFSError(ErrorCode.E_NOT_FOUND, "el bloque no está en este DataNode") from None
        try:
            # AESGCM valida el tag antes de entregar nada; espera el texto cifrado seguido del tag.
            data = AESGCM(self.node_key).decrypt(
                stored[:NONCE_BYTES], stored[NONCE_BYTES:], self._associated_data(user, hash)
            )
        except (InvalidTag, ValueError):
            data = None
        if data is None or hashlib.sha256(data).hexdigest() != hash:
            path.unlink(missing_ok=True)
            self._bad_blocks[(user, hash)] = None
            raise DFSError(ErrorCode.E_CHECKSUM, "la copia guardada está dañada")
        return data

    def delete_if_older(self, user: str, hash: str, written_before_ms: int) -> bool:
        """Borra el bloque solo si se escribió antes de written_before_ms; dice si lo borró (DA-02)."""
        path = self._path(user, hash)
        try:
            written_ms = path.stat().st_mtime_ns // 1_000_000
        except FileNotFoundError:
            return False
        if written_ms >= written_before_ms:
            return False
        path.unlink(missing_ok=True)
        return True

    def pop_bad_blocks(self) -> list[tuple[str, str]]:
        """Entrega los bloques dañados o faltantes anotados desde la última vez y vacía la lista."""
        bad = list(self._bad_blocks)
        self._bad_blocks.clear()
        return bad

    def free_bytes(self) -> int:
        return shutil.disk_usage(self.data_dir).free

    # --- Auxiliares ---

    def _path(self, user: str, hash: str) -> Path:
        # Las rutas HTTP ya validan estos patrones; se repite aquí porque el nombre de
        # usuario y el hash se vuelven una ruta del disco (un "../" saldría de data_dir).
        if not re.fullmatch(USER_PATTERN, user) or not re.fullmatch(SHA256_PATTERN, hash):
            raise DFSError(ErrorCode.E_BAD_REQUEST, "usuario o hash inválido")
        return self.data_dir / user / hash

    @staticmethod
    def _associated_data(user: str, hash: str) -> bytes:
        return f"{user}/{hash}".encode("ascii")

    def _remove_temporaries(self) -> None:
        """Borra los temporales que dejó una caída a mitad de un bloque (escenario 3 del enunciado)."""
        for tmp in self.data_dir.glob(f"*/*{TMP_MARK}*"):
            tmp.unlink(missing_ok=True)

    @staticmethod
    def _fsync_dir(folder: Path) -> None:
        """En POSIX, el renombre queda en disco solo después del fsync de la carpeta; Windows no lo permite."""
        if os.name != "posix":
            return
        fd = os.open(folder, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
