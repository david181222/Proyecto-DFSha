"""LockKeeper: mantiene vivo el lock de un archivo mientras el cliente escribe (tarea 17, DA-08).

Un hilo renueva el lock cada DFS_LOCK_RENEW_S con PUT /locks/{lock_id}.

- Si una renovación responde 409 E_LOCK_LOST, el lock se perdió: marca lost = True y se detiene.
- Cualquier otro error (un 503, un error de conexión, un 401) no lo detiene: queda en el log y
  reintenta en la siguiente vuelta, porque el lease puede seguir vivo. Si de verdad se perdió,
  la siguiente renovación, la reserva o el commit responden 409.
- check() lanza E_LOCK_LOST si se perdió: quien sube bloques lo llama antes de cada envío.
- renew=False no renueva. Solo sirve para la prueba de concurrencia (tarea 26), que necesita un
  lock vencido.

Uso: with LockKeeper(api, lock_id) as keeper: ... keeper.check() ...
"""

import logging
import threading

from dfs_common.errors import DFSError, ErrorCode

logger = logging.getLogger("dfsha")


class LockKeeper:
    def __init__(self, api, lock_id: str, renew: bool = True) -> None:
        """api: una DfsApi (o una con la misma interfaz); de su configuración sale DFS_LOCK_RENEW_S."""
        self.api = api
        self.lock_id = lock_id
        self.renew = renew
        self.lost = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def __enter__(self) -> "LockKeeper":
        if self.renew:
            self._thread = threading.Thread(target=self._run, name=f"lock-{self.lock_id}", daemon=True)
            self._thread.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.stop()

    def stop(self) -> None:
        """Deja de renovar y espera a que el hilo termine (a lo más, una renovación en curso)."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join()

    def check(self) -> None:
        if self.lost:
            raise DFSError(ErrorCode.E_LOCK_LOST, "se perdió el lock del archivo; empieza de nuevo")

    def renew_once(self) -> bool:
        """Una renovación. Devuelve False si el lock se perdió (409) y hay que dejar de renovar."""
        try:
            self.api.renew_lock(self.lock_id)
        except DFSError as error:
            if error.code == ErrorCode.E_LOCK_LOST:
                self.lost = True
                return False
            logger.warning("no se pudo renovar el lock %s; se intenta en la siguiente vuelta: %s", self.lock_id, error)
        return True

    def _run(self) -> None:
        # wait devuelve True apenas se llama a stop(): así el hilo termina sin esperar la vuelta completa.
        while not self._stop.wait(self.api.settings.lock_renew_s):
            if not self.renew_once():
                return
