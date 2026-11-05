"""DfsApi: el cliente HTTP de DFSha, con un método por ruta del contrato (tarea 16; DA-06, DA-11).

Es síncrono (httpx.Client): la tarea 17 lo usa desde un ThreadPoolExecutor y la 18 desde un
hilo de fondo; httpx.Client se puede compartir entre hilos.

- Descubrimiento: el cliente no tiene fijas las direcciones de los MetaServers (RNF7). Prueba
  las semillas de DFS_META_SEEDS en orden con GET https://{ip}:{DFS_META_PUBLIC_PORT}
  /api/v1/cluster/metaservers y guarda la lista, con el public_port de cada uno.
- Llamadas al MetaServer: empiezan por el último que respondió. Ante un error de conexión o un
  503, pasan al siguiente. Si ninguno responde, se vuelve a descubrir una vez y se reintenta una
  vez; si tampoco, 503 E_UNAVAILABLE. Es el mismo comportamiento de MetaClient en el DataNode.
  Riesgo aceptado: una escritura que sí se aplicó y cuya respuesta se perdió se repite en otro
  MetaServer, y puede responder 409 o 404 aunque haya salido bien (contrato, sección 6).
- Bloques: put_block y get_block van directo al DataNode, sin MetaServer. Si el DataNode no
  responde, DFSError E_UNAVAILABLE, para que quien llama decida solo por el código.
- Errores: una respuesta {error, message, retryable} se convierte en DFSError(code, message).
  Una que no tiene ese formato (por ejemplo, un 502 de un proxy) es E_UNAVAILABLE.
- TLS: verifica a los servidores con la CA propia (DFS_TLS_CA_FILE), sin certificado de cliente.
- Timeouts: lock espera wait_s + DFS_LOCK_RENEW_S; put_block y get_block, 3 × DFS_PRIMARY_WAIT_S
  (el primario espera hasta DFS_PRIMARY_WAIT_S a los secundarios); los demás, los de httpx.
"""

from typing import Any, Iterable

import httpx

from dfs_common.config import Settings
from dfs_common.errors import DFSError, ErrorCode
from dfs_common.models import (
    AdminStatusResponse,
    BlockSpec,
    BlockStoredResponse,
    CommitResponse,
    FileBlocksResponse,
    LockResponse,
    LoginResponse,
    LsResponse,
    MetaServerListResponse,
    OkResponse,
    ReallocResponse,
    StatResponse,
    UploadResponse,
)
from dfs_common.tls import client_ssl_context

API = "/api/v1"
DISCOVERY_PATH = f"{API}/cluster/metaservers"
DEFAULT_WAIT_S = 30  # wait_s por defecto del contrato (LockRequest)


