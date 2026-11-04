"""Claves de etcd del MetaServer (docs/contratos.md, sección 8), escritas en un solo lugar."""


def user_key(user: str) -> str:
    """Usuario: {password_hash, is_admin}."""
    return f"/users/{user}"


def ns_key(user: str, path: str) -> str:
    """Entrada de un archivo o directorio del espacio del usuario. La raíz / no tiene clave."""
    if path == "/":
        raise ValueError("la raíz / no se guarda en etcd")
    return f"/ns/{user}{path}"


def children_prefix(user: str, path: str) -> str:
    """Prefijo de todo lo que hay dentro del directorio path, también de la raíz: /ns/ana/docs/ o /ns/ana/."""
    return f"/ns/{user}{path.rstrip('/')}/"


def manifest_key(file_id: str, version: int) -> str:
    """Manifiesto de la versión version de un archivo."""
    return f"/files/{file_id}/v/{version}"


def lock_name(user: str, path: str) -> str:
    """Nombre del lock de etcd de un archivo; cada intento queda en {lock_name}/{lock_id} (DA-08)."""
    return f"/locks/{user}{path}"
