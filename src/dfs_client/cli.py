"""CLI dfs (tarea 16): login, logout, pwd, cd, ls, stat, mkdir, rmdir, rm, useradd y status.

Cada comando lee la sesión (dfs_client.session), resuelve la ruta contra el directorio actual
y llama a DfsApi. La API se obtiene con make_api, que las pruebas reemplazan por una falsa; el
formato de la salida está en funciones puras (format_*), que se prueban sin red.

Errores: se muestran como "error E_CODIGO: mensaje" y el comando sale con código 1. Dos
mensajes cambian para que se entienda qué hacer:
- un 401, o no tener sesión: "Tu sesión no existe o venció: corre dfs login";
- un 423 en rm: "El archivo está bloqueado por otro cliente; intenta más tarde".
"""

from contextlib import contextmanager
from datetime import datetime

import typer

from dfs_client import session
from dfs_client.api import DfsApi
from dfs_client.session import Session
from dfs_common.config import Settings
from dfs_common.errors import DFSError, ErrorCode
from dfs_common.models import AdminStatusResponse, DirEntry, FileBlocksResponse, StatResponse

SESSION_MESSAGE = "Tu sesión no existe o venció: corre dfs login"
LOCKED_MESSAGE = "El archivo está bloqueado por otro cliente; intenta más tarde"
SHORT_HASH = 12  # caracteres del hash que muestra stat --blocks (DA-16)
GIB = 1024**3

app = typer.Typer(add_completion=False, no_args_is_help=True, help="Cliente de DFSha.")


def make_api(settings: Settings, token: str | None) -> DfsApi:
    """La API que usan los comandos; las pruebas la reemplazan con monkeypatch."""
    return DfsApi(settings, token)


# --- Sesión y errores ---


@contextmanager
def handle_errors(*, needs_session: bool = True, locked_message: str | None = None):
    """Convierte un DFSError en el mensaje de error y el código de salida 1."""
    try:
        yield
    except DFSError as error:
        message = error.message
        if error.code == ErrorCode.E_AUTH and needs_session:
            message = SESSION_MESSAGE
        elif error.code == ErrorCode.E_LOCKED and locked_message:
            message = locked_message
        typer.echo(f"error {error.code.value}: {message}", err=True)
        raise typer.Exit(1) from None
    except FileNotFoundError as error:
        # Falta un archivo de configuración, como la CA de DFS_TLS_CA_FILE.
        typer.echo(f"error: no existe el archivo {error.filename}", err=True)
        raise typer.Exit(1) from None


def current_session() -> Session:
    found = session.load()
    if found is None:
        raise DFSError(ErrorCode.E_AUTH, SESSION_MESSAGE)
    return found


def open_api(current: Session | None = None) -> DfsApi:
    return make_api(Settings(), current.token if current else None)


# --- Formato de la salida ---


def format_time(ms: int) -> str:
    """Fecha local de un instante en ms; "-" para 0 (la raíz no tiene fecha)."""
    if ms == 0:
        return "-"
    return datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M")


def format_ls(entries: list[DirEntry]) -> str:
    """Una línea por entrada: tipo (d o -), tamaño, versión, fecha local y nombre."""
    lines = []
    for entry in entries:
        kind = "d" if entry.type == "dir" else "-"
        lines.append(f"{kind} {entry.size:>12} {entry.version:>4}  {format_time(entry.modified_at)}  {entry.name}")
    return "\n".join(lines)


def format_stat(info: StatResponse) -> str:
    kind = "directorio" if info.type == "dir" else "archivo"
    return "\n".join(
        [
            f"ruta:        {info.path}",
            f"tipo:        {kind}",
            f"tamaño:      {info.size} bytes",
            f"versión:     {info.version}",
            f"modificado:  {format_time(info.modified_at)}",
        ]
    )


def format_blocks(manifest: FileBlocksResponse) -> str:
    """Por bloque: índice, hash corto, tamaño y nodos con copia; los caídos van marcados (DA-16)."""
    file_hash = manifest.file_sha256[:SHORT_HASH] if manifest.file_sha256 else "sin hash (a o rw)"
    lines = [
        f"{manifest.path}  versión {manifest.version}  {manifest.size} bytes  "
        f"bloques de {manifest.block_size} bytes  sha256 {file_hash}",
        f"{'índice':>6}  {'hash':<{SHORT_HASH}}  {'tamaño':>10}  nodos",
    ]
    for block in manifest.blocks:
        alive = {replica.node_id for replica in block.replicas}
        nodes = ", ".join(node if node in alive else f"{node} (caído)" for node in block.stored_on)
        lines.append(f"{block.index:>6}  {block.hash[:SHORT_HASH]}  {block.size:>10}  {nodes or '-'}")
    return "\n".join(lines)


