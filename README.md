# DFSha

Sistema de archivos distribuido con alta disponibilidad, rendimiento y seguridad. Proyecto 1 de Sistemas Distribuidos (UPB, 2026-2), fase II, opción 1: cliente/servidor con servicio distribuido entre servidores.

Equipo: Edwin Vélez, José Díaz y Santiago Martínez.

## Estado

Tareas 01 a 02d: contratos de API, sin pendientes (`docs/contratos.md`). Tareas 03 a 05: seguridad común (JWT y pases de bloque), generación de la CA, los certificados y los secretos (`scripts/gen_secrets.py`), y el sistema completo en un PC con TLS 1.3 y mTLS (`compose.local.yml`, ver "Correr en local"). Las cuatro APIs existen como *stubs* que responden 501, salvo `GET /api/v1/health`; la validación de parámetros sí funciona (por ejemplo, una ruta de archivo inválida responde 400). Todavía no hay lógica (etcd, cifrado, réplicas ni almacenamiento).

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

## Correr en local

`compose.local.yml` levanta en un solo PC el sistema completo, con TLS 1.3 y mTLS reales y los mismos puertos que en AWS: 3 etcd, 3 MetaServers, 4 DataNodes y un contenedor cliente. Cada contenedor tiene una IP fija en la red `dfs` (`10.88.0.0/24`) y la zona de AWS que le corresponde en `DFS_ZONE`. Hace falta Docker con Compose v2; con Docker Desktop en Windows funciona igual desde PowerShell o desde WSL.

| Contenedor | IP | Zona |
|---|---|---|
| etcd1, etcd2, etcd3 | 10.88.0.11, .12, .13 | us-east-1a, 1b, 1c |
| meta1, meta2, meta3 | 10.88.0.21, .22, .23 | us-east-1a, 1b, 1c |
| dn1, dn2, dn3, dn4 | 10.88.0.31, .32, .33, .34 | us-east-1a, 1b, 1c, 1b |
| client | 10.88.0.41 | us-east-1a |

**1. Generar la CA, los certificados y los secretos** (una sola vez; con el entorno de Python activado). Quedan en `secrets/local/`, que git ignora. Si la carpeta ya existe, el script no la toca; `--force` regenera todo, pero deja ilegibles los bloques guardados, así que después corre `down -v`.

```bash
python scripts/gen_secrets.py --out secrets/local --nodes etcd1=10.88.0.11,etcd2=10.88.0.12,etcd3=10.88.0.13,meta1=10.88.0.21,meta2=10.88.0.22,meta3=10.88.0.23,dn1=10.88.0.31,dn2=10.88.0.32,dn3=10.88.0.33,dn4=10.88.0.34
```

**2. Levantar todo:**

```bash
docker compose -f compose.local.yml up -d --build
docker compose -f compose.local.yml ps
```

Deben quedar 11 contenedores corriendo y los 3 etcd en `healthy`. Los tiempos de DA-16 toman los valores normales; para usar los de prueba, crea un `.env` en la raíz (git lo ignora) con la columna "Pruebas" de `.env.example`, por ejemplo `DFS_RETENTION_S=60`.

**3. Comprobar TLS y mTLS:**

```bash
# API pública por HTTPS, verificada con la CA propia: {"status":"ok"}
docker compose -f compose.local.yml exec client curl --cacert /run/secrets/ca.crt https://10.88.0.21:8443/api/v1/health
docker compose -f compose.local.yml exec client curl --cacert /run/secrets/ca.crt https://10.88.0.31:9443/api/v1/health

# Solo TLS 1.3: con TLS 1.2 falla
docker compose -f compose.local.yml exec client curl --tls-max 1.2 --cacert /run/secrets/ca.crt https://10.88.0.21:8443/api/v1/health

# API interna (mTLS): sin certificado de cliente, el servidor corta el handshake
docker compose -f compose.local.yml exec client curl --cacert /run/secrets/ca.crt -X POST https://10.88.0.21:8444/api/v1/internal/register

# Con el certificado de un DataNode, el handshake pasa y responde JSON (400 o 501)
docker compose -f compose.local.yml exec dn1 curl --cacert /run/secrets/ca.crt --cert /run/secrets/tls.crt --key /run/secrets/tls.key -X POST https://10.88.0.21:8444/api/v1/internal/register

# etcd: 3 miembros y los 3 sanos (etcdctl ya trae los certificados en sus variables ETCDCTL_*)
docker compose -f compose.local.yml exec etcd1 etcdctl member list -w table
docker compose -f compose.local.yml exec etcd1 etcdctl endpoint health --cluster
```

**4. Apagar:**

```bash
docker compose -f compose.local.yml down      # conserva los datos de etcd y de los DataNodes
docker compose -f compose.local.yml down -v   # además borra los volúmenes con los datos
```

## Seguridad del repositorio

El repositorio es público: nunca se suben claves, certificados, archivos `.env`, `labsuser.pem` ni credenciales de AWS. `.gitignore` los excluye. Los secretos se leen de archivos cuya ruta viene en una variable de entorno (`DFS_JWT_SECRET_FILE`, `DFS_PASS_KEY_FILE`, `DFS_NODE_KEY_FILE`, `DFS_ADMIN_PASSWORD_FILE` y los certificados `DFS_TLS_*`).
