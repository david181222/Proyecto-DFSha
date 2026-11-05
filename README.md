# DFSha

Sistema de archivos distribuido con alta disponibilidad, rendimiento y seguridad. Proyecto 1 de Sistemas Distribuidos (UPB, 2026-2), fase II, opción 1: cliente/servidor con servicio distribuido entre servidores.

Equipo: Edwin Vélez, José Díaz y Santiago Martínez.

## Estado

| Tareas | Qué hay |
|---|---|
| 01 a 02d | Contratos de API, sin puntos abiertos (`docs/contratos.md`) |
| 03 a 05 | Seguridad común (JWT y pases de bloque), CA, certificados y secretos (`scripts/gen_secrets.py`), y el sistema completo en un PC con TLS 1.3 y mTLS (`compose.local.yml`, ver "Correr en local") |
| 06 | Cliente de etcd del MetaServer por la pasarela HTTP/JSON, con mTLS y paso a otro nodo si uno no responde (`src/metaserver/etcd.py`) |
| 07 | `POST /auth/login`, `POST /admin/users` y el primer administrador, que el MetaServer crea al arrancar (`src/metaserver/auth.py`) |
| 08 | `ls`, `stat`, `mkdir`, `rmdir` y `rm` sobre etcd (`src/metaserver/fs.py`) |
| 09 | Almacén de bloques del DataNode: cifrado con AES-256-GCM, escritura atómica y borrado por fecha (`src/datanode/storage.py`), y `PUT /internal/blocks/{user}/{hash}` |
| 10 | Verificación de pases en el DataNode (`src/datanode/auth.py`) y `GET /blocks/{hash}`. `PUT /blocks/{hash}` ya verifica el pase y responde 501 hasta la tarea 14 |
| 11 | Locks: `POST /locks` con espera en la fila de etcd y renovación del lease mientras espera, `PUT` y `DELETE /locks/{lock_id}` (`src/metaserver/locks.py`) |
| 12 | Subidas: reserva con ubicación de réplicas en 2 zonas, `realloc`, commit con su transacción, `GET /files/blocks` y `POST /internal/blocks/confirm` (`uploads.py`, `placement.py`, `blocks.py` y `files.py`) |
| 13 | Registro y latidos de los DataNodes (`datanodes.py`), autorregistro de los MetaServers y `GET /cluster/metaservers` (`metaservers.py`), y `GET /admin/status` (`status.py`) |
| 14 | `PUT /blocks/{hash}` completo: el primario reenvía a los secundarios y confirma al MetaServer (`src/datanode/primary.py`), con `MetaClient` (`src/datanode/meta_client.py`) |
| 15 | El DataNode se registra, late y cumple las órdenes `copy` y `delete` del líder, y crea su `disk_id` (`src/datanode/agent.py`) |

Ya funcionan todas las rutas de las cuatro APIs. Un DataNode guarda, entrega y reenvía bloques cifrados, se registra, late y cumple las órdenes del líder. Las decisiones de la tarea 15 están en el docstring de `src/datanode/agent.py`. Las decisiones de comportamiento de las tareas 06 a 08 están en la sección 11 de `docs/contratos.md`, y las de las tareas 09 y 10, en la sección 12. Las de la tarea 11 no cambian el contrato y están en el docstring de `src/metaserver/locks.py`; los cambios y decisiones de la tarea 12, en la sección 13; y las de la tarea 13, en los docstrings de `datanodes.py`, `metaservers.py` y `status.py`.

Código del MetaServer:

| Módulo | Qué hace |
|---|---|
| `public_api.py` y `internal_api.py` | Rutas: el contrato HTTP. Las implementadas solo llaman a los módulos de abajo |
| `etcd.py` | Cliente de etcd: claves y valores en base64, transacciones, leases, lock y elección |
| `deps.py` | Lo que se crea al arrancar (configuración, cliente de etcd, clave del JWT) y se entrega a las rutas |
| `auth.py` | Contraseñas Argon2id, `require_user` y `require_admin`, usuarios y primer administrador |
| `fs.py` | Directorios y archivos del espacio de cada usuario |
| `locks.py` | Locks de escritura: pedir (con espera y renovación), renovar, liberar y `resolve_lock` |
| `uploads.py` | Subidas: reserva, `realloc` y commit; `check_commit` valida la forma del archivo |
| `placement.py` | `choose_nodes`: a qué DataNodes va cada bloque (DA-05) |
| `blocks.py` | Claves de bloque: confirmación, `update_stored_on` y copias vivas en 2 zonas |
| `files.py` | `GET /files/blocks`: manifiesto, réplicas vivas y pases de lectura |
| `nodes.py` | DataNodes vivos de `/nodes/dn/` y comprobación de la IP de origen de la API interna |
| `datanodes.py` | Registro y latidos de los DataNodes, con sus órdenes |
| `metaservers.py` | Autorregistro de este MetaServer y lista de MetaServers vivos |
| `status.py` | `GET /admin/status`: `build_admin_status` arma la respuesta |
| `keys.py` | Formato de las claves de etcd (sección 8 del contrato) |

