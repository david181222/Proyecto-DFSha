"""Errores de DFSha: códigos, formato {error, message, retryable} y manejadores de FastAPI.

La tabla ERROR_TABLE es la misma de docs/contratos.md (sección "Errores").
DFSError es la única excepción del proyecto que lleva un código de error: para
responder un error, una ruta lanza DFSError(ErrorCode.X) y el manejador la convierte
en la respuesta HTTP que dice la tabla. Las cuatro apps instalan estos manejadores
(dfs_common.app.create_app); cualquier otra excepción responde 500 E_INTERNAL.
"""

import logging
from enum import StrEnum
from typing import Any, NamedTuple

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict
from starlette.exceptions import HTTPException as StarletteHTTPException


class ErrorCode(StrEnum):
    E_BAD_REQUEST = "E_BAD_REQUEST"
    E_AUTH = "E_AUTH"
    E_FORBIDDEN = "E_FORBIDDEN"
    E_NOT_FOUND = "E_NOT_FOUND"
    E_EXISTS = "E_EXISTS"
    E_NOT_EMPTY = "E_NOT_EMPTY"
    E_VERSION_CONFLICT = "E_VERSION_CONFLICT"
    E_LOCK_LOST = "E_LOCK_LOST"
    E_CHECKSUM = "E_CHECKSUM"
    E_REPLICAS = "E_REPLICAS"
    E_LOCKED = "E_LOCKED"
    E_INTERNAL = "E_INTERNAL"
    E_NOT_IMPLEMENTED = "E_NOT_IMPLEMENTED"
    E_UNAVAILABLE = "E_UNAVAILABLE"
    E_NO_SPACE = "E_NO_SPACE"


class ErrorSpec(NamedTuple):
    http_status: int
    retryable: bool
    description: str  # cuándo ocurre; es también el mensaje por defecto


ERROR_TABLE: dict[ErrorCode, ErrorSpec] = {
    ErrorCode.E_BAD_REQUEST: ErrorSpec(
        400,
        False,
        "parámetros o ruta inválidos, escritura que deja un hueco o trunca el archivo, rm o rmdir sobre el tipo "
        "equivocado, o borrar la raíz /",
    ),
    ErrorCode.E_AUTH: ErrorSpec(401, False, "JWT ausente, inválido o vencido, o pase ausente o vencido"),
    ErrorCode.E_FORBIDDEN: ErrorSpec(
        403,
        False,
        "pase mal formado, con firma inválida, de otro hash o de otra operación; ruta /admin sin rol admin; "
        "lock_id o upload_id de otro usuario; o un nodo que usa la API interna con un node_id ajeno",
    ),
    ErrorCode.E_NOT_FOUND: ErrorSpec(404, False, "ruta, bloque, versión o lease inexistente"),
    ErrorCode.E_EXISTS: ErrorSpec(409, False, "la ruta ya existe"),
    ErrorCode.E_NOT_EMPTY: ErrorSpec(409, False, "directorio no vacío"),
    ErrorCode.E_VERSION_CONFLICT: ErrorSpec(409, False, "la versión base ya no es la actual"),
    ErrorCode.E_LOCK_LOST: ErrorSpec(409, False, "el lock venció o es de otro archivo"),
    ErrorCode.E_CHECKSUM: ErrorSpec(422, True, "el contenido no coincide con el hash, o la copia guardada está dañada"),
    ErrorCode.E_REPLICAS: ErrorSpec(
        422, True, "faltan copias: no se alcanzó DFS_W al escribir, o no hay copias vivas en 2 zonas distintas"
    ),
    ErrorCode.E_LOCKED: ErrorSpec(423, True, "lock ocupado y la espera se agotó o era 0"),
    ErrorCode.E_INTERNAL: ErrorSpec(500, False, "error inesperado del servidor"),
    ErrorCode.E_NOT_IMPLEMENTED: ErrorSpec(501, False, "ruta todavía sin implementar"),
    ErrorCode.E_UNAVAILABLE: ErrorSpec(
        503, True, "etcd sin mayoría o sin DataNodes vivos suficientes"
    ),
    ErrorCode.E_NO_SPACE: ErrorSpec(507, False, "sin espacio en los DataNodes"),
}


