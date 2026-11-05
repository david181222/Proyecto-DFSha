"""Cliente del MetaServer para el DataNode (tarea 14; DA-11).

El DataNode no tiene fijas las direcciones de los MetaServers (RNF7): sabe las IP semilla de
DFS_META_SEEDS y les pide la lista actual. Lo usan la confirmación de un bloque (tarea 14) y
el registro y los latidos (tarea 15).

- Descubrimiento: prueba las semillas en orden con GET https://{ip}:{DFS_META_PUBLIC_PORT}
  /api/v1/cluster/metaservers y guarda la lista.
- Llamadas: van a https://{ip}:{internal_port}{path} de cada MetaServer, por mTLS, empezando
  por el último que respondió. Ante un error de conexión o un 503, pasa al siguiente. Si
  todos fallan, vuelve a descubrir una vez y reintenta una vez; si tampoco, 503 E_UNAVAILABLE.
- Errores: cualquier otra respuesta de error (un 4xx, o un 500) se convierte en un DFSError
  con el mismo código, para que quien llama decida qué hacer.
"""

import logging
import ssl

import httpx

from dfs_common.config import Settings
from dfs_common.errors import DFSError, ErrorCode

logger = logging.getLogger("dfsha")

DISCOVERY_PATH = "/api/v1/cluster/metaservers"


class MetaClient:
    def __init__(
        self, settings: Settings, ssl_context: ssl.SSLContext | None, *, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        """ssl_context: client_ssl_context(ca, cert, key) del nodo, para el mTLS de la API interna.

        transport: solo para las pruebas (httpx.MockTransport). Timeouts por defecto de httpx.
        """
        self._seeds = settings.meta_seeds
        self._public_port = settings.meta_public_port
        self._client = httpx.AsyncClient(verify=ssl_context or True, transport=transport)
        self._metaservers: list[str] = []  # https://ip:internal_port de cada MetaServer conocido
        self._current = 0  # índice del último que respondió

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "MetaClient":
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    async def post(self, path: str, json: dict) -> dict:
        """POST a la API interna de algún MetaServer; devuelve el JSON de la respuesta."""
        if not self._metaservers:
            await self._discover()
        answer = await self._post_to_any(path, json)
        if answer is None:
            await self._discover()  # la lista pudo cambiar: se vuelve a pedir una vez
            answer = await self._post_to_any(path, json)
        if answer is None:
            raise DFSError(ErrorCode.E_UNAVAILABLE, "ningún MetaServer respondió")
        return answer

    async def _discover(self) -> None:
        """Pide la lista de MetaServers a la primera semilla que responda."""
        for seed in self._seeds:
            url = f"https://{seed}:{self._public_port}{DISCOVERY_PATH}"
            try:
                response = await self._client.get(url)
            except httpx.TransportError as exc:
                logger.warning("la semilla %s no respondió: %r", seed, exc)
                continue
            try:
                listed = response.json()["metaservers"] if response.status_code == 200 else None
            except (ValueError, KeyError, TypeError):
                listed = None
            if listed is None:
                logger.warning("la semilla %s no entregó la lista de MetaServers (HTTP %s)", seed, response.status_code)
                continue
            self._metaservers = [f"https://{meta['ip']}:{meta['internal_port']}" for meta in listed]
            self._current = 0
            return
        raise DFSError(ErrorCode.E_UNAVAILABLE, "ninguna semilla de DFS_META_SEEDS respondió")

    async def _post_to_any(self, path: str, json: dict) -> dict | None:
        """Prueba los MetaServers desde el último que respondió. None si ninguno respondió."""
        count = len(self._metaservers)
        for step in range(count):
            index = (self._current + step) % count
            base = self._metaservers[index]
            try:
                response = await self._client.post(base + path, json=json)
            except httpx.TransportError as exc:
                logger.warning("el MetaServer %s no respondió a %s: %r", base, path, exc)
                continue
            if response.status_code == 503:
                logger.warning("el MetaServer %s respondió 503 a %s", base, path)
                continue
            self._current = index
            if response.is_success:
                return response.json()
            raise _as_dfs_error(response)
        return None


def _as_dfs_error(response: httpx.Response) -> DFSError:
    """Una respuesta de error de la API ({error, message, retryable}) como DFSError con el mismo código."""
    try:
        body = response.json()
        return DFSError(ErrorCode(body["error"]), body["message"])
    except (ValueError, KeyError, TypeError):
        return DFSError(ErrorCode.E_UNAVAILABLE, f"respuesta inesperada del MetaServer: HTTP {response.status_code}")
