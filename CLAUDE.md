# DFSha — guía para Claude Code

Proyecto 1 de Sistemas Distribuidos (UPB, 2026-2): sistema de archivos distribuido con alta disponibilidad, rendimiento y seguridad. Fase II: implementación grupal desplegada en AWS Academy. Opción asignada: 1, cliente/servidor con servicio distribuido entre servidores (S2S).

Equipo: Edwin Vélez, José Díaz y Santiago Martínez. En la defensa el profesor puede preguntarle a cualquiera por cualquier parte, así que el código debe ser simple y fácil de leer.

## Fuentes de verdad, en este orden

1. `docs/decisiones.md`: decisiones DA-01 a DA-16. Manda sobre todo lo demás. Es la copia en Markdown de `docs/DFSha_FaseII_Matriz_y_Decisiones_v5.pdf`; lee siempre el Markdown.
2. `docs/contratos.md` y `docs/api/*.json`: rutas, mensajes, errores y claves de etcd (se crean en la tarea 01).
3. `docs/enunciado.pdf`: requisitos RF1–RF3 y RNF1–RNF7.

Las propuestas individuales de la fase I no están en el repositorio: lo que se tomó de cada una ya está resumido en `docs/decisiones.md` (matriz comparativa y campo "Origen" de cada decisión). No las busques.

**DA-07 ya está cerrada: el lenguaje es Python.** El documento todavía la muestra como abierta; ignora esa parte.

## Stack fijo

- Python 3.12, FastAPI + uvicorn, Pydantic v2 y pydantic-settings.
- httpx para llamadas HTTP, incluida la pasarela HTTP/JSON de etcd v3 (`/v3/kv`, `/v3/lease`, `/v3/lock`, `/v3/election`). No usar clientes de etcd para Python.
- cryptography (AES-256-GCM), hashlib y hmac (SHA-256, HMAC), argon2-cffi, PyJWT y typer (CLI `dfs`).
- pytest para las pruebas.
- Docker Compose: en AWS, cada VM con `network_mode: host`; en local (`compose.local.yml`), una red *bridge* `dfs` (`10.88.0.0/24`) con una IP fija por contenedor y los mismos puertos que en AWS. Scripts de infraestructura en bash con la AWS CLI; en Windows se corren desde WSL o Git Bash.
- No agregues dependencias fuera de esta lista sin anotarlo y justificarlo.

## Estructura del repositorio

```
CLAUDE.md
README.md
pyproject.toml          dependencias; instalación con pip install -e ".[dev]"
.env.example            variables de entorno con valores de ejemplo (nunca secretos)
.gitignore
.gitattributes          finales LF en lo que corre dentro de Linux (Dockerfile, .yml, .sh)
Dockerfile              una sola imagen para MetaServer, DataNode y cliente
compose.local.yml       sistema completo en un PC, con TLS 1.3 y mTLS (README, "Correr en local")
docs/
  enunciado.pdf
  DFSha_FaseII_Matriz_y_Decisiones_v5.pdf    documento oficial de decisiones
  decisiones.md         copia en Markdown del documento de decisiones
  img/                  figuras de decisiones.md
  contratos.md          contrato legible (tarea 01)
  api/                  OpenAPI exportado en JSON (tarea 01)
src/
  dfs_common/           modelos, errores y configuración compartidos
  metaserver/           MetaServer: API pública (8443) e interna (8444)
  datanode/             DataNode: API pública (9443) e interna (9444)
  dfs_client/           cliente y CLI dfs
infra/                  scripts de AWS (DA-15)
scripts/                export_openapi.py (OpenAPI) y gen_secrets.py (CA, certificados y secretos)
secrets/                secretos generados en local; git lo ignora
tests/
```

## Reglas

- KISS: haz lo que piden el enunciado y las decisiones, nada más. Si algo no está decidido, no lo inventes: anótalo en la sección "Pendientes" del documento que estés tocando y avísame al final.
- Código e identificadores en inglés (snake_case en Python y en JSON). Documentación y comentarios en español.
- El repositorio es público: nunca crees ni subas claves, certificados, archivos `.env`, `labsuser.pem` ni credenciales de AWS. Solo `.env.example`. Los secretos se leen desde archivos cuya ruta viene en una variable de entorno.
- Los tiempos y umbrales del comportamiento del sistema (latidos, leases, re-réplica, limpieza, retención) salen de variables de entorno (DA-16, `src/dfs_common/config.py`); nada de esos tiempos fijos en el código. Los límites de validación de la API, como `wait_s` (de 0 a 60 s, 30 por defecto), son parte del contrato y van fijos en los modelos.
- Toda respuesta de error usa `{error, message, retryable}` con los códigos de `docs/contratos.md`.
- Si cambias un contrato (modelo, ruta o error), en el mismo cambio actualiza los modelos, `docs/contratos.md` y `docs/api/*.json` (con `python scripts/export_openapi.py`).
- Lo que esté en Python debe correr igual en Windows y en Linux: usa `pathlib` y no dependas de comandos de un sistema operativo.
- Antes de terminar una tarea: corre `pytest` y muéstrame el resultado, los archivos creados o cambiados y los pendientes.
- No hagas commit ni push; el equipo revisa primero.
