"""Secretos, JWT del login y pases de bloque (DA-10, docs/contratos.md secciones 7.1 y 7.2).

Funciones puras, sin FastAPI. Las usan el MetaServer (firma JWT y pases) y el
DataNode (verifica pases). `now` va en segundos desde epoch y existe para las
pruebas; si no viene, se usa la hora actual.
"""

import base64
import binascii
import hashlib
import hmac
import time
from pathlib import Path

import jwt
from pydantic import ValidationError

from dfs_common.errors import DFSError, ErrorCode
from dfs_common.models import BlockPass, JwtClaims

JWT_ALGORITHM = "HS256"
JWT_CLAIMS = ["sub", "admin", "iat", "exp"]


def read_secret(path, size: int | None = None) -> bytes:
    """Devuelve los bytes del archivo tal cual; con size, exige ese largo exacto.

    Los mensajes de error nombran la ruta y nunca el contenido. Son excepciones
    estándar porque esto ocurre al arrancar: si falta un secreto, el proceso se cae.
    """
    path = Path(path)
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        raise FileNotFoundError(f"no existe el archivo de secreto {path}") from None
    if size is not None and len(data) != size:
        raise ValueError(f"el secreto {path} mide {len(data)} bytes; se esperaban {size}")
    return data


# --- JWT del login (sección 7.1) ---


def create_jwt(user: str, admin: bool, secret: bytes, ttl_s: int, now: int | None = None) -> str:
    iat = int(time.time()) if now is None else now
    claims = {"sub": user, "admin": admin, "iat": iat, "exp": iat + ttl_s}
    return jwt.encode(claims, secret, algorithm=JWT_ALGORITHM)


def verify_jwt(token: str, secret: bytes) -> JwtClaims:
    """Verifica firma, vencimiento y claims. Cualquier fallo es 401 E_AUTH.

    algorithms fijo: se ignora el alg que diga el token, así que "alg: none" y
    cualquier otro algoritmo quedan rechazados.
    """
    try:
        payload = jwt.decode(token, secret, algorithms=[JWT_ALGORITHM], options={"require": JWT_CLAIMS})
        return JwtClaims.model_validate(payload)
    except (jwt.PyJWTError, ValidationError):
        raise DFSError(ErrorCode.E_AUTH, "JWT ausente, inválido o vencido") from None


# --- Pase de bloque (sección 7.2) ---


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(text: str) -> bytes:
    """Decodifica base64url sin relleno; lanza ValueError si no es base64url válido."""
    try:
        return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except (binascii.Error, ValueError) as exc:
        raise ValueError("base64url inválido") from exc


def _pass_signature(payload: bytes, key: bytes) -> bytes:
    return hmac.new(key, payload, hashlib.sha256).digest()


def sign_pass(claims: BlockPass, key: bytes) -> str:
    """base64url(json).base64url(hmac_sha256(key, json)), sin relleno "=".

    El JSON es compacto y en el orden de campos del modelo; un pase de lectura
    no lleva secondaries (exclude_none).
    """
    payload = claims.model_dump_json(exclude_none=True).encode("utf-8")
    return f"{_b64url_encode(payload)}.{_b64url_encode(_pass_signature(payload, key))}"


def verify_pass(token: str, key: bytes, *, hash: str, op: str, now: int | None = None) -> BlockPass:
    """Verifica un pase para el bloque `hash` y la operación `op` ("r" o "w").

    La firma se comprueba sobre los bytes recibidos, sin volver a serializar, y
    antes de mirar el contenido. Firma válida pero vencido: 401 E_AUTH (el cliente
    pide otro). Mal formado, firma mala, otro hash u otra operación: 403 E_FORBIDDEN.
    """
    parts = token.split(".")
    if len(parts) != 2:
        raise DFSError(ErrorCode.E_FORBIDDEN, "pase mal formado")
    try:
        payload, signature = _b64url_decode(parts[0]), _b64url_decode(parts[1])
    except ValueError:
        raise DFSError(ErrorCode.E_FORBIDDEN, "pase mal formado") from None
    if not hmac.compare_digest(_pass_signature(payload, key), signature):
        raise DFSError(ErrorCode.E_FORBIDDEN, "pase con firma inválida")
    try:
        block_pass = BlockPass.model_validate_json(payload)
    except ValidationError:
        raise DFSError(ErrorCode.E_FORBIDDEN, "pase mal formado") from None
    if block_pass.exp <= (int(time.time()) if now is None else now):
        raise DFSError(ErrorCode.E_AUTH, "pase vencido")
    if block_pass.hash != hash:
        raise DFSError(ErrorCode.E_FORBIDDEN, "el pase es de otro bloque")
    if block_pass.op != op:
        raise DFSError(ErrorCode.E_FORBIDDEN, "el pase es de otra operación")
    return block_pass
