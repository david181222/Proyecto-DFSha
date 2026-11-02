# DFSha

Sistema de archivos distribuido con alta disponibilidad, rendimiento y seguridad. Proyecto 1 de Sistemas Distribuidos (UPB, 2026-2), fase II, opción 1: cliente/servidor con servicio distribuido entre servidores.

Equipo: Edwin Vélez, José Díaz y Santiago Martínez.

## Estado

Tareas 01, 02, 02b y 02c: contratos de API. Con la tarea 02c, que cerró los hallazgos de la auditoría, `docs/contratos.md` no tiene pendientes. Las cuatro APIs existen como *stubs* que responden 501, salvo `GET /api/v1/health`; la validación de parámetros sí funciona (por ejemplo, una ruta de archivo inválida responde 400). Todavía no hay lógica (etcd, cifrado, réplicas ni almacenamiento).

| Documento | Contenido |
|---|---|
| `docs/decisiones.md` | decisiones de arquitectura DA-01 a DA-16 |
| `docs/contratos.md` | rutas, mensajes, errores, formatos y claves de etcd |
| `docs/api/*.json` | OpenAPI de cada API, generado desde el código |
| `.env.example` | variables de entorno con sus valores normales y de prueba |

## Instalación

El sistema se despliega con Python 3.12, dentro de Docker. Para desarrollar y correr las pruebas sirve Python 3.12 o más reciente: las pruebas también pasan en 3.13. Desde la raíz del repositorio:

**Windows (PowerShell)**

```powershell
py -3.12 -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
```

Si PowerShell no deja ejecutar `Activate.ps1`, corre antes `Set-ExecutionPolicy -Scope Process Bypass`.

**Linux**

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
```

## Pruebas

Con el entorno activado, igual en Windows y en Linux:

```bash
pytest
```

`tests/test_contratos.py` comprueba que las rutas sean las del contrato, que los ejemplos de `docs/contratos.md` sean los de los modelos y validen, que las tablas de rutas, de errores y de variables coincidan con el código y con `.env.example`, que `docs/api/` esté al día y que se cumplan las decisiones con que se cerraron los pendientes de la tarea 01.

## Regenerar el OpenAPI

Después de cambiar un modelo, una ruta o un error:

```bash
python scripts/export_openapi.py
```

Escribe `docs/api/metaserver-public.json`, `metaserver-internal.json`, `datanode-public.json` y `datanode-internal.json`. Si nadie cambió el código, los archivos quedan iguales. `fastapi` y `pydantic` tienen versión fija en `pyproject.toml` porque el OpenAPI generado cambia entre versiones.

## Correr un stub en local

Sin TLS, solo para mirar la documentación interactiva en `http://127.0.0.1:8443/docs`:

```bash
uvicorn metaserver.public_api:app --port 8443
```

Las otras apps son `metaserver.internal_api:app`, `datanode.public_api:app` y `datanode.internal_api:app`.

## Seguridad del repositorio

El repositorio es público: nunca se suben claves, certificados, archivos `.env`, `labsuser.pem` ni credenciales de AWS. `.gitignore` los excluye. Los secretos se leen de archivos cuya ruta viene en una variable de entorno (`DFS_JWT_SECRET_FILE`, `DFS_PASS_KEY_FILE`, `DFS_NODE_KEY_FILE`, `DFS_ADMIN_PASSWORD_FILE` y los certificados `DFS_TLS_*`).