Código del DataNode:

| Módulo | Qué hace |
|---|---|
| `public_api.py` y `internal_api.py` | Rutas: el contrato HTTP. Las implementadas solo llaman a los módulos de abajo |
| `deps.py` | Lo que se crea al arrancar (configuración, almacén y clave de los pases); las dos apps comparten el almacén |
| `storage.py` | `BlockStore`: guarda, lee y borra bloques cifrados en `DFS_DATA_DIR` |
| `auth.py` | `require_pass`: verifica el pase de bloque de cada petición |
| `primary.py` | `PUT /blocks/{hash}`: guardar, reenviar a los secundarios y confirmar |
| `meta_client.py` | `MetaClient`: descubre los MetaServers y les habla por mTLS |
| `agent.py` | `NodeAgent`: registro, latidos y órdenes, en tareas de fondo del lifespan; `load_disk_id` lee o crea `DFS_DATA_DIR/.disk_id` |

Código del cliente (`src/dfs_client/`):

| Módulo | Qué hace |
|---|---|
| `api.py` | `DfsApi`: una llamada por ruta del contrato; descubre los MetaServers y pasa al siguiente si uno no responde |
| `session.py` | Sesión en `~/.dfs/session.json` (usuario, JWT y directorio actual); `Session.resolve` arma la ruta absoluta |
| `cli.py` | Comandos `dfs` con typer; el formato de la salida está en las funciones `format_*` |

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

Las rutas del MetaServer se prueban con un etcd en memoria (`tests/fakes.py`). Las pruebas contra un etcd real (`tests/test_etcd_integration.py` y `tests/test_metaserver_integration.py`) se saltan si no está la variable `DFS_TEST_ETCD`. Para correrlas, desde PowerShell:

```powershell
docker run -d --rm --name etcd-test -p 2379:2379 -e ETCD_LISTEN_CLIENT_URLS=http://0.0.0.0:2379 -e ETCD_ADVERTISE_CLIENT_URLS=http://127.0.0.1:2379 quay.io/coreos/etcd:v3.5.35
$env:DFS_TEST_ETCD = "http://127.0.0.1:2379"; pytest; Remove-Item Env:DFS_TEST_ETCD
docker stop etcd-test
```

En Linux, la segunda línea es `DFS_TEST_ETCD=http://127.0.0.1:2379 pytest`.

## Regenerar el OpenAPI

Después de cambiar un modelo, una ruta o un error:

```bash
python scripts/export_openapi.py
```

Escribe `docs/api/metaserver-public.json`, `metaserver-internal.json`, `datanode-public.json` y `datanode-internal.json`. Si nadie cambió el código, los archivos quedan iguales. `fastapi` y `pydantic` tienen versión fija en `pyproject.toml` porque el OpenAPI generado cambia entre versiones.

## Mirar la documentación interactiva

Sin TLS, en `http://127.0.0.1:8443/docs`:

```bash
uvicorn metaserver.public_api:app --port 8443 --lifespan off
```

`--lifespan off` hace falta porque, al arrancar, la API pública del MetaServer lee sus secretos y se conecta a etcd, y las del DataNode leen sus claves y abren `DFS_DATA_DIR`; sin esa opción, fuera de Docker no arrancan. Así sirve `/docs` y `/health`; las rutas implementadas responden 500, porque no hay etcd, claves ni almacén. Las otras apps son `metaserver.internal_api:app`, `datanode.public_api:app` y `datanode.internal_api:app`.

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

