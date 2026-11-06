"""dfs shell: open, read, write y close a mano, con descriptores (tarea 18, RF3).

Sirve para demostrar RF3 y la concurrencia con dos terminales a la vez (video y tareas 25 y
26). Cada open devuelve un número de descriptor (fd) que usan los demás comandos. Las rutas
relativas se resuelven contra el directorio actual de la sesión (dfs cd).

Usa cmd.Cmd de la biblioteca estándar. Un error se muestra como "error E_CODIGO: mensaje" y el
shell sigue. quit (o Ctrl+D) cierra lo abierto sin publicar y suelta los locks.

Los argumentos se separan por espacios. Una ruta local (writefile y read --to) es el resto de
la línea, así que puede tener espacios y, en Windows, barras invertidas.
"""

import cmd
from pathlib import Path

from dfs_client.api import DEFAULT_WAIT_S
from dfs_client.files import DfsFile
from dfs_client.session import Session
from dfs_common.errors import DFSError, ErrorCode


class DfsShell(cmd.Cmd):
    intro = "Shell de DFSha. help lista los comandos; quit sale sin publicar lo que siga abierto."
    prompt = "dfs> "

    def __init__(self, api, session: Session | None = None, stdout=None) -> None:
        super().__init__(stdout=stdout)
        self.api = api
        self.session = session
        self.files: dict[int, DfsFile] = {}
        self.next_fd = 3  # como en POSIX: 0, 1 y 2 ya tienen dueño

    # --- Comandos ---

    def do_open(self, line: str) -> None:
        """open ruta modo [--wait N] [--no-renew]
        Abre un archivo y muestra su fd. Modos: r, w, a, rw. w, a y rw toman el lock, esperando
        hasta N segundos (30 por defecto; 0 no espera).
        --no-renew no renueva el lock: existe solo para la prueba de concurrencia, para dejar que
        el lock venza y mostrar que un close tardío recibe 409 (G4)."""
        args = line.split()
        renew = "--no-renew" not in args
        args = [arg for arg in args if arg != "--no-renew"]
        wait_s = DEFAULT_WAIT_S
        if "--wait" in args:
            at = args.index("--wait")
            wait_s = int(args[at + 1])
            del args[at : at + 2]
        path, mode = args
        file = DfsFile.open(self.api, self._resolve(path), mode, wait_s=wait_s, renew=renew)
        fd = self.next_fd
        self.next_fd += 1
        self.files[fd] = file
        self._say(f"fd {fd}: {file.path} en modo {mode}, versión {file.version}, {file.size} bytes")

    def do_read(self, line: str) -> None:
        """read fd offset tamaño [--to local]
        Lee hasta tamaño bytes desde offset. Sin --to muestra el texto; con --to lo guarda en un
        archivo local."""
        target = None
        if " --to " in line:
            line, local = line.split(" --to ", 1)
            target = Path(local.strip())
        fd, offset, size = (int(arg) for arg in line.split())
        data = self._file(fd).read(offset, size)
        if target is None:
            self._say(data.decode("utf-8", errors="replace"))
        else:
            target.write_bytes(data)
            self._say(f"{len(data)} bytes en {target}")

    def do_write(self, line: str) -> None:
        """write fd offset texto
        Escribe el texto (el resto de la línea) en offset. En modo a va al final."""
        fd, offset, text = line.split(maxsplit=2)
        written = self._file(int(fd)).write(int(offset), text.encode("utf-8"))
        self._say(f"{written} bytes escritos")

    def do_writefile(self, line: str) -> None:
        """writefile fd offset local
        Escribe el contenido de un archivo local en offset."""
        fd, offset, local = line.split(maxsplit=2)
        written = self._file(int(fd)).write(int(offset), Path(local.strip()).read_bytes())
        self._say(f"{written} bytes escritos")

    def do_close(self, line: str) -> None:
        """close fd
        Cierra el archivo. Un escritor publica la versión nueva y suelta el lock."""
        fd = int(line.strip())
        file = self._file(fd)
        del self.files[fd]
        answer = file.close()
        if answer is None:
            self._say(f"fd {fd} cerrado sin publicar (no hubo cambios)")
        else:
            self._say(f"fd {fd} cerrado: {answer.path} versión {answer.version}")

    def do_files(self, line: str) -> None:
        """files
        Lista los descriptores abiertos."""
        if not self.files:
            self._say("no hay archivos abiertos")
        for fd, file in sorted(self.files.items()):
            changes = f", {len(file.modified)} bloques sin publicar" if file.modified else ""
            self._say(f"fd {fd}: {file.path} modo {file.mode} versión {file.version} {file.size} bytes{changes}")

    def do_quit(self, line: str) -> bool:
        """quit
        Sale. Lo que siga abierto se cierra sin publicar y se sueltan los locks."""
        for file in self.files.values():
            file.abort()
        self.files.clear()
        return True

    def do_EOF(self, line: str) -> bool:
        """Ctrl+D: igual que quit."""
        self._say("")
        return self.do_quit(line)

    # --- Errores y auxiliares ---

    def onecmd(self, line: str) -> bool:
        """Corre un comando; un error se muestra y el shell sigue."""
        try:
            return super().onecmd(line)
        except DFSError as error:
            self._say(f"error {error.code.value}: {error.message}")
        except (ValueError, IndexError):
            command = line.split()[0] if line.split() else ""
            self._say(f"error: argumentos inválidos; mira help {command}".rstrip())
        except OSError as error:
            self._say(f"error: {error}")
        return False

    def emptyline(self) -> bool:
        return False  # una línea vacía no repite el comando anterior

    def _file(self, fd: int) -> DfsFile:
        if fd not in self.files:
            raise DFSError(ErrorCode.E_BAD_REQUEST, f"fd {fd} no está abierto")
        return self.files[fd]

    def _resolve(self, path: str) -> str:
        return self.session.resolve(path) if self.session else path

    def _say(self, text: str) -> None:
        self.stdout.write(text + "\n")
