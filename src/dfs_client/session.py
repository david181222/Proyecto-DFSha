"""Sesión del cliente dfs: usuario, JWT y directorio actual, en ~/.dfs/session.json (tarea 16).

cd y pwd son estado del cliente y no tienen ruta en la API (docs/contratos.md, sección 1):
el directorio actual vive aquí, y Session.resolve convierte las rutas relativas que escribe
el usuario en las rutas absolutas que pide la API.

El archivo guarda el JWT, así que en POSIX queda con permisos 600 (solo el dueño lo lee).
Las pruebas cambian SESSION_FILE con monkeypatch.
"""

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path

SESSION_FILE = Path.home() / ".dfs" / "session.json"


@dataclass
class Session:
    user: str
    token: str
    cwd: str = "/"

    def resolve(self, path: str) -> str:
        """Ruta absoluta a partir de cwd. Maneja . y .., junta // y quita la / final.

        Nunca sale de /: .. en la raíz se queda en la raíz, como en Linux. Una ruta
        absoluta también se normaliza.
        """
        start = [] if path.startswith("/") else self.cwd.split("/")
        parts: list[str] = []
        for name in [*start, *path.split("/")]:
            if name in ("", "."):
                continue
            if name == "..":
                if parts:
                    parts.pop()
                continue
            parts.append(name)
        return "/" + "/".join(parts)


def load() -> Session | None:
    """La sesión guardada, o None si no hay una (o si el archivo está dañado)."""
    try:
        return Session(**json.loads(SESSION_FILE.read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError):
        return None


def save(current: Session) -> None:
    SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
    # El modo 0o600 de os.open aplica al crear; chmod lo corrige si el archivo ya existía.
    fd = os.open(SESSION_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as out:
        json.dump(asdict(current), out)
    if os.name == "posix":
        os.chmod(SESSION_FILE, 0o600)


def clear() -> None:
    SESSION_FILE.unlink(missing_ok=True)
