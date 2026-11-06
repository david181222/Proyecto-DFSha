"""Pruebas de la tarea 23: el despliegue en AWS lleva los mismos tiempos de DA-16 que el local.

Los scripts de infra/ se revisan con bash -n y shellcheck, y se prueban de verdad en AWS (tarea 24).
Aquí solo se comprueba que las variables de tiempo sean las mismas en el bloque x-dfs-times de
compose.local.yml y de compose.aws.yml, en TIME_VARS de infra/config.sh (las que deploy.sh lleva
a las VMs) y en Settings: si se agrega una variable y se olvida en un lugar, la prueba lo dice.
"""

import re
from pathlib import Path

from dfs_common.config import Settings

ROOT = Path(__file__).resolve().parents[1]
TIME_LINE = re.compile(r"^\s+(DFS_[A-Z0-9_]+): \$\{\1:-\}$", re.MULTILINE)


def compose_time_vars(name: str) -> list[str]:
    """Las variables del bloque x-dfs-times de un archivo de Compose, en orden."""
    text = (ROOT / name).read_text(encoding="utf-8")
    block = text.split("x-dfs-times: &dfs-times\n", 1)[1].split("\n\n", 1)[0]
    return TIME_LINE.findall(block)


def deploy_time_vars() -> list[str]:
    """TIME_VARS de infra/config.sh, en orden."""
    text = (ROOT / "infra" / "config.sh").read_text(encoding="utf-8")
    return re.search(r"^TIME_VARS=\((.*?)\)", text, re.MULTILINE | re.DOTALL).group(1).split()


def test_local_compose_has_time_variables():
    assert len(compose_time_vars("compose.local.yml")) == 16


def test_aws_compose_has_the_same_time_variables_as_local():
    assert compose_time_vars("compose.aws.yml") == compose_time_vars("compose.local.yml")


def test_deploy_carries_every_time_variable():
    assert deploy_time_vars() == compose_time_vars("compose.local.yml")


def test_time_variables_are_settings():
    fields = {f"DFS_{name.upper()}" for name in Settings.model_fields}
    assert set(deploy_time_vars()) <= fields
