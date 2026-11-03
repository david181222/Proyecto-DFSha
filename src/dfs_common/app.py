"""Creación de las apps FastAPI de DFSha, con el mismo manejo de errores en todas."""

from typing import Any

from fastapi import FastAPI

from dfs_common.errors import install_error_handlers

API_VERSION = "0.1.0"

# Cuerpo binario de los bloques (DA-06): se documenta a mano porque la ruta lee
# el cuerpo directamente de la petición, sin un modelo.
BINARY_REQUEST_BODY: dict[str, Any] = {
    "requestBody": {
        "required": True,
        "content": {"application/octet-stream": {"schema": {"type": "string", "format": "binary"}}},
    }
}

MTLS_SCHEME: dict[str, Any] = {
    "type": "mutualTLS",
    "description": "TLS mutuo con un certificado de servidor firmado por la CA propia (DA-06, DA-11).",
}


def create_app(title: str, description: str, mtls: bool = False, lifespan: Any = None) -> FastAPI:
    """Crea una app de DFSha. mtls=True para las APIs internas (puertos 8444 y 9444).

    lifespan: lo que la app hace al arrancar y al apagarse (por ejemplo, conectarse a etcd).
    Corre al levantar el servidor, nunca al importar.
    """
    app = FastAPI(
        title=title,
        description=description,
        version=API_VERSION,
        # operationId = nombre de la función, en vez del nombre largo que arma FastAPI.
        generate_unique_id_function=lambda route: route.name,
        lifespan=lifespan,
    )
    install_error_handlers(app)
    _adjust_openapi(app, mtls)
    return app


def _adjust_openapi(app: FastAPI, mtls: bool) -> None:
    """Ajusta el OpenAPI que genera FastAPI a las reglas de DFSha.

    - Quita el 422 "Validation Error" que FastAPI agrega por su cuenta: en DFSha los
      parámetros inválidos responden 400 E_BAD_REQUEST (errors.handle_validation_error).
    - En las APIs internas declara mTLS, que FastAPI no sabe documentar.
    """
    build_openapi = app.openapi

    def openapi() -> dict[str, Any]:
        schema = build_openapi()
        for operations in schema.get("paths", {}).values():
            for operation in operations.values():
                responses = operation.get("responses", {})
                if responses.get("422", {}).get("description") == "Validation Error":
                    del responses["422"]
                if mtls:
                    operation["security"] = [{"mTLS": []}]
        components = schema.setdefault("components", {})
        components.get("schemas", {}).pop("HTTPValidationError", None)
        components.get("schemas", {}).pop("ValidationError", None)
        if mtls:
            components.setdefault("securitySchemes", {})["mTLS"] = MTLS_SCHEME
        return schema

    app.openapi = openapi  # type: ignore[method-assign]