def format_status(status: AdminStatusResponse) -> str:
    lines = [f"Líder: {status.leader or 'sin líder'}", "", "MetaServers:"]
    for meta in status.metaservers:
        lines.append(f"  {meta.meta_id:<8} {meta.ip:<16} {meta.public_port}/{meta.internal_port}")
    lines += ["", "DataNodes:"]
    for node in status.datanodes:
        if node.alive:
            free = f"{node.free_bytes / GIB:.1f} GiB libres" if node.free_bytes is not None else "-"
            lines.append(f"  {node.node_id:<8} vivo   {node.zone or '-':<12} {node.address or '-':<22} {free}")
        else:
            lines.append(f"  {node.node_id:<8} caído")
    lines += ["", f"Bloques con menos de 3 copias vivas: {len(status.under_replicated)}"]
    for block in status.under_replicated:
        lines.append(f"  {block.user} {block.hash[:SHORT_HASH]}  vivas en: {', '.join(block.alive_on) or '-'}")
    lines += ["", f"Órdenes pendientes: {len(status.pending_orders)}"]
    for pending in status.pending_orders:
        order = pending.order
        if order.type == "copy":
            detail = f"copy   {order.user} {order.hash[:SHORT_HASH]} -> {order.target.node_id}"
        else:
            detail = f"delete {order.user} {order.hash[:SHORT_HASH]} (escrito antes de {format_time(order.written_before_ms)})"
        lines.append(f"  {pending.node_id:<8} {detail}")
    return "\n".join(lines)


# --- Comandos ---


@app.command()
def login(user: str = typer.Option(None, "--user", "-u", help="Usuario; si falta, se pregunta.")):
    """Inicia sesión y deja el directorio actual en /."""
    if user is None:
        user = typer.prompt("Usuario")
    password = typer.prompt("Contraseña", hide_input=True)
    with handle_errors(needs_session=False):
        answer = open_api().login(user, password)
    session.save(Session(user=user, token=answer.token, cwd="/"))
    typer.echo(f"Sesión iniciada como {user}")


@app.command()
def logout():
    """Borra la sesión guardada."""
    session.clear()
    typer.echo("Sesión cerrada")


@app.command()
def pwd():
    """Muestra el directorio actual."""
    with handle_errors():
        typer.echo(current_session().cwd)


@app.command()
def cd(path: str = typer.Argument(None, help="Directorio; sin ruta, vuelve a /.")):
    """Cambia el directorio actual; comprueba con stat que sea un directorio."""
    with handle_errors():
        current = current_session()
        target = current.resolve(path) if path else "/"
        if target != "/":
            info = open_api(current).stat(target)
            if info.type != "dir":
                raise DFSError(ErrorCode.E_BAD_REQUEST, f"{target} no es un directorio")
        current.cwd = target
        session.save(current)


@app.command()
def ls(path: str = typer.Argument(".", help="Directorio o archivo; por defecto, el actual.")):
    """Lista un directorio: tipo, tamaño, versión, fecha y nombre."""
    with handle_errors():
        current = current_session()
        listing = open_api(current).ls(current.resolve(path))
    if listing.entries:
        typer.echo(format_ls(listing.entries))


@app.command()
def stat(
    path: str,
    blocks: bool = typer.Option(False, "--blocks", help="Muestra cada bloque y los DataNodes que lo tienen."),
):
    """Tipo, tamaño, versión y fecha; con --blocks, dónde está cada bloque (DA-16)."""
    with handle_errors():
        current = current_session()
        api = open_api(current)
        target = current.resolve(path)
        text = format_blocks(api.files_blocks(target)) if blocks else format_stat(api.stat(target))
    typer.echo(text)


@app.command()
def mkdir(path: str):
    """Crea un directorio (el padre debe existir)."""
    with handle_errors():
        current = current_session()
        open_api(current).mkdir(current.resolve(path))


@app.command()
def rmdir(path: str):
    """Borra un directorio vacío."""
    with handle_errors():
        current = current_session()
        open_api(current).rmdir(current.resolve(path))


@app.command()
def rm(path: str):
    """Borra un archivo."""
    with handle_errors(locked_message=LOCKED_MESSAGE):
        current = current_session()
        open_api(current).rm(current.resolve(path))


@app.command()
def useradd(user: str):
    """Crea un usuario (solo admin); pide su contraseña dos veces."""
    with handle_errors():
        current = current_session()
        password = typer.prompt(f"Contraseña de {user}", hide_input=True, confirmation_prompt=True)
        open_api(current).add_user(user, password)
    typer.echo(f"Usuario {user} creado")


@app.command()
def status():
    """Estado del sistema (solo admin): líder, nodos, bloques con menos de 3 copias y órdenes (DA-16)."""
    with handle_errors():
        answer = open_api(current_session()).admin_status()
    typer.echo(format_status(answer))