logger = logging.getLogger("dfsha")


class ErrorResponse(BaseModel):
    """Cuerpo de toda respuesta de error (DA-06)."""

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "error": "E_VERSION_CONFLICT",
                    "message": "la versión base 3 ya no es la actual (4)",
                    "retryable": False,
                }
            ]
        }
    )

    error: ErrorCode
    message: str
    retryable: bool


class DFSError(Exception):
    """Única excepción del proyecto con código de error: DFSError(code, message).

    El manejador la convierte en ErrorResponse con el HTTP y el retryable de la tabla.
    Si no se da message, usa la descripción de la tabla.
    """

    def __init__(self, code: ErrorCode, message: str | None = None) -> None:
        self.code = code
        self.message = message or ERROR_TABLE[code].description
        super().__init__(f"{code}: {self.message}")


def error_json(code: ErrorCode, message: str) -> JSONResponse:
    spec = ERROR_TABLE[code]
    body = ErrorResponse(error=code, message=message, retryable=spec.retryable)
    return JSONResponse(status_code=spec.http_status, content=body.model_dump(mode="json"))


async def handle_dfs_error(request: Request, exc: DFSError) -> JSONResponse:
    return error_json(exc.code, exc.message)


async def handle_validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    """Los parámetros inválidos responden 400 E_BAD_REQUEST, no el 422 propio de FastAPI."""
    first = exc.errors()[0]
    where = ".".join(str(part) for part in first["loc"])
    # Pydantic antepone "Value error, " a los mensajes de nuestros validadores (check_dfs_path).
    message = first["msg"].removeprefix("Value error, ")
    return error_json(ErrorCode.E_BAD_REQUEST, f"{where}: {message}")


async def handle_http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    """Errores que genera el propio framework: ruta HTTP inexistente o método no permitido."""
    if exc.status_code == 404:
        return error_json(ErrorCode.E_NOT_FOUND, "ruta HTTP inexistente")
    return error_json(ErrorCode.E_BAD_REQUEST, str(exc.detail))


async def handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
    """Cualquier excepción no prevista: la escribe en el log y responde 500 E_INTERNAL.

    El mensaje es el genérico de la tabla, para no mostrarle al cliente detalles internos.
    Starlette vuelve a lanzar la excepción después de responder, así que uvicorn también la registra.
    """
    logger.error("error inesperado en %s %s", request.method, request.url.path, exc_info=exc)
    return error_json(ErrorCode.E_INTERNAL, ERROR_TABLE[ErrorCode.E_INTERNAL].description)


def install_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(DFSError, handle_dfs_error)
    app.add_exception_handler(RequestValidationError, handle_validation_error)
    app.add_exception_handler(StarletteHTTPException, handle_http_error)
    app.add_exception_handler(Exception, handle_unexpected_error)


def error_responses(*codes: ErrorCode) -> dict[int | str, dict[str, Any]]:
    """Arma el parámetro responses= de una ruta: un estado HTTP por grupo de códigos.

    Cada estado lleva el modelo ErrorResponse, una descripción con los códigos
    posibles y un ejemplo por código, para que se vean en el OpenAPI.
    """
    responses: dict[int | str, dict[str, Any]] = {}
    for code in sorted(codes, key=lambda c: ERROR_TABLE[c].http_status):
        spec = ERROR_TABLE[code]
        response = responses.setdefault(
            spec.http_status,
            {"model": ErrorResponse, "description": "", "content": {"application/json": {"examples": {}}}},
        )
        response["description"] = f"{response['description']} `{code}`: {spec.description}.".strip()
        example = {"error": code.value, "message": spec.description, "retryable": spec.retryable}
        response["content"]["application/json"]["examples"][code.value] = {"value": example}
    return responses