Deben quedar 11 contenedores corriendo y los 3 etcd en `healthy`. Al arrancar, un MetaServer crea el usuario `admin` con la contraseña de `secrets/local/admin_password`; si etcd todavía no responde, el contenedor termina y Docker lo reinicia hasta que etcd esté listo. Un DataNode no arranca si `DFS_DATA_DIR` tiene bloques pero no `.disk_id` (contrato, sección 3): si los volúmenes vienen de antes de la tarea 15, borra los datos con `down -v` (paso 5). Los tiempos de DA-16 toman los valores normales; para usar los de prueba, crea un `.env` en la raíz (git lo ignora) con la columna "Pruebas" de `.env.example`, por ejemplo `DFS_RETENTION_S=60`.

**3. Comprobar TLS y mTLS:**

```bash
# API pública por HTTPS, verificada con la CA propia: {"status":"ok"}
docker compose -f compose.local.yml exec client curl --cacert /run/secrets/ca.crt https://10.88.0.21:8443/api/v1/health
docker compose -f compose.local.yml exec client curl --cacert /run/secrets/ca.crt https://10.88.0.31:9443/api/v1/health

# Solo TLS 1.3: con TLS 1.2 falla
docker compose -f compose.local.yml exec client curl --tls-max 1.2 --cacert /run/secrets/ca.crt https://10.88.0.21:8443/api/v1/health

# API interna (mTLS): sin certificado de cliente, el servidor corta el handshake
docker compose -f compose.local.yml exec client curl --cacert /run/secrets/ca.crt -X POST https://10.88.0.21:8444/api/v1/internal/register

# Con el certificado de un DataNode, el handshake pasa y responde JSON (400: falta el cuerpo)
docker compose -f compose.local.yml exec dn1 curl --cacert /run/secrets/ca.crt --cert /run/secrets/tls.crt --key /run/secrets/tls.key -X POST https://10.88.0.21:8444/api/v1/internal/register

# etcd: 3 miembros y los 3 sanos (etcdctl ya trae los certificados en sus variables ETCDCTL_*)
docker compose -f compose.local.yml exec etcd1 etcdctl member list -w table
docker compose -f compose.local.yml exec etcd1 etcdctl endpoint health --cluster
```

**4. Entrar como administrador.** Responde `{"token": "...", "expires_in": 3600}`. La contraseña se lee del archivo y no se muestra.

PowerShell:

```powershell
$login = @{ user = "admin"; password = (Get-Content secrets/local/admin_password -Raw).Trim() } | ConvertTo-Json -Compress
$login | docker compose -f compose.local.yml exec -T client curl -s --cacert /run/secrets/ca.crt -H "Content-Type: application/json" --data-binary "@-" https://10.88.0.21:8443/api/v1/auth/login
```

WSL o Linux:

```bash
printf '{"user":"admin","password":"%s"}' "$(cat secrets/local/admin_password)" | docker compose -f compose.local.yml exec -T client curl -s --cacert /run/secrets/ca.crt -H "Content-Type: application/json" --data-binary @- https://10.88.0.21:8443/api/v1/auth/login
```

Con ese token se crean usuarios (`POST /api/v1/admin/users`), y con el token de cada usuario se usan las rutas `/fs` (`docs/contratos.md`, sección 2).

**5. Usar el cliente `dfs`.** Desde el contenedor `client`; la contraseña de `admin` está en `secrets/local/admin_password`. La sesión queda en `~/.dfs/session.json` del contenedor.

```bash
docker compose -f compose.local.yml exec client dfs login --user admin
docker compose -f compose.local.yml exec client dfs useradd ana
docker compose -f compose.local.yml exec client dfs status
docker compose -f compose.local.yml exec client dfs login --user ana
docker compose -f compose.local.yml exec client dfs mkdir /docs
docker compose -f compose.local.yml exec client dfs cd /docs
docker compose -f compose.local.yml exec client dfs ls
docker compose -f compose.local.yml exec client dfs rmdir /docs
```

`dfs --help` lista los comandos, y `dfs COMANDO --help` explica cada uno.

**6. Apagar:**

```bash
docker compose -f compose.local.yml down      # conserva los datos de etcd y de los DataNodes
docker compose -f compose.local.yml down -v   # además borra los volúmenes con los datos
```

## Seguridad del repositorio

El repositorio es público: nunca se suben claves, certificados, archivos `.env`, `labsuser.pem` ni credenciales de AWS. `.gitignore` los excluye. Los secretos se leen de archivos cuya ruta viene en una variable de entorno (`DFS_JWT_SECRET_FILE`, `DFS_PASS_KEY_FILE`, `DFS_NODE_KEY_FILE`, `DFS_ADMIN_PASSWORD_FILE` y los certificados `DFS_TLS_*`).
