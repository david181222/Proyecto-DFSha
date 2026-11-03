"""Configuración de todos los componentes, leída de variables de entorno (DA-16).

Cada variable lleva el prefijo DFS_: el campo block_mib se lee de DFS_BLOCK_MIB.
Los valores por defecto son los normales de la Tabla 19 de docs/decisiones.md;
los de prueba están comentados en .env.example. Los secretos nunca van aquí: solo
la ruta del archivo que los contiene (DA-10).
"""

from ipaddress import IPv4Address
from pathlib import Path
from typing import Annotated

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

# Una IP, o un nombre DNS si el laboratorio permite Route 53 (DA-11); nunca con puerto.
SeedHost = Annotated[str, Field(pattern=r"^[A-Za-z0-9.-]+$")]
EtcdEndpoint = Annotated[str, Field(pattern=r"^https://[^/\s:]+:[0-9]+$")]


class Settings(BaseSettings):
    # env_ignore_empty: una variable vacía (DFS_ADMIN_PASSWORD_FILE=) toma el valor por defecto.
    model_config = SettingsConfigDict(env_prefix="DFS_", env_file=".env", extra="ignore", env_ignore_empty=True)

    # --- Tiempos y umbrales (Tabla 19, DA-16) ---
    block_mib: int = Field(8, ge=1, description="Tamaño de bloque en MiB (DA-03)")
    w: int = Field(2, ge=2, le=3, description="Copias para confirmar un bloque (DA-04)")
    primary_wait_s: int = Field(10, ge=1, description="Espera del primario por los secundarios (DA-04)")
    lock_lease_s: int = Field(30, ge=1, description="Lease de cada intento de lock (DA-08)")
    heartbeat_s: int = Field(5, ge=1, description="Intervalo de latido de los DataNodes (DA-12)")
    node_lease_s: int = Field(15, ge=1, description="Vencimiento de un nodo sin latidos (DA-12)")
    rereplicate_after_s: int = Field(60, ge=1, description="Espera antes de re-replicar (DA-12)")
    leader_grace_s: int = Field(300, ge=0, description="Espera del líder nuevo antes de re-replicar (DA-12)")
    retention_s: int = Field(600, ge=1, description="Retención de la versión anterior (DA-14)")
    gc_interval_s: int = Field(600, ge=1, description="Cada cuánto corre la limpieza (DA-02)")
    gc_min_age_s: int = Field(3600, ge=1, description="Tiempo sin cambios para ser candidato (DA-02)")

    # --- Tiempos agregados en la tarea 02 (mismo valor normal y de prueba) ---
    lock_renew_s: int = Field(10, ge=1, description="Cada cuánto renueva el cliente su lock (DA-08)")
    gc_clock_margin_s: int = Field(1, ge=0, description="Margen por diferencia de relojes en la limpieza (DA-02)")

    # --- Tiempo agregado en la tarea 02d (mismo valor normal y de prueba) ---
    leader_scan_s: int = Field(
        10, ge=1, description="Cada cuánto revisa el líder nodos caídos, copias faltantes y órdenes atascadas (DA-12)"
    )

    # --- Vencimiento de tokens (DA-10) ---
    jwt_ttl_s: int = Field(3600, ge=1, description="Duración del JWT del login")
    pass_ttl_s: int = Field(600, ge=1, description="Duración de un pase de bloque")

    # --- Puertos (DA-13) ---
    meta_public_port: int = Field(8443, description="MetaServer, API pública (HTTPS)")
    meta_internal_port: int = Field(8444, description="MetaServer, API interna (mTLS)")
    dn_public_port: int = Field(9443, description="DataNode, API pública (HTTPS)")
    dn_internal_port: int = Field(9444, description="DataNode, API interna (mTLS)")

    # --- Identidad y descubrimiento (DA-11) ---
    # Las listas van separadas por coma en el .env: 172.31.0.10,172.31.16.10,172.31.32.10
    meta_seeds: Annotated[list[SeedHost], NoDecode] = Field(
        default_factory=list,
        description="IP privadas de los 3 MetaServers, sin puerto, en el orden en que se prueban; "
        "el cliente usa meta_public_port y el DataNode meta_internal_port. Si se usa Route 53, "
        "un nombre DNS en su lugar (DA-11)",
    )
    etcd_endpoints: Annotated[list[EtcdEndpoint], NoDecode] = Field(
        default_factory=list,
        description="https://ip:2379 de los 3 nodos de etcd (DA-01); el MetaServer les habla por mTLS "
        "con tls_cert_file, tls_key_file y tls_ca_file",
    )
    zone: str = Field("", description="Zona de este nodo o cliente, por ejemplo us-east-1a")
    node_id: str = Field("", description="Identificador de este nodo: meta1..meta3 o dn1..dn4")
    # Sin valor por defecto: la escribe el despliegue. Es None y no obligatoria porque el
    # cliente usa esta misma clase y no la necesita.
    node_ip: IPv4Address | None = Field(
        None,
        description="IP privada de este nodo. La usan el DataNode al registrarse (ip de POST /internal/register) "
        "y el MetaServer al registrarse en /nodes/meta/ (DA-11)",
    )
    data_dir: Path = Field(Path("/data"), description="Carpeta de bloques del DataNode (DA-02)")

    # --- TLS: CA propia y certificado de este nodo (DA-06, DA-11) ---
    tls_ca_file: Path = Field(Path("/run/secrets/ca.crt"), description="Certificado de la CA propia")
    tls_cert_file: Path = Field(Path("/run/secrets/tls.crt"), description="Certificado de este nodo")
    tls_key_file: Path = Field(Path("/run/secrets/tls.key"), description="Llave privada del certificado")

    # --- Archivos de secretos (DA-10) ---
    jwt_secret_file: Path = Field(Path("/run/secrets/jwt_secret"), description="Clave HS256 del JWT")
    pass_key_file: Path = Field(Path("/run/secrets/pass_key"), description="Clave HMAC de los pases")
    node_key_file: Path = Field(Path("/run/secrets/node_key"), description="Clave AES-256 del DataNode (DA-09)")
    admin_password_file: Path | None = Field(
        None,
        description="Contraseña inicial del usuario admin; si falta, el MetaServer no crea el administrador",
    )

    @field_validator("meta_seeds", "etcd_endpoints", mode="before")
    @classmethod
    def split_by_comma(cls, value: object) -> object:
        """Convierte "a:1,b:2" en ["a:1", "b:2"]."""
        if isinstance(value, str):
            return [seed.strip() for seed in value.split(",") if seed.strip()]
        return value

    def require(self, *fields: str) -> None:
        """Detiene el arranque si falta alguna de estas variables.

        Las de identidad y descubrimiento (node_id, node_ip, zone, meta_seeds, etcd_endpoints)
        no tienen un valor por defecto útil: las escribe el despliegue. Sin esta comprobación,
        un MetaServer sin DFS_NODE_ID arrancaría y se registraría con un nombre vacío.
        """
        missing = [f"DFS_{field.upper()}" for field in fields if getattr(self, field) in ("", None, [])]
        if missing:
            raise SystemExit(f"faltan variables de entorno: {', '.join(missing)}")