class DfsApi:
    def __init__(self, settings: Settings, token: str | None = None, *, transport: httpx.BaseTransport | None = None):
        """token: el JWT del login, o None antes de iniciar sesión.

        transport: solo para las pruebas (httpx.MockTransport); con él no se lee la CA.
        """
        self.settings = settings
        self.token = token
        verify = True if transport is not None else client_ssl_context(settings.tls_ca_file)
        self._client = httpx.Client(verify=verify, transport=transport)
        self._metaservers: list[str] = []  # https://ip:public_port de cada MetaServer conocido
        self._current = 0  # índice del último que respondió

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "DfsApi":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # --- Control ---

    def login(self, user: str, password: str) -> LoginResponse:
        """Inicia sesión y deja el token nuevo en self.token."""
        answer = LoginResponse.model_validate(
            self._call("POST", "/auth/login", json={"user": user, "password": password}, auth=False)
        )
        self.token = answer.token
        return answer

    def ls(self, path: str) -> LsResponse:
        return LsResponse.model_validate(self._call("GET", "/fs/ls", params={"path": path}))

    def stat(self, path: str) -> StatResponse:
        return StatResponse.model_validate(self._call("GET", "/fs/stat", params={"path": path}))

    def mkdir(self, path: str) -> OkResponse:
        return OkResponse.model_validate(self._call("POST", "/fs/mkdir", json={"path": path}))

    def rmdir(self, path: str) -> OkResponse:
        return OkResponse.model_validate(self._call("DELETE", "/fs/rmdir", params={"path": path}))

    def rm(self, path: str) -> OkResponse:
        return OkResponse.model_validate(self._call("DELETE", "/fs/rm", params={"path": path}))

    def add_user(self, user: str, password: str) -> OkResponse:
        return OkResponse.model_validate(self._call("POST", "/admin/users", json={"user": user, "password": password}))

    def admin_status(self) -> AdminStatusResponse:
        return AdminStatusResponse.model_validate(self._call("GET", "/admin/status"))

    # --- Escritura ---

    def lock(self, path: str, wait_s: int = DEFAULT_WAIT_S) -> LockResponse:
        """Toma el lock. Espera en la fila hasta wait_s, más un margen de DFS_LOCK_RENEW_S para la respuesta."""
        timeout = wait_s + self.settings.lock_renew_s
        answer = self._call("POST", "/locks", json={"path": path, "wait_s": wait_s}, timeout=timeout)
        return LockResponse.model_validate(answer)

    def renew_lock(self, lock_id: str) -> LockResponse:
        return LockResponse.model_validate(self._call("PUT", f"/locks/{lock_id}"))

    def unlock(self, lock_id: str) -> OkResponse:
        return OkResponse.model_validate(self._call("DELETE", f"/locks/{lock_id}"))

    def files_blocks(self, path: str, version: int | None = None) -> FileBlocksResponse:
        """Manifiesto con réplicas y pases de lectura; las réplicas de DFS_ZONE van primero (DA-11)."""
        params: dict[str, Any] = {"path": path}
        if version is not None:
            params["version"] = version
        headers = {"X-Client-Zone": self.settings.zone} if self.settings.zone else {}
        return FileBlocksResponse.model_validate(self._call("GET", "/files/blocks", params=params, headers=headers))

    def uploads(
        self,
        *,
        path: str,
        mode: str,
        base_version: int,
        block_size: int,
        lock_id: str,
        blocks: Iterable[BlockSpec | tuple[int, str, int]],
    ) -> UploadResponse:
        """Reserva una subida. blocks: los bloques nuevos o cambiados, como BlockSpec o (index, hash, size)."""
        body = {
            "path": path,
            "mode": mode,
            "base_version": base_version,
            "block_size": block_size,
            "lock_id": lock_id,
            "blocks": [_block_spec(block) for block in blocks],
        }
        return UploadResponse.model_validate(self._call("POST", "/uploads", json=body))

    def realloc(self, upload_id: str, indices: Iterable[int], exclude_nodes: Iterable[str] = ()) -> ReallocResponse:
        body = {"indices": list(indices), "exclude_nodes": list(exclude_nodes)}
        return ReallocResponse.model_validate(self._call("POST", f"/uploads/{upload_id}/realloc", json=body))

    def commit(
        self, upload_id: str, *, base_version: int, lock_id: str, hashes: list[str], size: int, file_sha256: str | None
    ) -> CommitResponse:
        body = {
            "base_version": base_version,
            "lock_id": lock_id,
            "hashes": list(hashes),
            "size": size,
            "file_sha256": file_sha256,
        }
        return CommitResponse.model_validate(self._call("POST", f"/uploads/{upload_id}/commit", json=body))

    # --- Bloques: directo al DataNode ---

    def put_block(self, address: str, hash: str, pase: str, data: bytes) -> BlockStoredResponse:
        """PUT /blocks/{hash} al primario, con el pase de escritura. Responde dónde quedó el bloque."""
        response = self._block_request(
            "PUT", address, hash, pase, content=data, headers={"Content-Type": "application/octet-stream"}
        )
        return BlockStoredResponse.model_validate(response.json())

    def get_block(self, address: str, hash: str, pase: str) -> bytes:
        """GET /blocks/{hash} a una réplica, con el pase de lectura. Devuelve el bloque sin cifrar."""
        return self._block_request("GET", address, hash, pase).content

    # --- Auxiliares ---

    def _call(self, method: str, path: str, *, auth: bool = True, **request: Any) -> dict:
        """Una llamada a la API pública de algún MetaServer; devuelve el JSON de la respuesta."""
        if auth and self.token:
            request["headers"] = {**request.get("headers", {}), "Authorization": f"Bearer {self.token}"}
        if not self._metaservers:
            self._discover()
        response = self._send_to_any(method, path, request)
        if response is None:
            self._discover()  # la lista pudo cambiar: se vuelve a pedir una vez
            response = self._send_to_any(method, path, request)
        if response is None:
            raise DFSError(ErrorCode.E_UNAVAILABLE, "ningún MetaServer respondió")
        if response.is_success:
            return response.json()
        raise as_dfs_error(response)

    def _discover(self) -> None:
        """Pide la lista de MetaServers a la primera semilla que responda."""
        for seed in self.settings.meta_seeds:
            try:
                response = self._client.get(f"https://{seed}:{self.settings.meta_public_port}{DISCOVERY_PATH}")
            except httpx.TransportError:
                continue
            if response.status_code != 200:
                continue
            try:
                listed = MetaServerListResponse.model_validate(response.json()).metaservers
            except ValueError:
                continue
            self._metaservers = [f"https://{meta.ip}:{meta.public_port}" for meta in listed]
            self._current = 0
            return
        raise DFSError(ErrorCode.E_UNAVAILABLE, "ninguna semilla de DFS_META_SEEDS respondió")

    def _send_to_any(self, method: str, path: str, request: dict) -> httpx.Response | None:
        """Prueba los MetaServers desde el último que respondió. None si ninguno respondió."""
        count = len(self._metaservers)
        for step in range(count):
            index = (self._current + step) % count
            try:
                response = self._client.request(method, self._metaservers[index] + API + path, **request)
            except httpx.TransportError:
                continue
            if response.status_code == 503:
                continue
            self._current = index
            return response
        return None

    def _block_request(self, method: str, address: str, hash: str, pase: str, **request: Any) -> httpx.Response:
        headers = {**request.pop("headers", {}), "Authorization": f"Bearer {pase}"}
        url = f"https://{address}{API}/blocks/{hash}"
        try:
            response = self._client.request(
                method, url, headers=headers, timeout=3 * self.settings.primary_wait_s, **request
            )
        except httpx.TransportError as exc:
            raise DFSError(ErrorCode.E_UNAVAILABLE, f"el DataNode {address} no respondió: {exc!r}") from None
        if not response.is_success:
            raise as_dfs_error(response)
        return response


def as_dfs_error(response: httpx.Response) -> DFSError:
    """Una respuesta de error de la API ({error, message, retryable}) como DFSError con el mismo código."""
    try:
        answer = response.json()
        return DFSError(ErrorCode(answer["error"]), answer["message"])
    except (ValueError, KeyError, TypeError):
        return DFSError(ErrorCode.E_UNAVAILABLE, f"respuesta inesperada del servidor: HTTP {response.status_code}")


def _block_spec(block: BlockSpec | tuple[int, str, int]) -> dict:
    if isinstance(block, BlockSpec):
        return block.model_dump()
    index, hash, size = block
    return {"index": index, "hash": hash, "size": size}
