"""Exporta el OpenAPI de las cuatro APIs a docs/api/ (contrato, tarea 01).

Uso, desde la raíz del repositorio y con el paquete instalado (pip install -e ".[dev]"):

    python scripts/export_openapi.py

Si nadie cambió el código, los JSON quedan idénticos. tests/test_contratos.py
falla si los JSON de docs/api/ no coinciden con lo que genera el código.
"""

import json
from pathlib import Path

from datanode.internal_api import app as datanode_internal
from datanode.public_api import app as datanode_public
from metaserver.internal_api import app as metaserver_internal
from metaserver.public_api import app as metaserver_public

API_DIR = Path(__file__).resolve().parents[1] / "docs" / "api"

APPS = {
    "metaserver-public": metaserver_public,
    "metaserver-internal": metaserver_internal,
    "datanode-public": datanode_public,
    "datanode-internal": datanode_internal,
}


def main() -> None:
    API_DIR.mkdir(parents=True, exist_ok=True)
    for name, app in APPS.items():
        path = API_DIR / f"{name}.json"
        text = json.dumps(app.openapi(), indent=2, ensure_ascii=False) + "\n"
        # newline="\n": el mismo archivo en Windows y en Linux.
        path.write_text(text, encoding="utf-8", newline="\n")
        print(f"escrito docs/api/{path.name}")


if __name__ == "__main__":
    main()
