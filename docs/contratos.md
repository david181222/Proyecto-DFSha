# Contratos de DFSha

Versión legible del contrato entre el cliente, el MetaServer y el DataNode (tareas 01 a 02d; la sección 11 agrega cómo se comportan las rutas implementadas en las tareas 06 a 08; la sección 12, las de las tareas 09 y 10; y la sección 13, los cambios y decisiones de la tarea 12). El código manda y este documento lo resume:

| Qué | Dónde |
|---|---|
| Mensajes (modelos Pydantic con su ejemplo) y reglas de las rutas de archivo | `src/dfs_common/models.py` |
| Códigos de error y formato `{error, message, retryable}` | `src/dfs_common/errors.py` |
| Variables de entorno (DA-16) | `src/dfs_common/config.py` y `.env.example` |
| Rutas (FastAPI; las que no están implementadas son stubs) | `src/metaserver/public_api.py`, `src/metaserver/internal_api.py`, `src/datanode/public_api.py`, `src/datanode/internal_api.py` |
| Claves de etcd que ya usa el código (sección 8) | `src/metaserver/keys.py` |
| OpenAPI exportado | `docs/api/*.json` (`python scripts/export_openapi.py`) |

`tests/test_contratos.py` comprueba que las rutas sean exactamente las de este documento, que cada ejemplo de aquí sea el mismo del modelo y valide contra él, que las tablas de errores y de variables coincidan con el código y con `.env.example`, y que `docs/api/` esté al día. Si cambias un contrato, en el mismo cambio actualiza el modelo o la ruta, este documento y el OpenAPI.

## 1. Convenciones

- Todas las rutas cuelgan de `/api/v1`; las internas, de `/api/v1/internal`.
- JSON en UTF-8 con campos en snake_case. Los tamaños van en bytes.
- **Respuestas:** toda respuesta JSON de la API es un objeto, nunca una lista suelta; las listas van dentro de un campo (`metaservers`, `entries`, `orders`...).
- **Rutas de archivo** (RF1): absolutas dentro del espacio del usuario del token; en etcd quedan bajo `/ns/{user}{path}`. Las valida `check_dfs_path` (`src/dfs_common/models.py`); si no cumplen, 400 `E_BAD_REQUEST`:
  - empiezan en `/` y sus componentes se separan con `/`;
  - cada componente mide de 1 a 255 bytes en UTF-8, no contiene `/` ni el carácter nulo, y no es `.` ni `..`;
  - la ruta completa mide hasta 1024 bytes, no termina en `/` (salvo la raíz) y no contiene `//`.

  Como toda ruta se resuelve dentro del espacio propio, no existe "ruta de otro usuario". La raíz `/` de cada usuario existe siempre y no se guarda en etcd: `stat /` devuelve `modified_at` 0, y `rmdir /` y `rm /` responden 400 `E_BAD_REQUEST`.
- **`cd` y `pwd`** (RF1) son estado del cliente (el directorio actual de la CLI) y no tienen ruta en la API.
- **Hashes:** SHA-256 del contenido sin cifrar, 64 caracteres hexadecimales en minúscula (DA-02).
- **Usuarios:** `^[a-z0-9_-]{1,32}$`, porque el nombre va en rutas HTTP, en claves de etcd y en `/data/{user}/` del DataNode.
- **Direcciones:** `ip:puerto` con la IP privada (DA-11). Al cliente se le entregan los puertos públicos (8443, 9443); entre servidores se usan los internos (8444, 9444).
- **Versiones:** empiezan en 1 y suben de a una con cada commit. `base_version: 0` significa que el archivo todavía no existe.
- **Instantes:** enteros en milisegundos desde epoch (UTC), en la API y en etcd: `modified_at`, `created_at`, `replaced_at`, `touched_at` y `written_before_ms`. El cliente los formatea al mostrarlos. La excepción es `exp`, que va en segundos tanto en el JWT (lo exige su estándar, que también pone `iat` en segundos) como en el pase.
- **Errores:** siempre `{error, message, retryable}` (sección 6).
- **Stubs:** mientras una ruta no esté implementada responde 501 `E_NOT_IMPLEMENTED`. Ya funcionan `GET /api/v1/health` de las dos APIs públicas y todas las rutas del MetaServer: en la API pública, `POST /auth/login` y `POST /admin/users` (tarea 07), las cinco rutas `/fs` (tarea 08), las tres de `/locks` (tarea 11), `GET /files/blocks` y las tres de `/uploads` (tarea 12), y `GET /cluster/metaservers` y `GET /admin/status` (tarea 13); en la interna, `POST /internal/blocks/confirm` (tarea 12), y `POST /internal/register` y `POST /internal/heartbeat` (tarea 13). En el DataNode funcionan `PUT /internal/blocks/{user}/{hash}` y `GET /blocks/{hash}` (tareas 09 y 10); `PUT /blocks/{hash}` ya verifica el pase y después responde 501 hasta la tarea 14. La validación de parámetros funciona en todas las rutas: una petición inválida recibe 400 antes de llegar al stub.
- **Orden de las comprobaciones:** en las rutas con JWT, primero se comprueba el token y después los parámetros. Una petición sin token, o con un token inválido, responde 401 `E_AUTH` aunque además traiga una ruta inválida; con un token válido, la ruta inválida responde 400. Así, quien no se autenticó no puede usar la API para averiguar qué rutas son válidas.

Todos los ejemplos cuentan la misma historia: la usuaria `ana` reemplaza `/docs/informe.pdf` (versión 3 → 4; 20 MiB en tres bloques: 8 MiB, 8 MiB y 4 MiB) en el despliegue de DA-13. Las IP son de ejemplo; las reales las escribe el script de despliegue. El instante `1793628090000` es el 2 de noviembre de 2026 a las 14:01:30 UTC.

| Nodos | VM | Zona | IP privada |
|---|---|---|---|
| meta1, dn1 | VM1 | us-east-1a | 172.31.0.10 |
| meta2, dn2 | VM2 | us-east-1b | 172.31.16.10 |
| meta3, dn3 | VM3 | us-east-1c | 172.31.32.10 |
| dn4 | VM4 | us-east-1b | 172.31.16.20 |
| cliente | VM5 | us-east-1a | 172.31.0.20 |

### Cómo se encadenan las rutas

**Escritura** (`put`, o `close()` de un archivo abierto en `w`, `a` o `rw`; DA-04, DA-08, DA-14). Es lo que RF3 pide declarar: `w` crea o reemplaza el archivo, `a` anexa al final y `rw` modifica partes; `write()` acepta offsets entre 0 y el tamaño actual (sin huecos) y reescribe completos los bloques que toca, y `close()` publica la versión nueva. `a` y `rw` sobre un archivo que no existe lo crean con `base_version: 0`, igual que `w`; con `rw`, el único offset válido es entonces 0. Quién valida qué: el cliente rechaza en `write()` un offset mayor que el tamaño actual, antes de llamar al MetaServer; el MetaServer comprueba en el commit que el resultado no tenga huecos ni trunque el archivo (sección 2).

1. `POST /locks` toma el lock del archivo y devuelve `lock_id`. Mientras escribe, el cliente lo renueva cada `DFS_LOCK_RENEW_S` con `PUT /locks/{lock_id}`. Si una renovación responde 409 `E_LOCK_LOST`, perdió el lock y empieza de nuevo.
2. `POST /uploads` con los bloques nuevos o cambiados y el `block_size` con que el cliente partió el archivo. El MetaServer responde cuáles ya están guardados (`present`) y, para cada uno de los que faltan, el primario y un pase de escritura.
3. El cliente envía cada bloque faltante con `PUT /blocks/{hash}` al primario. El primario lo reenvía a los secundarios del pase (2, o 1 si solo quedan 2 DataNodes vivos; DA-05) con `PUT /internal/blocks/{user}/{hash}`, confirma con `POST /internal/blocks/confirm` y solo entonces responde 201.
4. Si un envío falla con un error reintentable, el cliente pide otros destinos con `POST /uploads/{upload_id}/realloc` y reenvía el mismo bloque.
5. `POST /uploads/{upload_id}/commit` publica la versión nueva (G1, G4). Si responde 422 `E_REPLICAS`, el cliente vuelve al paso 2 con la misma lista.
6. `DELETE /locks/{lock_id}` libera el lock.

**Lectura** (`get`, o `open()` en `r`): `GET /files/blocks` entrega la lista de bloques con sus réplicas y un pase de lectura por bloque; el cliente pide cada bloque con `GET /blocks/{hash}` a la primera réplica que responda, pasa a la siguiente si recibe 422 `E_CHECKSUM` o 404 `E_NOT_FOUND`, y verifica el hash. Por cada bloque recibe solo las réplicas vivas; si un bloque no tiene ninguna, el cliente no puede leerlo hasta que vuelva alguno de sus DataNodes y lo informa como un error que se puede reintentar más tarde. Si la versión que abrió ya no está retenida, `GET /files/blocks` responde 404 y el cliente reabre el archivo (DA-14).

**Borrado** (`rm`): una sola llamada a `DELETE /fs/rm`; el MetaServer toma el lock por su cuenta.

## 2. MetaServer público (puerto 8443, HTTPS)

Autenticación con `Authorization: Bearer <JWT>` (sección 7.1), salvo donde dice "ninguna".

| Método | Ruta | Auth | Respuesta | Errores | Ref. |
|---|---|---|---|---|---|
| POST | `/api/v1/auth/login` | ninguna | 200 `LoginResponse` | 400, 401, 503 | DA-10 |
| GET | `/api/v1/cluster/metaservers` | ninguna | 200 `MetaServerListResponse` | 503 | DA-11 |
| GET | `/api/v1/fs/ls?path=` | JWT | 200 `LsResponse` | 400, 401, 404, 503 | RF1 |
| GET | `/api/v1/fs/stat?path=` | JWT | 200 `StatResponse` | 400, 401, 404, 503 | RF1 |
| POST | `/api/v1/fs/mkdir` | JWT | 200 `OkResponse` | 400, 401, 404, 409 `E_EXISTS`, 503 | RF1 |
| DELETE | `/api/v1/fs/rmdir?path=` | JWT | 200 `OkResponse` | 400, 401, 404, 409 `E_NOT_EMPTY`, 503 | RF1 |
| DELETE | `/api/v1/fs/rm?path=` | JWT | 200 `OkResponse` | 400, 401, 404, 423, 503 | RF1, DA-14 |
| POST | `/api/v1/locks` | JWT | 200 `LockResponse` | 400, 401, 423, 503 | DA-08 |
| PUT | `/api/v1/locks/{lock_id}` | JWT | 200 `LockResponse` | 401, 403, 409 `E_LOCK_LOST`, 503 | DA-08 |
| DELETE | `/api/v1/locks/{lock_id}` | JWT | 200 `OkResponse` | 401, 403, 503 | DA-08 |
| GET | `/api/v1/files/blocks?path=&version=` | JWT | 200 `FileBlocksResponse` | 400, 401, 404, 503 | DA-05, DA-11 |
| POST | `/api/v1/uploads` | JWT | 200 `UploadResponse` | 400, 401, 403, 404, 409, 503, 507 | DA-02, DA-04 |
| POST | `/api/v1/uploads/{upload_id}/realloc` | JWT | 200 `ReallocResponse` | 400, 401, 403, 404, 503, 507 | DA-04 |
| POST | `/api/v1/uploads/{upload_id}/commit` | JWT | 200 `CommitResponse` | 400, 401, 403, 404, 409, 422 `E_REPLICAS`, 503 | DA-08, G1, G4 |
| POST | `/api/v1/admin/users` | JWT admin | 200 `OkResponse` | 400, 401, 403, 409 `E_EXISTS`, 503 | DA-10 |
| GET | `/api/v1/admin/status` | JWT admin | 200 `AdminStatusResponse` | 401, 403, 503 | DA-16 |
| GET | `/api/v1/health` | ninguna | 200 `HealthResponse` | — | — |

### POST /api/v1/auth/login

Verifica la contraseña (Argon2id) y entrega un JWT que dura `DFS_JWT_TTL_S`. Usuario o contraseña incorrectos: 401 `E_AUTH`, con el mismo cuerpo en los dos casos, para no revelar si el usuario existe.

```http
POST /api/v1/auth/login
Content-Type: application/json
```

```json
{
  "user": "ana",
  "password": "caballo-correcto-bateria"
}
```

Respuesta 200:

```json
{
  "token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiJhbmEiLCJhZG1pbiI6ZmFsc2UsImlhdCI6MTc5MzYyODAwMCwiZXhwIjoxNzkzNjMxNjAwfQ.jCuFONqnGHr41k8gQqELbF7cDtJhsfrCuGdn-vYMO94",
  "expires_in": 3600
}
```

### GET /api/v1/cluster/metaservers

Lista actual de MetaServers vivos, leída de `/nodes/meta/` en etcd (DA-11). El cliente y los DataNodes tienen en `DFS_META_SEEDS` las IP de los 3 MetaServers, sin puerto; prueban en orden `https://{ip}:{DFS_META_PUBLIC_PORT}/api/v1/cluster/metaservers` hasta que una responde. Con la lista, el cliente usa `public_port` y el DataNode `internal_port`. Como toda respuesta de la API, es un objeto: la lista va en `metaservers`.

```http
GET /api/v1/cluster/metaservers
```

Respuesta 200:

```json
{
  "metaservers": [
    {
      "meta_id": "meta1",
      "ip": "172.31.0.10",
      "public_port": 8443,
      "internal_port": 8444
    },
    {
      "meta_id": "meta2",
      "ip": "172.31.16.10",
      "public_port": 8443,
      "internal_port": 8444
    },
    {
      "meta_id": "meta3",
      "ip": "172.31.32.10",
      "public_port": 8443,
      "internal_port": 8444
    }
  ]
}
```

### GET /api/v1/fs/ls

Entradas de un directorio. La raíz `/` existe siempre. Un directorio aparece con `size` 0, `version` 0 y `modified_at` igual a su `created_at`.

```http
GET /api/v1/fs/ls?path=/docs
Authorization: Bearer <JWT>
```

Respuesta 200:

```json
{
  "entries": [
    {
      "name": "fotos",
      "type": "dir",
      "size": 0,
      "version": 0,
      "modified_at": 1793178900000
    },
    {
      "name": "informe.pdf",
      "type": "file",
      "size": 20971520,
      "version": 4,
      "modified_at": 1793628090000
    }
  ]
}
```

Sobre un archivo, `ls` devuelve ese archivo como única entrada, como `ls archivo` en Linux:

```http
GET /api/v1/fs/ls?path=/docs/informe.pdf
Authorization: Bearer <JWT>
```

Respuesta 200:

```json
{
  "entries": [
    {
      "name": "informe.pdf",
      "type": "file",
      "size": 20971520,
      "version": 4,
      "modified_at": 1793628090000
    }
  ]
}
```

### GET /api/v1/fs/stat

Tipo, tamaño, versión actual y fecha de un archivo o directorio, con los mismos valores que en `ls`.

```http
GET /api/v1/fs/stat?path=/docs/informe.pdf
Authorization: Bearer <JWT>
```

Respuesta 200:

```json
{
  "path": "/docs/informe.pdf",
  "type": "file",
  "size": 20971520,
  "version": 4,
  "modified_at": 1793628090000
}
```

La raíz no se guarda en etcd y no tiene `created_at`, así que `stat /` devuelve `modified_at` 0:

```http
GET /api/v1/fs/stat?path=/
Authorization: Bearer <JWT>
```

Respuesta 200:

```json
{
  "path": "/",
  "type": "dir",
  "size": 0,
  "version": 0,
  "modified_at": 0
}
```

### POST /api/v1/fs/mkdir

Crea un directorio y guarda `{type: "dir", created_at}` en `/ns/{user}{path}`, con la transacción de creación de la sección 8. No crea los padres: si el padre no existe o es un archivo, 404 `E_NOT_FOUND`. Si la ruta ya existe, incluida la raíz, 409 `E_EXISTS`. Qué responde si otro cliente cambia algo a la vez: sección 11, punto 5.

```http
POST /api/v1/fs/mkdir
Authorization: Bearer <JWT>
Content-Type: application/json
```

```json
{
  "path": "/docs/fotos"
}
```

Respuesta 200:

```json
{
  "ok": true
}
```

### DELETE /api/v1/fs/rmdir

Borra un directorio vacío, con la transacción de borrado de la sección 8. Si tiene entradas, 409 `E_NOT_EMPTY`; si la ruta es un archivo o la raíz `/` (que existe siempre), 400 `E_BAD_REQUEST`. Qué responde si otro cliente cambia algo a la vez: sección 11, punto 5.

```http
DELETE /api/v1/fs/rmdir?path=/docs/fotos
Authorization: Bearer <JWT>
```

Respuesta 200:

```json
{
  "ok": true
}
```

### DELETE /api/v1/fs/rm

Borra un archivo. El MetaServer toma internamente el lock del archivo con espera 0: si otro cliente lo tiene, 423 `E_LOCKED`. El cliente no llama a `/locks` para borrar. La versión actual pasa a retenida (`replaced_at`) y la limpieza la quita cuando vence `DFS_RETENTION_S` (DA-02, DA-14). Un lector que ya la tenía abierta termina con los pases que tiene, pero no puede renovarlos: la ruta ya no existe y `GET /files/blocks` responde 404. Mientras dure la retención, la limpieza no borra sus bloques, porque el manifiesto guarda el usuario (sección 8). Si la ruta es un directorio, incluida la raíz `/`, 400 `E_BAD_REQUEST`. Si al terminar no se puede soltar el lock, la respuesta no cambia (sección 11, punto 6).

```http
DELETE /api/v1/fs/rm?path=/docs/borrador.txt
Authorization: Bearer <JWT>
```

Respuesta 200:

```json
{
  "ok": true
}
```

### POST /api/v1/locks

Toma el lock de un archivo con el servicio de lock de etcd (DA-08). Cada intento usa su propio lease de `DFS_LOCK_LEASE_S`. Los escritores esperan en fila, en orden de llegada, hasta `wait_s` segundos: de 0 a 60, y 30 si no se envía; fuera de ese rango, 400 `E_BAD_REQUEST`. Estos límites son parte del contrato y van fijos en el modelo; no salen de variables de entorno. Si la espera se agota, o si `wait_s` es 0 y el lock está ocupado, responde 423 `E_LOCKED` y el MetaServer revoca el lease del intento para sacarlo de la fila. Si la ruta es un directorio, 400 `E_BAD_REQUEST`. La ruta no tiene que existir todavía: con `w`, el lock se toma antes de crear el archivo; que exista el directorio padre lo comprueban la reserva y el commit (404). `lock_id` es el ID del lease de etcd del intento, en hexadecimal (sección 8); para el cliente es opaco.

```http
POST /api/v1/locks
Authorization: Bearer <JWT>
Content-Type: application/json
```

```json
{
  "path": "/docs/informe.pdf",
  "wait_s": 30
}
```

Respuesta 200:

```json
{
  "lock_id": "694d8a1c2e3b4f05",
  "lease_s": 30
}
```

### PUT /api/v1/locks/{lock_id}

Renueva el lease del lock; el cliente lo hace cada `DFS_LOCK_RENEW_S` (DA-08). Si el lock ya venció, 409 `E_LOCK_LOST`: igual que en el commit, quiere decir "perdiste el lock, empieza de nuevo". Un `lock_id` de otro usuario: 403 `E_FORBIDDEN`. Un `lock_id` mal formado se trata como uno que ya no existe (sección 11, punto 3).

```http
PUT /api/v1/locks/694d8a1c2e3b4f05
Authorization: Bearer <JWT>
```

Respuesta 200:

```json
{
  "lock_id": "694d8a1c2e3b4f05",
  "lease_s": 30
}
```

### DELETE /api/v1/locks/{lock_id}

Libera el lock revocando su lease; entra el siguiente de la fila. Es idempotente: si el lock ya no existe, también responde 200 `{"ok": true}`, y lo mismo con un `lock_id` mal formado (sección 11, punto 3). Un `lock_id` de otro usuario: 403 `E_FORBIDDEN`.

```http
DELETE /api/v1/locks/694d8a1c2e3b4f05
Authorization: Bearer <JWT>
```

Respuesta 200:

```json
{
  "ok": true
}
```

### GET /api/v1/files/blocks

Manifiesto de una versión: la actual, o la indicada en `version` mientras siga retenida (`DFS_RETENTION_S`, DA-14); pasada la retención, esa versión responde 404 `E_NOT_FOUND` y el cliente reabre el archivo, como pide DA-14. Cada bloque trae `stored_on`, todos los DataNodes que tienen copia según etcd, vivos o caídos; `replicas`, los vivos con su dirección (`{node_id, address}`, con el puerto público del DataNode; un DataNode caído no tiene dirección en `/nodes/dn/`), primero los de la zona que el cliente manda en `X-Client-Zone` (DA-11); y un pase de lectura. Si ninguno de sus DataNodes está vivo, `replicas` va vacía: el lector no puede leer ese bloque por ahora, y `dfs stat --blocks` (DA-16) sigue mostrando en `stored_on` dónde está. Si la ruta es un directorio, 400 `E_BAD_REQUEST`. `file_sha256` es `null` si la versión se publicó con `a` o `rw` (sección 13); entonces `get` no compara el hash del archivo, y la integridad la dan el hash de cada bloque y el orden de `hashes` en etcd. Para renovar pases de lectura vencidos se vuelve a pedir con la misma `version` (sección 7.2). También lo usa `dfs stat --blocks` (DA-16).

```http
GET /api/v1/files/blocks?path=/docs/informe.pdf
Authorization: Bearer <JWT>
X-Client-Zone: us-east-1a
```

Respuesta 200:

```json
{
  "path": "/docs/informe.pdf",
  "version": 4,
  "size": 20971520,
  "block_size": 8388608,
  "file_sha256": "247a8174ec0c61e4f6c2f0299b27648c9ef3dc8482f7083d01c8a389976a6a76",
  "blocks": [
    {
      "index": 0,
      "hash": "6ce5a55b446b43e4e935c44695a9a09f161e8e87d1ff2359adebc5f9ee5ee5cf",
      "size": 8388608,
      "stored_on": [
        "dn2",
        "dn3",
        "dn4"
      ],
      "replicas": [
        {
          "node_id": "dn2",
          "address": "172.31.16.10:9443"
        },
        {
          "node_id": "dn3",
          "address": "172.31.32.10:9443"
        },
        {
          "node_id": "dn4",
          "address": "172.31.16.20:9443"
        }
      ],
      "pass": "eyJ1c2VyIjoiYW5hIiwiaGFzaCI6IjZjZTVhNTViNDQ2YjQzZTRlOTM1YzQ0Njk1YTlhMDlmMTYxZThlODdkMWZmMjM1OWFkZWJjNWY5ZWU1ZWU1Y2YiLCJvcCI6InIiLCJleHAiOjE3OTM2Mjg3MjB9.z0WiI50c6AfbzmSTYB6-LFdTZU2A_zS5GxQMu4iXGKc"
    },
    {
      "index": 1,
      "hash": "b64bb9046f61749097db060781bcbbba317c08ff0ac378e9aed9a6d18217e449",
      "size": 8388608,
      "stored_on": [
        "dn3",
        "dn4",
        "dn1"
      ],
      "replicas": [
        {
          "node_id": "dn1",
          "address": "172.31.0.10:9443"
        },
        {
          "node_id": "dn3",
          "address": "172.31.32.10:9443"
        },
        {
          "node_id": "dn4",
          "address": "172.31.16.20:9443"
        }
      ],
      "pass": "eyJ1c2VyIjoiYW5hIiwiaGFzaCI6ImI2NGJiOTA0NmY2MTc0OTA5N2RiMDYwNzgxYmNiYmJhMzE3YzA4ZmYwYWMzNzhlOWFlZDlhNmQxODIxN2U0NDkiLCJvcCI6InIiLCJleHAiOjE3OTM2Mjg3MjB9.2aosUSBmyey_LOS0yTROyMoPw2rPa0MY_RU8amfSLuc"
    },
    {
      "index": 2,
      "hash": "8de70b36bbfaaf4bd8fecc81370e32e388e0fa57cabc32a1fbd46777ac7ac937",
      "size": 4194304,
      "stored_on": [
        "dn1",
        "dn2",
        "dn3"
      ],
      "replicas": [
        {
          "node_id": "dn1",
          "address": "172.31.0.10:9443"
        },
        {
          "node_id": "dn2",
          "address": "172.31.16.10:9443"
        },
        {
          "node_id": "dn3",
          "address": "172.31.32.10:9443"
        }
      ],
      "pass": "eyJ1c2VyIjoiYW5hIiwiaGFzaCI6IjhkZTcwYjM2YmJmYWFmNGJkOGZlY2M4MTM3MGUzMmUzODhlMGZhNTdjYWJjMzJhMWZiZDQ2Nzc3YWM3YWM5MzciLCJvcCI6InIiLCJleHAiOjE3OTM2Mjg3MjB9.3ekV5C2jUAFMJZrsb41tDqE8q4U2BD-FH1hE1DwYnYg"
    }
  ]
}
```

### POST /api/v1/uploads

Reserva una subida (DA-02, DA-03, DA-04, DA-05). `blocks` trae los bloques nuevos o cambiados de la versión que se va a publicar, y `block_size` los bytes por bloque (DA-03):

- con `w`, o con `a`/`rw` sobre un archivo que no existe, debe ser `DFS_BLOCK_MIB × 1048576`;
- con `a` y `rw` sobre un archivo que existe, el `block_size` de su versión actual (el cliente lo lee en `GET /files/blocks`);
- si no coincide: 400 `E_BAD_REQUEST`.

`a` y `rw` sobre un archivo que no existe lo crean con `base_version: 0`, igual que `w`. El MetaServer:

- marca como `present` los que ya existen en `/blocks/{user}/{hash}` con copias en DataNodes vivos que cubren al menos 2 zonas (la misma regla del commit y de `/internal/blocks/confirm`); no se vuelven a enviar, y actualiza su `touched_at` para que la limpieza no los borre. Un bloque cuya clave existe pero no cumple esa regla va en `missing` y se vuelve a enviar; la confirmación une su `stored_on` con el que tenía;
- para cada faltante elige el primario y 2 secundarios por turnos desde un inicio aleatorio, en al menos 2 zonas (si solo quedan 2 DataNodes vivos en zonas distintas, usa esos 2: un primario y un secundario, DA-05), y entrega un pase de escritura que lleva las direcciones internas de los secundarios;
- guarda la subida en `/uploads/{upload_id}`, con su `block_size` para el manifiesto, atada al lease del lock: si el lock vence, la subida desaparece sola. Los bloques que alcanzaron a confirmarse y que nadie usa los borra la limpieza normal (DA-02).

Exige el lock del archivo (modos `w`, `a` y `rw`). Si la escritura deja un hueco o la ruta es un directorio: 400 `E_BAD_REQUEST`. Si el directorio padre no existe o es un archivo: 404 `E_NOT_FOUND`. Un `lock_id` de otro usuario: 403 `E_FORBIDDEN`. Para fallar antes de transferir bloques, responde 409 `E_VERSION_CONFLICT` si la versión base ya no es la actual, y 409 `E_LOCK_LOST` si el lock venció o el `lock_id` es del mismo usuario pero de otro archivo. Hacen falta al menos `DFS_W` DataNodes vivos que cubran 2 zonas: si no los hay, 503 `E_UNAVAILABLE` (reintentable); si los hay pero no alcanzan contando solo los que tienen espacio para el bloque, 507 `E_NO_SPACE`. `DFS_W` es cuántas copias espera el primario antes de responder (DA-04); el commit exige siempre 2 copias vivas en 2 zonas (G1). Con `DFS_W=3`, que sirve para medir el costo de la replicación, hacen falta 3 DataNodes vivos: el caso de DA-05 con solo 2 DataNodes vivos aplica únicamente con `DFS_W=2`. Índices repetidos en `blocks`, o un bloque mayor que `block_size`: 400 `E_BAD_REQUEST`.

```http
POST /api/v1/uploads
Authorization: Bearer <JWT>
Content-Type: application/json
```

```json
{
  "path": "/docs/informe.pdf",
  "mode": "w",
  "base_version": 3,
  "block_size": 8388608,
  "lock_id": "694d8a1c2e3b4f05",
  "blocks": [
    {
      "index": 0,
      "hash": "6ce5a55b446b43e4e935c44695a9a09f161e8e87d1ff2359adebc5f9ee5ee5cf",
      "size": 8388608
    },
    {
      "index": 1,
      "hash": "b64bb9046f61749097db060781bcbbba317c08ff0ac378e9aed9a6d18217e449",
      "size": 8388608
    },
    {
      "index": 2,
      "hash": "8de70b36bbfaaf4bd8fecc81370e32e388e0fa57cabc32a1fbd46777ac7ac937",
      "size": 4194304
    }
  ]
}
```

Respuesta 200 (el bloque 0 no cambió y ya estaba guardado):

```json
{
  "upload_id": "9b2f6d1e-4c3a-4e8b-9f1d-2a7c5e8b1d43",
  "present": [
    0
  ],
  "missing": [
    {
      "index": 1,
      "hash": "b64bb9046f61749097db060781bcbbba317c08ff0ac378e9aed9a6d18217e449",
      "primary": {
        "node_id": "dn3",
        "address": "172.31.32.10:9443"
      },
      "pass": "eyJ1c2VyIjoiYW5hIiwiaGFzaCI6ImI2NGJiOTA0NmY2MTc0OTA5N2RiMDYwNzgxYmNiYmJhMzE3YzA4ZmYwYWMzNzhlOWFlZDlhNmQxODIxN2U0NDkiLCJvcCI6InciLCJzZWNvbmRhcmllcyI6WyIxNzIuMzEuMTYuMjA6OTQ0NCIsIjE3Mi4zMS4wLjEwOjk0NDQiXSwiZXhwIjoxNzkzNjI4NjAwfQ.cmdifTy29ib542pe296X3Ud6ZGdU0WvkgzZm4fWTDC0"
    },
    {
      "index": 2,
      "hash": "8de70b36bbfaaf4bd8fecc81370e32e388e0fa57cabc32a1fbd46777ac7ac937",
      "primary": {
        "node_id": "dn4",
        "address": "172.31.16.20:9443"
      },
      "pass": "eyJ1c2VyIjoiYW5hIiwiaGFzaCI6IjhkZTcwYjM2YmJmYWFmNGJkOGZlY2M4MTM3MGUzMmUzODhlMGZhNTdjYWJjMzJhMWZiZDQ2Nzc3YWM3YWM5MzciLCJvcCI6InciLCJzZWNvbmRhcmllcyI6WyIxNzIuMzEuMC4xMDo5NDQ0IiwiMTcyLjMxLjE2LjEwOjk0NDQiXSwiZXhwIjoxNzkzNjI4NjAwfQ.Euwkh7fQO8z-ga48XdTOvZBGlqRQRL8170T6T_eotMs"
    }
  ]
}
```

### POST /api/v1/uploads/{upload_id}/realloc

Cuando un envío falla con un error reintentable (`E_CHECKSUM`, `E_REPLICAS`, `E_UNAVAILABLE`) o el primario no responde, el cliente pide otro primario sin los nodos que fallaron y reenvía el mismo bloque, con el mismo hash (DA-04). Con los mismos índices y `exclude_nodes: []` sirve también para renovar pases de escritura vencidos (sección 7.2). Un `upload_id` de otro usuario: 403 `E_FORBIDDEN`. Sigue la regla de la reserva: sin al menos `DFS_W` DataNodes vivos que cubran 2 zonas, sin contar `exclude_nodes`, 503 `E_UNAVAILABLE`.

```http
POST /api/v1/uploads/9b2f6d1e-4c3a-4e8b-9f1d-2a7c5e8b1d43/realloc
Authorization: Bearer <JWT>
Content-Type: application/json
```

```json
{
  "indices": [
    2
  ],
  "exclude_nodes": [
    "dn4"
  ]
}
```

Respuesta 200:

```json
{
  "missing": [
    {
      "index": 2,
      "hash": "8de70b36bbfaaf4bd8fecc81370e32e388e0fa57cabc32a1fbd46777ac7ac937",
      "primary": {
        "node_id": "dn1",
        "address": "172.31.0.10:9443"
      },
      "pass": "eyJ1c2VyIjoiYW5hIiwiaGFzaCI6IjhkZTcwYjM2YmJmYWFmNGJkOGZlY2M4MTM3MGUzMmUzODhlMGZhNTdjYWJjMzJhMWZiZDQ2Nzc3YWM3YWM5MzciLCJvcCI6InciLCJzZWNvbmRhcmllcyI6WyIxNzIuMzEuMTYuMTA6OTQ0NCIsIjE3Mi4zMS4zMi4xMDo5NDQ0Il0sImV4cCI6MTc5MzYyODY2MH0.Df5QOig70VjvVVw_06qWo8BllA6DahuI9B4VxRR41uE"
    }
  ]
}
```

### POST /api/v1/uploads/{upload_id}/commit

Publica la versión nueva con una transacción de etcd (DA-08). `hashes` es la lista completa de bloques de la versión nueva, en orden. `file_sha256` es opcional: `put` y `open` en `w` lo envían, y `a` y `rw` envían `null`, porque calcularlo obligaría al cliente a descargar el archivo entero para cambiar un solo bloque (sección 13). Responde:

- 409 `E_VERSION_CONFLICT` si `base_version` ya no es la actual, o 409 `E_LOCK_LOST` si la clave del lock ya no existe o el `lock_id` es de otro archivo (G4);
- 422 `E_REPLICAS` (reintentable) si algún bloque de `hashes` que no está en la versión base no tiene copias en DataNodes vivos que cubran 2 zonas (G1; la misma regla de `present`), o si falta la clave `/blocks/{user}/{hash}` de algún bloque de `hashes`. Los bloques que ya estaban en la versión base solo necesitan que su clave exista (sección 13). El cliente vuelve a llamar `POST /uploads` con la misma lista, que le devuelve los bloques que faltan (DA-02), los envía y repite el commit;
- 400 `E_BAD_REQUEST` si el resultado tendría huecos o truncaría el archivo. Con los tamaños guardados en `/blocks/{user}/{hash}`, el MetaServer comprueba que `hashes` tenga ⌈`size` / `block_size`⌉ bloques; que todos midan `block_size` salvo el último, que mide lo que falta hasta `size`; con `a` y `rw`, que `size` no sea menor que el de la versión base (sin truncado, DA-14); y con `a`, que los primeros ⌊tamaño base / `block_size`⌋ hashes sean los de la versión base (anexar no cambia lo que ya estaba);
- 404 `E_NOT_FOUND` si el directorio padre ya no existe o es un archivo;
- 403 `E_FORBIDDEN` si el `upload_id` o el `lock_id` son de otro usuario.

Con `base_version: 0` el commit crea la entrada, con la transacción de creación de la sección 8.

```http
POST /api/v1/uploads/9b2f6d1e-4c3a-4e8b-9f1d-2a7c5e8b1d43/commit
Authorization: Bearer <JWT>
Content-Type: application/json
```

```json
{
  "base_version": 3,
  "lock_id": "694d8a1c2e3b4f05",
  "hashes": [
    "6ce5a55b446b43e4e935c44695a9a09f161e8e87d1ff2359adebc5f9ee5ee5cf",
    "b64bb9046f61749097db060781bcbbba317c08ff0ac378e9aed9a6d18217e449",
    "8de70b36bbfaaf4bd8fecc81370e32e388e0fa57cabc32a1fbd46777ac7ac937"
  ],
  "size": 20971520,
  "file_sha256": "247a8174ec0c61e4f6c2f0299b27648c9ef3dc8482f7083d01c8a389976a6a76"
}
```

Respuesta 200:

```json
{
  "path": "/docs/informe.pdf",
  "version": 4
}
```

### POST /api/v1/admin/users

Crea un usuario; la contraseña se guarda con Argon2id. Exige un JWT con `admin: true` (si no, 403 `E_FORBIDDEN`). Si el usuario ya existe: 409 `E_EXISTS`.

```http
POST /api/v1/admin/users
Authorization: Bearer <JWT de admin>
Content-Type: application/json
```

```json
{
  "user": "bruno",
  "password": "otra-clave-larga-de-prueba"
}
```

Respuesta 200:

```json
{
  "ok": true
}
```

**Primer administrador.** Al arrancar, si no existe ningún administrador y `DFS_ADMIN_PASSWORD_FILE` apunta a un archivo, el MetaServer crea el usuario `admin` con esa contraseña. Lo hace con una transacción de etcd que solo escribe `/users/admin` si la clave no existe, para que tres MetaServers arrancando a la vez no choquen. Si `admin` ya existe, no se cambia su contraseña (sección 11, punto 7).

### GET /api/v1/admin/status

Estado del sistema para `dfs status` (DA-16): líder actual, MetaServers, DataNodes con zona y estado, bloques con menos de 3 copias vivas y órdenes pendientes. Exige un JWT con `admin: true`. Cada MetaServer tiene la misma forma que en `GET /cluster/metaservers`. La clave `/nodes/dn/{node_id}` de un DataNode caído desaparece al vencer su lease, así que de él solo se conoce el `node_id` (por los `stored_on`) y sus campos `address`, `zone` y `free_bytes` van en `null`. El ejemplo muestra el sistema poco después de caer dn4: dos bloques quedaron con 2 copias y el líder ya ordenó reponer la tercera.

```http
GET /api/v1/admin/status
Authorization: Bearer <JWT de admin>
```

Respuesta 200:

```json
{
  "leader": "meta2",
  "metaservers": [
    {
      "meta_id": "meta1",
      "ip": "172.31.0.10",
      "public_port": 8443,
      "internal_port": 8444
    },
    {
      "meta_id": "meta2",
      "ip": "172.31.16.10",
      "public_port": 8443,
      "internal_port": 8444
    },
    {
      "meta_id": "meta3",
      "ip": "172.31.32.10",
      "public_port": 8443,
      "internal_port": 8444
    }
  ],
  "datanodes": [
    {
      "node_id": "dn1",
      "address": "172.31.0.10:9443",
      "zone": "us-east-1a",
      "free_bytes": 15032385536,
      "alive": true
    },
    {
      "node_id": "dn2",
      "address": "172.31.16.10:9443",
      "zone": "us-east-1b",
      "free_bytes": 14495514624,
      "alive": true
    },
    {
      "node_id": "dn3",
      "address": "172.31.32.10:9443",
      "zone": "us-east-1c",
      "free_bytes": 15569256448,
      "alive": true
    },
    {
      "node_id": "dn4",
      "address": null,
      "zone": null,
      "free_bytes": null,
      "alive": false
    }
  ],
  "under_replicated": [
    {
      "user": "ana",
      "hash": "6ce5a55b446b43e4e935c44695a9a09f161e8e87d1ff2359adebc5f9ee5ee5cf",
      "alive_on": [
        "dn2",
        "dn3"
      ]
    },
    {
      "user": "ana",
      "hash": "b64bb9046f61749097db060781bcbbba317c08ff0ac378e9aed9a6d18217e449",
      "alive_on": [
        "dn3",
        "dn1"
      ]
    }
  ],
  "pending_orders": [
    {
      "node_id": "dn2",
      "order": {
        "order_id": "b7e3c2a1-4d5f-4a6b-8c9d-0e1f2a3b4c5d",
        "type": "copy",
        "user": "ana",
        "hash": "6ce5a55b446b43e4e935c44695a9a09f161e8e87d1ff2359adebc5f9ee5ee5cf",
        "target": {
          "node_id": "dn1",
          "address": "172.31.0.10:9444"
        }
      }
    },
    {
      "node_id": "dn3",
      "order": {
        "order_id": "c8f4d3b2-5e6a-4b7c-9d0e-1f2a3b4c5d6e",
        "type": "copy",
        "user": "ana",
        "hash": "b64bb9046f61749097db060781bcbbba317c08ff0ac378e9aed9a6d18217e449",
        "target": {
          "node_id": "dn2",
          "address": "172.31.16.10:9444"
        }
      }
    }
  ]
}
```

### GET /api/v1/health

```http
GET /api/v1/health
```

Respuesta 200:

```json
{
  "status": "ok"
}
```

## 3. MetaServer interno (puerto 8444, mTLS)

Solo lo usan los DataNodes, con un certificado firmado por la CA propia (DA-06). Sin JWT: la identidad la da el certificado, y en el OpenAPI aparece como el esquema de seguridad `mTLS` (tipo `mutualTLS`).

Como todos los nodos tienen un certificado de la misma CA, el MetaServer además ata cada llamada a su nodo por la **IP de origen** de la conexión: en `register` debe ser la `ip` del cuerpo y, si el `node_id` ya está registrado y vivo, la que tiene registrada; en `heartbeat`, la de `/nodes/dn/{node_id}`; en `/blocks/confirm`, la del primario (el primer nodo de `stored_on`). Si no coincide, 403 `E_FORBIDDEN`. Así, un error de configuración como dos DataNodes con el mismo `DFS_NODE_ID` no borra ni pisa el registro del otro. No se usa el nombre (CN) del certificado porque uvicorn no le entrega a la aplicación el certificado del cliente.

| Método | Ruta | Respuesta | Errores | Ref. |
|---|---|---|---|---|
| POST | `/api/v1/internal/register` | 200 `RegisterResponse` | 400, 403, 503 | DA-11 |
| POST | `/api/v1/internal/heartbeat` | 200 `HeartbeatResponse` | 400, 403, 404, 503 | DA-12 |
| POST | `/api/v1/internal/blocks/confirm` | 200 `OkResponse` | 400, 403, 422 `E_REPLICAS`, 503 | DA-04, DA-05 |

### POST /api/v1/internal/register

El DataNode se registra al arrancar a través de cualquier MetaServer, con su IP privada de `DFS_NODE_IP` en `ip`. El MetaServer guarda `/nodes/dn/{node_id}` atada a un lease de `DFS_NODE_LEASE_S` y responde el lease y cada cuánto enviar el latido.

También manda `disk_id`. El DataNode lo crea la primera vez que arranca con `DFS_DATA_DIR` vacía y lo guarda en `DFS_DATA_DIR/.disk_id`; el punto evita chocar con la carpeta de un usuario, cuyo nombre no puede tener puntos. Si la carpeta tiene bloques pero no `.disk_id`, el DataNode no arranca: alguien tocó el disco a mano y hay que decidir qué hacer con esos bloques. El MetaServer compara el valor con `/nodes/disk/{node_id}`: si no había, solo lo guarda; si cambió (por ejemplo, se recreó la VM con un disco nuevo), primero quita ese nodo de todos los `stored_on`, porque sus copias ya no existen, y después guarda el valor nuevo. Si se corta a la mitad, el siguiente registro repite la limpieza. Así la re-réplica ve que faltan copias (G5).

```json
{
  "node_id": "dn1",
  "disk_id": "5f0c7e2a-9d41-4b8e-a3c6-1e2f3a4b5c6d",
  "ip": "172.31.0.10",
  "public_port": 9443,
  "internal_port": 9444,
  "zone": "us-east-1a",
  "free_bytes": 15032385536
}
```

Respuesta 200:

```json
{
  "lease_id": "694d8a1c2e3b4f12",
  "heartbeat_s": 5
}
```

### POST /api/v1/internal/heartbeat

Latido cada `DFS_HEARTBEAT_S`, a cualquier MetaServer (DA-12). El MetaServer renueva el lease del nodo y responde las órdenes pendientes (sección 7.3). Además:

- borra de `/orders/{node_id}/` las órdenes de `done_orders`; por cada `copy` cumplida agrega `target.node_id` a `stored_on` del bloque;
- por cada bloque de `bad_blocks` (copias que el DataNode encontró dañadas y borró, o que debía tener y no encontró; sección 4) quita este nodo de `stored_on`, y la re-réplica repone la copia.

Si el lease ya venció: 404 `E_NOT_FOUND`, y el DataNode se registra de nuevo.

```json
{
  "node_id": "dn2",
  "lease_id": "694d8a1c2e3b4f13",
  "free_bytes": 14495514624,
  "done_orders": [
    "a6d2b1f0-3c4e-4f5a-9b8c-7d6e5f4a3b2c"
  ],
  "bad_blocks": [
    {
      "user": "ana",
      "hash": "8de70b36bbfaaf4bd8fecc81370e32e388e0fa57cabc32a1fbd46777ac7ac937"
    }
  ]
}
```

Respuesta 200:

```json
{
  "orders": [
    {
      "order_id": "b7e3c2a1-4d5f-4a6b-8c9d-0e1f2a3b4c5d",
      "type": "copy",
      "user": "ana",
      "hash": "6ce5a55b446b43e4e935c44695a9a09f161e8e87d1ff2359adebc5f9ee5ee5cf",
      "target": {
        "node_id": "dn1",
        "address": "172.31.0.10:9444"
      }
    },
    {
      "order_id": "d9a5e4c3-6f7b-4c8d-8e1f-2a3b4c5d6e7f",
      "type": "delete",
      "user": "ana",
      "hash": "29124d08ab194c99b65e04622016f3306c6c39ce331241c0e089b10e66411607",
      "written_before_ms": 1793628599000
    }
  ]
}
```

### POST /api/v1/internal/blocks/confirm

Lo llama el DataNode primario cuando el bloque quedó en `DFS_W` nodos, antes de responderle al cliente (DA-04). El primario no conoce la zona de los secundarios, así que la comprobación de zonas la hace el MetaServer:

- busca en `/nodes/dn/` la zona de cada nodo de `stored_on`; un nodo sin clave (caído) no cuenta;
- si las copias no cubren 2 zonas, responde 422 `E_REPLICAS` y no escribe nada; el primario le pasa ese error al cliente;
- si las cubren, escribe `/blocks/{user}/{hash}`; si la clave ya existía, une las dos listas `stored_on` sin repetir.

```json
{
  "user": "ana",
  "hash": "b64bb9046f61749097db060781bcbbba317c08ff0ac378e9aed9a6d18217e449",
  "size": 8388608,
  "stored_on": [
    "dn3",
    "dn4",
    "dn1"
  ]
}
```

Respuesta 200:

```json
{
  "ok": true
}
```

## 4. DataNode público (puerto 9443, HTTPS)

Cada petición lleva el pase del bloque en `Authorization: Bearer <pase>` (sección 7.2): sin pase o con el pase vencido, 401 `E_AUTH`; con un pase mal formado, con firma inválida, de otro hash o de otra operación, 403 `E_FORBIDDEN`. El cuerpo de los bloques es binario (`application/octet-stream`, DA-06).

| Método | Ruta | Auth | Respuesta | Errores | Ref. |
|---|---|---|---|---|---|
| PUT | `/api/v1/blocks/{hash}` | pase `w` | 201 `BlockStoredResponse` | 400, 401, 403, 422 `E_CHECKSUM` / `E_REPLICAS`, 503, 507 | DA-04 |
| GET | `/api/v1/blocks/{hash}` | pase `r` | 200 binario | 401, 403, 404, 422 `E_CHECKSUM` | DA-09, DA-10, DA-12 |
| GET | `/api/v1/health` | ninguna | 200 `HealthResponse` | — | — |

### PUT /api/v1/blocks/{hash}

El primario recibe el bloque en un archivo temporal, verifica el hash, lo cifra con AES-256-GCM, hace fsync y lo renombra. Luego lo reenvía sin cifrar a los secundarios del pase, a la vez, espera hasta `DFS_PRIMARY_WAIT_S`, confirma al MetaServer y responde con la lista de nodos donde quedó (DA-04, DA-09). Si el cuerpo pasa de `DFS_BLOCK_MIB` MiB, 400 `E_BAD_REQUEST`. Errores reintentables: 422 `E_CHECKSUM` si el contenido no coincide con el hash y 422 `E_REPLICAS` si no logró `DFS_W` copias o si `/internal/blocks/confirm` respondió que no cubren 2 zonas; en ambos casos el cliente pide `realloc`.

```http
PUT /api/v1/blocks/b64bb9046f61749097db060781bcbbba317c08ff0ac378e9aed9a6d18217e449
Authorization: Bearer <pase de escritura>
Content-Type: application/octet-stream
Content-Length: 8388608

<8 MiB del bloque, sin cifrar>
```

Respuesta 201:

```json
{
  "hash": "b64bb9046f61749097db060781bcbbba317c08ff0ac378e9aed9a6d18217e449",
  "stored_on": [
    "dn3",
    "dn4",
    "dn1"
  ]
}
```

### GET /api/v1/blocks/{hash}

Descifra el bloque y lo devuelve en binario. El pase debe ser de lectura y para ese hash. Si la copia está dañada (falla el descifrado AES-GCM o el hash no coincide), responde 422 `E_CHECKSUM`, borra la copia y la reporta en `bad_blocks` de su siguiente latido. Si no tiene el bloque, responde 404 `E_NOT_FOUND` y también lo reporta en `bad_blocks`: el MetaServer lo tenía en `stored_on` y así lo corrige. En los dos casos el cliente prueba la siguiente réplica.

```http
GET /api/v1/blocks/6ce5a55b446b43e4e935c44695a9a09f161e8e87d1ff2359adebc5f9ee5ee5cf
Authorization: Bearer <pase de lectura>
```

Respuesta 200: `Content-Type: application/octet-stream`, con los 8 MiB del bloque sin cifrar.

### GET /api/v1/health

Igual que en el MetaServer: responde `{"status": "ok"}`.

## 5. DataNode interno (puerto 9444, mTLS)

Solo lo usan otros DataNodes, con certificado de la CA propia; en el OpenAPI, esquema de seguridad `mTLS`.

| Método | Ruta | Respuesta | Errores | Ref. |
|---|---|---|---|---|
| PUT | `/api/v1/internal/blocks/{user}/{hash}` | 201 `ReplicaStoredResponse` | 400, 422 `E_CHECKSUM`, 507 | DA-04, DA-09, DA-12 |

### PUT /api/v1/internal/blocks/{user}/{hash}

Recibe una réplica que envía el primario o una copia de re-réplica (orden `copy`). El cuerpo llega sin cifrar dentro de mTLS; el DataNode verifica el hash y lo cifra con su propia clave (DA-09). Si el cuerpo pasa de `DFS_BLOCK_MIB` MiB, 400 `E_BAD_REQUEST`.

```http
PUT /api/v1/internal/blocks/ana/b64bb9046f61749097db060781bcbbba317c08ff0ac378e9aed9a6d18217e449
Content-Type: application/octet-stream
Content-Length: 8388608

<8 MiB del bloque, sin cifrar>
```

Respuesta 201:

```json
{
  "hash": "b64bb9046f61749097db060781bcbbba317c08ff0ac378e9aed9a6d18217e449",
  "node_id": "dn4"
}
```

## 6. Errores

Toda respuesta de error tiene la forma `{error, message, retryable}`. En el código hay una sola excepción con código de error, `DFSError(code, message)` (`src/dfs_common/errors.py`): una ruta la lanza y las cuatro apps la convierten en esta respuesta, con el HTTP y el `retryable` de la tabla.

```json
{
  "error": "E_VERSION_CONFLICT",
  "message": "la versión base 3 ya no es la actual (4)",
  "retryable": false
}
```

| HTTP | Código | Cuándo | retryable |
|---|---|---|---|
| 400 | `E_BAD_REQUEST` | parámetros o ruta inválidos, escritura que deja un hueco o trunca el archivo, rm o rmdir sobre el tipo equivocado, o borrar la raíz / | false |
| 401 | `E_AUTH` | JWT ausente, inválido o vencido, o pase ausente o vencido | false |
| 403 | `E_FORBIDDEN` | pase mal formado, con firma inválida, de otro hash o de otra operación; ruta /admin sin rol admin; lock_id o upload_id de otro usuario; o un nodo que usa la API interna con un node_id ajeno | false |
| 404 | `E_NOT_FOUND` | ruta, bloque, versión o lease inexistente | false |
| 409 | `E_EXISTS` | la ruta ya existe | false |
| 409 | `E_NOT_EMPTY` | directorio no vacío | false |
| 409 | `E_VERSION_CONFLICT` | la versión base ya no es la actual | false |
| 409 | `E_LOCK_LOST` | el lock venció o es de otro archivo | false |
| 422 | `E_CHECKSUM` | el contenido no coincide con el hash, o la copia guardada está dañada | true |
| 422 | `E_REPLICAS` | faltan copias: no se alcanzó DFS_W al escribir, o no hay copias vivas en 2 zonas distintas | true |
| 423 | `E_LOCKED` | lock ocupado y la espera se agotó o era 0 | true |
| 500 | `E_INTERNAL` | error inesperado del servidor | false |
| 501 | `E_NOT_IMPLEMENTED` | ruta todavía sin implementar | false |
| 503 | `E_UNAVAILABLE` | etcd sin mayoría o sin DataNodes vivos suficientes | true |
| 507 | `E_NO_SPACE` | sin espacio en los DataNodes | false |

- **401 o 403 con un pase:** 401 `E_AUTH` si falta o venció (con firma válida), igual que un JWT vencido, y el cliente pide pases nuevos (sección 7.2); 403 `E_FORBIDDEN` si está mal formado, tiene firma inválida o es de otro hash o de otra operación.
- **403 `E_FORBIDDEN`** aparece solo en cuatro casos: un pase inválido en el DataNode; las rutas `/admin` sin rol de administrador; un `lock_id` o `upload_id` que pertenece a otro usuario; y una llamada a la API interna del MetaServer desde una IP que no es la de su `node_id` (sección 3). En `/fs`, `/locks` (al pedirlo por ruta), `/files` y `/uploads` no existe "ruta de otro usuario".
- **409 `E_LOCK_LOST`** siempre quiere decir "perdiste el lock, empieza de nuevo": al renovarlo, en la reserva o en el commit, sea porque venció o porque el `lock_id` es del mismo usuario pero de otro archivo.
- **`retryable: true`** quiere decir que el cliente puede reintentar: con `realloc` si `PUT /blocks` da `E_CHECKSUM` o `E_REPLICAS`; con la siguiente réplica si `GET /blocks` da `E_CHECKSUM` (con un 404 también se pasa a la siguiente réplica, aunque 404 no sea reintentable: la copia falta solo en ese DataNode); repitiendo `POST /uploads` si el commit da `E_REPLICAS`; más tarde si es `E_LOCKED` o `E_UNAVAILABLE`.
- **503 `E_UNAVAILABLE` en una operación que escribe** (`mkdir`, `rmdir`, `rm`, `POST /admin/users`, el commit) puede querer decir que etcd sí la aplicó y la respuesta se perdió (sección 11). Al reintentar, una respuesta como 409 `E_EXISTS` en `mkdir` o 404 en `rm` puede ser el resultado del primer intento: el cliente lo confirma con `stat` o `ls`.

Errores que genera el propio framework, convertidos al mismo formato:

- Parámetros, cuerpo o ruta de archivo inválidos: 400 `E_BAD_REQUEST` (no el 422 que FastAPI usa por defecto), con el campo que falló en `message`.
- Ruta HTTP inexistente: 404 `E_NOT_FOUND`.
- Método HTTP no permitido: 400 `E_BAD_REQUEST` (no 405, que no está en la tabla).
- Cualquier excepción no prevista: 500 `E_INTERNAL`, con el mensaje genérico de la tabla para no mostrar detalles internos. Se escribe en el log del servicio. No aparece en la tabla de cada ruta porque cualquiera puede darlo.

## 7. Formatos

### 7.1 JWT (DA-10)

HS256, firmado con la clave del archivo `DFS_JWT_SECRET_FILE`. Dura `DFS_JWT_TTL_S` (3600 s). Viaja en `Authorization: Bearer <JWT>` hacia el MetaServer público. Contenido (`JwtClaims`):

```json
{
  "sub": "ana",
  "admin": false,
  "iat": 1793628000,
  "exp": 1793631600
}
```

| Claim | Significado |
|---|---|
| `sub` | usuario dueño del token; define su espacio de nombres |
| `admin` | `true` solo para administradores (rutas `/admin/*`) |
| `iat` | emitido en, segundos desde epoch (el estándar JWT lo exige en segundos) |
| `exp` | vence en, segundos desde epoch (`iat + DFS_JWT_TTL_S`) |

### 7.2 Pase de bloque (DA-10)

El MetaServer lo entrega por bloque en `GET /files/blocks` (lectura), `POST /uploads` y `realloc` (escritura). El DataNode lo verifica sin consultar al MetaServer. Contenido (`BlockPass`), de lectura y de escritura:

```json
{
  "user": "ana",
  "hash": "6ce5a55b446b43e4e935c44695a9a09f161e8e87d1ff2359adebc5f9ee5ee5cf",
  "op": "r",
  "exp": 1793628720
}
```

```json
{
  "user": "ana",
  "hash": "b64bb9046f61749097db060781bcbbba317c08ff0ac378e9aed9a6d18217e449",
  "op": "w",
  "secondaries": [
    "172.31.16.20:9444",
    "172.31.0.10:9444"
  ],
  "exp": 1793628600
}
```

- `op`: `r` permite `GET /blocks/{hash}`; `w` permite `PUT /blocks/{hash}`.
- `secondaries`: `ip:puerto` internos de los secundarios: 2, o 1 si solo quedan 2 DataNodes vivos en zonas distintas (DA-05). Solo va en los pases de escritura, así el cliente no puede cambiar a dónde se replica el bloque.
- `exp`: segundos desde epoch, como en el JWT; el pase dura `DFS_PASS_TTL_S` (600 s).

Codificación: `base64url(json) + "." + base64url(hmac_sha256(clave, json))`, sin relleno `=`, donde `clave` es el contenido de `DFS_PASS_KEY_FILE` y `json` son exactamente los bytes codificados en la primera parte (el DataNode no vuelve a serializar). Viaja en `Authorization: Bearer <pase>`. El DataNode lo comprueba en este orden y se detiene en el primer fallo: formato y firma, contenido válido, vencimiento, `hash` de la ruta y `op` del método (`src/dfs_common/security.py`). Por ese orden, un pase vencido de otro bloque da 401 y no 403. `user` dice en qué carpeta (`/data/{user}/`) va el bloque.

- Sin pase, o con un pase de firma válida pero vencido: 401 `E_AUTH`, igual que un JWT vencido.
- Pase mal formado, con firma inválida, con otro `hash` que el de la ruta o con la operación equivocada: 403 `E_FORBIDDEN`.

Una transferencia larga puede durar más que el pase. Para pedir pases nuevos:

- **escritura:** `POST /uploads/{upload_id}/realloc` con los mismos índices y `exclude_nodes: []`;
- **lectura:** `GET /files/blocks` con la misma `version`, para seguir leyendo la versión que se abrió (DA-14).

### 7.3 Órdenes en la respuesta del latido (DA-02, DA-12)

El líder guarda las órdenes en `/orders/{node_id}/{order_id}`; así un cambio de líder no las pierde y un DataNode caído las recibe al volver. El DataNode las recibe en la respuesta a su latido y devuelve los `order_id` cumplidos en `done_orders` del siguiente.

**`copy`**: el DataNode que tiene el bloque lo envía con `PUT /internal/blocks/{user}/{hash}` a `target.address` (puerto interno del destino). Solo reporta la orden en `done_orders` si el destino respondió 201; entonces el MetaServer agrega `target.node_id` a `stored_on`. Se usa para reponer copias (re-réplica).

**Cómo genera el líder las órdenes `copy`** (DA-04, DA-12). Cada `DFS_LEADER_SCAN_S` el líder revisa `/nodes/dn/` y `/blocks/`:

- Un DataNode cuenta como caído desde que el líder ve que su clave en `/nodes/dn/` desapareció (venció su lease). El líder lo recuerda en memoria; un líder nuevo empieza a contar desde que gana la elección y no ordena copias antes de `DFS_LEADER_GRACE_S`.
- Para decidir si un bloque necesita copia cuentan las de `stored_on` en nodos vivos o caídos hace menos de `DFS_REREPLICATE_AFTER_S`. Si son menos de 3 y el bloque no tiene ya una orden `copy` pendiente, el líder crea una: origen, un nodo vivo con copia; destino, un nodo vivo sin el bloque y con espacio, de preferencia en una zona que el bloque todavía no cubre. Así también repone la tercera copia de los bloques que se confirmaron con 2.
- Una `copy` que falla se reintenta sola: la orden sigue en `/orders/` y vuelve en cada latido. Si el origen o el destino dejan de estar vivos, o el origen ya no está en `stored_on`, el líder borra la orden en su siguiente revisión y crea otra con nodos vivos.
- Si el origen no tiene el bloque, o lo tiene dañado, no reporta la orden como cumplida: lo informa en `bad_blocks`, el MetaServer lo quita de `stored_on` y el líder elige otro origen.

```json
{
  "order_id": "b7e3c2a1-4d5f-4a6b-8c9d-0e1f2a3b4c5d",
  "type": "copy",
  "user": "ana",
  "hash": "6ce5a55b446b43e4e935c44695a9a09f161e8e87d1ff2359adebc5f9ee5ee5cf",
  "target": {
    "node_id": "dn1",
    "address": "172.31.0.10:9444"
  }
}
```

**`delete`**: borra el bloque solo si lo escribió antes de `written_before_ms` (ms desde epoch). La limpieza la usa para un bloque que ya sacó de etcd: el MetaServer le restó a T el margen `DFS_GC_CLOCK_MARGIN_S` por la diferencia de relojes (DA-02, Figura 1).

También borra las **copias que sobran** (DA-12). Cuando un DataNode vuelve y un bloque queda con más de 3 copias vivas, el líder quita de `stored_on` el nodo que volvió y le envía una orden `delete` con `written_before_ms` igual al instante en que lo quitó, menos `DFS_GC_CLOCK_MARGIN_S`. Así, si después ese nodo recibe una copia nueva del bloque, esa copia no se borra.

```json
{
  "order_id": "d9a5e4c3-6f7b-4c8d-8e1f-2a3b4c5d6e7f",
  "type": "delete",
  "user": "ana",
  "hash": "29124d08ab194c99b65e04622016f3306c6c39ce331241c0e089b10e66411607",
  "written_before_ms": 1793628599000
}
```

## 8. Claves de etcd (DA-01, DA-02, DA-11, DA-12)

Solo los MetaServers hablan con etcd, por la pasarela HTTP/JSON (`/v3/kv`, `/v3/lease`, `/v3/lock`, `/v3/election`), donde claves y valores viajan en base64. Se conectan a los nodos de `DFS_ETCD_ENDPOINTS` (`https://ip:2379`) por mTLS, con el mismo certificado de nodo (`DFS_TLS_CERT_FILE`, `DFS_TLS_KEY_FILE`) y la misma autoridad certificadora (`DFS_TLS_CA_FILE`) de las demás conexiones internas. Los valores son JSON en UTF-8 y los instantes, milisegundos desde epoch (UTC).

| Clave | Valor | Notas |
|---|---|---|
| `/users/{user}` | `{password_hash, is_admin}` | `password_hash` en formato Argon2id (DA-10); el primer `admin` lo crea el MetaServer al arrancar (sección 2) |
| `/ns/{user}{path}` | directorio: `{type: "dir", created_at}`; archivo: `{type: "file", file_id, version}` | una por archivo o directorio; `version` es la actual; la raíz `/` no se guarda |
| `/files/{file_id}/v/{n}` | `{user, size, block_size, hashes, file_sha256, created_at, replaced_at}` | manifiesto de la versión `n`; `file_sha256` es `null` si la versión se publicó con `a` o `rw`; `replaced_at` es `null` mientras es la actual y se llena al reemplazarla o al borrar el archivo; desde ahí corre `DFS_RETENTION_S` (DA-14). `user` permite que la limpieza (DA-02) recorra `/files/` y sepa qué `/blocks/{user}/{hash}` protege cada versión retenida, también después de `rm`, cuando ya no hay entrada en `/ns/` |
| `/blocks/{user}/{hash}` | `{size, stored_on, touched_at}` | `stored_on`: `node_id` con copia; `touched_at` lo actualiza cada reserva que reutiliza el bloque (DA-02) |
| `/nodes/dn/{node_id}` | `{ip, public_port, internal_port, zone, free_bytes}` | atada al lease de latidos (`DFS_NODE_LEASE_S`); `ip` es el `DFS_NODE_IP` del DataNode; `/internal/blocks/confirm` lee aquí la zona |
| `/nodes/meta/{meta_id}` | `{ip, public_port, internal_port}` | atada a un lease; `ip` es el `DFS_NODE_IP` del MetaServer; es lo que devuelve `GET /cluster/metaservers` |
| `/nodes/disk/{node_id}` | `{disk_id}` | sin lease: sobrevive a la caída del nodo; `POST /internal/register` la compara (sección 3) |
| `/orders/{node_id}/{order_id}` | la orden (sección 7.3) | se borra cuando llega en `done_orders` |
| `/uploads/{upload_id}` | `{user, path, mode, base_version, block_size, lock_key, blocks, created_at}` | atada al lease del lock de la subida: desaparece sola si el lock vence. `blocks` son los `[{index, hash, size}]` de la reserva: `realloc` lee aquí el hash de cada índice (sección 13) |
| `/locks/{user}{path}` | — | nombre del lock de etcd (DA-08); la clave de cada intento es `/locks/{user}{path}/{lock_id}`, con `lock_id` = ID del lease en hexadecimal; `rm` también lo toma. Los intentos sobre rutas que están debajo de `path` quedan en la misma fila (sección 11) |
| `/election/leader` | — | prefijo que usa el servicio de elección de etcd (DA-12) |

**Crear y borrar sin carreras.** Dos transacciones de etcd evitan que un `mkdir` dentro de un directorio y el `rmdir` de ese directorio se crucen: el que llega segundo ve el cambio y falla.

- **Crear una entrada** (`mkdir`, o el commit con `base_version: 0`): la transacción exige que la clave no exista (`create_revision = 0`) y que la `mod_revision` del padre siga siendo la que se leyó. La raíz no tiene clave y no se compara.
- **`rmdir`:** la transacción exige que la `mod_revision` del directorio no haya cambiado y que no haya claves bajo `/ns/{user}{path}/`. Esto último es una comparación de rango con `create_revision = 0`, que es verdadera solo si el rango está vacío.

**Transacción del commit** (DA-08). Antes, el MetaServer lee la entrada `/ns/{user}{path}`, el manifiesto base y las claves `/blocks/` de `hashes`, y valida lo de la sección 2 (copias vivas en 2 zonas para los bloques que no están en la versión base, sin huecos ni truncado). Las claves de bloque no entran en la comparación, por tres razones. La limpieza no las borra en esa ventana: la reserva actualizó su `touched_at` y la limpieza espera `DFS_GC_MIN_AGE_S` (DA-02). Si un DataNode cae, o reporta la copia en `bad_blocks`, entre la validación y la transacción, es lo mismo que si ocurriera justo después del commit: G1 se cumple en el momento de confirmar y la copia perdida la repone la re-réplica (G5). Y comparar cada bloque haría fallar el commit cuando otra subida reutiliza el mismo bloque y, en archivos de 1 GiB o más (128 bloques, DA-03), superaría el límite de etcd de 128 operaciones por transacción, que también cuenta las comparaciones (`--max-txn-ops`). La transacción:

- compara que exista la clave del lock `/locks/{user}{path}/{lock_id}` y, si `base_version` es mayor que 0, que la `mod_revision` de `/ns/{user}{path}` sea la que se leyó; si `base_version` es 0, aplica la regla de creación de arriba;
- si todo se cumple, escribe en un solo paso el manifiesto nuevo `/files/{file_id}/v/{n}` con `replaced_at: null`, `/ns/{user}{path}` con la versión `n` y el `replaced_at` del manifiesto anterior, si lo hay, y borra `/uploads/{upload_id}`. Un archivo nuevo recibe un `file_id` nuevo: un archivo borrado y vuelto a crear tiene otro;
- si algo falla no escribe nada, y el MetaServer responde según la comparación que falló: lock, 409 `E_LOCK_LOST`; versión base o clave que ya existe, 409 `E_VERSION_CONFLICT`; padre que cambió, 404 `E_NOT_FOUND`.

**Transacción de `rm`.** Con el lock tomado (espera 0), compara que la `mod_revision` de `/ns/{user}{path}` sea la que se leyó; borra la entrada y llena el `replaced_at` del manifiesto actual, que queda retenido `DFS_RETENTION_S` (DA-14).

**Riesgo conocido: confianza entre servidores.** etcd exige un certificado de cliente firmado por la CA propia (`ETCD_CLIENT_CERT_AUTH`), pero con una sola CA (tarea 02, decisión 13) acepta también el de un DataNode; lo mismo pasa en las APIs internas, donde entra cualquier certificado de la CA. Hoy, que solo los MetaServers hablen con etcd (DA-01) depende de que los DataNodes no tengan código para hacerlo. El riesgo nuevo es acotado: un DataNode comprometido ya tiene la clave HMAC de los pases y podría falsificarlos, porque el modelo de DA-10 confía en los servidores. Lo que sí se agregó es la comprobación de la IP de origen en la API interna del MetaServer (sección 3). Para etcd, la mitigación es activar usuarios y roles tomando como usuario el CN del certificado (`meta1`, `meta2`, `meta3`); queda para la tarea de despliegue (DA-15), donde se puede probar con etcd corriendo.

## 9. Variables de entorno (DA-16)

Las lee `src/dfs_common/config.py`; `.env.example` trae los valores normales y, en comentarios, los de prueba. "Pruebas" es el valor de la Tabla 19 de `docs/decisiones.md` cuando es un tiempo o umbral. Una variable vacía toma el valor por defecto. La Tabla 19 es copia del documento oficial y no se edita: `DFS_LOCK_RENEW_S` y `DFS_GC_CLOCK_MARGIN_S` (tarea 02) y `DFS_LEADER_SCAN_S` (tarea 02d) se agregaron después y están solo aquí y en `.env.example`.

Aquí van los tiempos y umbrales del comportamiento del sistema (latidos, leases, re-réplica, limpieza, retención). Los límites de validación de la API, como el rango de `wait_s` o el largo de las rutas, son parte del contrato y van fijos en los modelos.

Al arrancar, `python -m metaserver` exige `DFS_NODE_ID`, `DFS_NODE_IP` y `DFS_ETCD_ENDPOINTS`, y `python -m datanode` exige `DFS_NODE_ID`, `DFS_NODE_IP`, `DFS_ZONE` y `DFS_META_SEEDS`; si falta alguna, el proceso termina con un mensaje que las nombra (`Settings.require`).

| Variable | Normal | Pruebas | Qué controla |
|---|---|---|---|
| `DFS_BLOCK_MIB` | 8 | 8 | tamaño de bloque en MiB (DA-03) |
| `DFS_W` | 2 | 2 o 3 | copias para confirmar un bloque (DA-04) |
| `DFS_PRIMARY_WAIT_S` | 10 | 10 | espera del primario por los secundarios (DA-04) |
| `DFS_LOCK_LEASE_S` | 30 | 30 | lease de cada intento de lock (DA-08) |
| `DFS_HEARTBEAT_S` | 5 | 5 | intervalo de latido de los DataNodes (DA-12) |
| `DFS_NODE_LEASE_S` | 15 | 15 | vencimiento de un nodo sin latidos (DA-12) |
| `DFS_REREPLICATE_AFTER_S` | 60 | 60 | espera antes de re-replicar (DA-12) |
| `DFS_LEADER_GRACE_S` | 300 | 60 | espera del líder nuevo antes de re-replicar (DA-12) |
| `DFS_RETENTION_S` | 600 | 60 | retención de la versión anterior (DA-14) |
| `DFS_GC_INTERVAL_S` | 600 | 30 | cada cuánto corre la limpieza (DA-02) |
| `DFS_GC_MIN_AGE_S` | 3600 | 120 | tiempo sin cambios para ser candidato a limpieza (DA-02) |
| `DFS_LOCK_RENEW_S` | 10 | 10 | cada cuánto renueva el cliente su lock (DA-08) |
| `DFS_GC_CLOCK_MARGIN_S` | 1 | 1 | margen por diferencia de relojes en la limpieza (DA-02) |
| `DFS_LEADER_SCAN_S` | 10 | 10 | cada cuánto revisa el líder nodos caídos, copias faltantes y órdenes atascadas (DA-12) |
| `DFS_JWT_TTL_S` | 3600 | 3600 | duración del JWT (DA-10) |
| `DFS_PASS_TTL_S` | 600 | 600 | duración del pase de bloque (DA-10) |
| `DFS_META_PUBLIC_PORT` | 8443 | — | MetaServer, API pública (DA-13) |
| `DFS_META_INTERNAL_PORT` | 8444 | — | MetaServer, API interna (DA-13) |
| `DFS_DN_PUBLIC_PORT` | 9443 | — | DataNode, API pública (DA-13) |
| `DFS_DN_INTERNAL_PORT` | 9444 | — | DataNode, API interna (DA-13) |
| `DFS_META_SEEDS` | — | — | IP de los 3 MetaServers, sin puerto, separadas por coma; las escribe el despliegue. Con Route 53, un nombre DNS en su lugar (DA-11) |
| `DFS_ETCD_ENDPOINTS` | — | — | `https://ip:2379` de los 3 nodos de etcd, separados por coma; solo el MetaServer |
| `DFS_ZONE` | — | — | zona de este nodo o cliente; el cliente la manda en `X-Client-Zone` |
| `DFS_NODE_ID` | — | — | identificador del nodo: `meta1`..`meta3` o `dn1`..`dn4` |
| `DFS_NODE_IP` | — | — | IP privada de este nodo, sin valor por defecto; la escribe el despliegue. La usan el DataNode al registrarse y el MetaServer en `/nodes/meta/` (DA-11) |
| `DFS_DATA_DIR` | `/data` | — | carpeta de bloques del DataNode (DA-02) |
| `DFS_TLS_CA_FILE` | `/run/secrets/ca.crt` | — | certificado de la CA propia |
| `DFS_TLS_CERT_FILE` | `/run/secrets/tls.crt` | — | certificado de este nodo; también para etcd |
| `DFS_TLS_KEY_FILE` | `/run/secrets/tls.key` | — | llave privada de ese certificado |
| `DFS_JWT_SECRET_FILE` | `/run/secrets/jwt_secret` | — | clave HS256 del JWT (DA-10) |
| `DFS_PASS_KEY_FILE` | `/run/secrets/pass_key` | — | clave HMAC de los pases (DA-10) |
| `DFS_NODE_KEY_FILE` | `/run/secrets/node_key` | — | clave AES-256 del DataNode (DA-09) |
| `DFS_ADMIN_PASSWORD_FILE` | vacía | — | contraseña inicial del usuario `admin`; vacía = no se crea (sección 2) |

## 10. Decisiones de las tareas 02c y 02d

Con estas decisiones el contrato queda sin puntos abiertos.

### Tarea 02c

1. **Zonas al confirmar un bloque:** las comprueba `POST /internal/blocks/confirm`, que responde 422 `E_REPLICAS` si no hay 2 zonas (sección 3).
2. **Tamaño de bloque:** `block_size` en `POST /uploads`, y 400 si un `PUT` de bloque pasa de `DFS_BLOCK_MIB` MiB (secciones 2, 4 y 5).
3. **Pase vencido:** 401 `E_AUTH`, y pases nuevos con `realloc` o `GET /files/blocks` (secciones 4, 6 y 7.2).
4. **Copias que sobran:** el líder quita el nodo de `stored_on` y le envía una orden `delete` (sección 7.3).
5. **Errores inesperados:** 500 `E_INTERNAL` en las cuatro apps; `DFSError` es la única excepción con código (sección 6).
6. **Tipo equivocado o sin padre:** 400 sobre directorios, 404 sin padre o con un archivo como padre, y `a`/`rw` crean el archivo si no existe (secciones 1 y 2).
7. **Crear y borrar sin carreras:** transacciones con `create_revision` y `mod_revision` (sección 8).
8. **IP del nodo:** `DFS_NODE_IP` (secciones 3, 8 y 9).

### Tarea 02d: correcciones de la auditoría

Los puntos 10 a 12, y los ajustes a los puntos 2, 4 y 9, salen de revisar una segunda auditoría hecha sobre esta tarea.

1. **Quién valida la escritura:** el cliente, los offsets; el MetaServer, en la reserva los índices y tamaños, y en el commit los huecos y el truncado (secciones 1 y 2).
2. **Copias vivas:** `present`, el commit y `/internal/blocks/confirm` cuentan solo copias en DataNodes vivos; `GET /files/blocks` entrega `stored_on` completo y solo réplicas vivas (vacía si no queda ninguna), y 404 pasada la retención (secciones 1 y 2).
3. **DataNodes suficientes:** al menos `DFS_W` vivos que cubran 2 zonas; si no, 503 (sección 2).
4. **Copias fantasma:** un bloque que no está se reporta en `bad_blocks`, y `disk_id` (en `DFS_DATA_DIR/.disk_id`) detecta un disco nuevo; primero se limpia y después se guarda (secciones 3, 4 y 8).
5. **Órdenes `copy` del líder:** se revisan cada `DFS_LEADER_SCAN_S`, se reintentan y se reasignan (secciones 7.3 y 9).
6. **Lock y transacciones:** formato de la clave del lock, y transacciones del commit y de `rm` (secciones 2 y 8).
7. **Orden de comprobación del pase:** el del código (sección 7.2).
8. **Arranque:** cada servicio exige sus variables de identidad (sección 9).
9. **Riesgo conocido:** etcd y las APIs internas aceptan cualquier certificado de la CA propia (sección 8).
10. **Identidad en la API interna del MetaServer:** la IP de origen debe ser la del `node_id`; si no, 403 (secciones 3 y 6).
11. **Retención después de `rm`:** el manifiesto guarda `user` para que la limpieza proteja sus bloques; el lector termina con los pases que tiene (secciones 2 y 8).
12. **Qué es `DFS_W`:** cuántas copias espera el primario; el commit exige siempre 2 en 2 zonas (sección 2).

## 11. Decisiones de las tareas 06 a 08

Las tareas 06 (cliente de etcd, `src/metaserver/etcd.py`), 07 (login, usuarios y primer administrador) y 08 (`ls`, `stat`, `mkdir`, `rmdir` y `rm`) no cambian rutas, mensajes ni errores. Precisan cómo se comportan:

1. **401 antes que 400** en las rutas con JWT (sección 1, "Orden de las comprobaciones").
2. **Reintentos contra etcd.** El MetaServer prueba los nodos de `DFS_ETCD_ENDPOINTS` en orden y empieza por el último que respondió. Pasa al siguiente:
   - si la petición no llegó a enviarse;
   - si etcd responde `Unavailable` o `DeadlineExceeded`.

   Una **transacción** solo se reenvía si es seguro que etcd no la aplicó: no llegó a enviarse, o etcd respondió "no leader". Si se envió y no se sabe si se aplicó (se perdió la respuesta, "request timed out", "leader changed"), la ruta responde 503 `E_UNAVAILABLE` y no la repite: repetida, podría responder 409 o 423 a algo que sí se aplicó (sección 6). Las lecturas, y lo que repetido da lo mismo (`put`, borrar, leases), sí se reenvían. Si ningún nodo responde, 503.
3. **`lock_id` mal formado.** Un `lock_id` que no tiene exactamente la forma que entrega `POST /locks` (hexadecimal en minúsculas, sin ceros a la izquierda, entre 1 y 16 cifras) no nombra ningún lock: se trata como un lock que ya no existe. `PUT /locks/{lock_id}` responde 409 `E_LOCK_LOST`, `DELETE /locks/{lock_id}` responde 200, y en la reserva y el commit responde 409 `E_LOCK_LOST`. Por eso esas rutas no tienen 400.
4. **Locks de rutas anidadas.** Como la clave de cada intento es `/locks/{user}{path}/{lock_id}` (sección 8), los intentos sobre una ruta que está debajo de `path` (por ejemplo, `/a/b` debajo de `/a`) quedan en la misma fila que el lock de `path`.
   - Nunca hay dos dueños del mismo archivo, ni cruces entre usuarios.
   - No pasa con directorios: `POST /locks` y `rm` sobre un directorio responden 400 antes de tomar el lock.
   - Solo pasa cuando un mismo usuario trabaja con el archivo `/a` y a la vez intenta crear algo debajo de él. Eso nunca puede salir bien, porque debajo de un archivo no se puede publicar (404). El efecto es una espera o un 423 de más en esa operación.

   Se acepta así y no se cambia el formato de la clave.
5. **Respuesta de `mkdir` y `rmdir` cuando su transacción falla.** Se vuelve a leer una vez la clave que la transacción comparaba por `mod_revision` (el padre en `mkdir`, el directorio en `rmdir`).
   - Si sigue igual, falló la otra comparación: en `mkdir`, la ruta ya existía, así que 409 `E_EXISTS`; en `rmdir`, el directorio tenía entradas, así que 409 `E_NOT_EMPTY`. Vale aunque, al releer, la ruta ya no exista o las entradas ya no estén.
   - Si cambió o ya no está, 404 `E_NOT_FOUND`.

   En `mkdir` con la raíz como padre, la raíz no se compara, así que una transacción fallida es siempre 409.
6. **`rm` y su lock.** El lease del lock de `rm` se revoca siempre al terminar. Si la revocación falla, `rm` responde lo mismo que habría respondido (200 si borró, 423 si no pudo) y el lease vence solo en `DFS_LOCK_LEASE_S`.
7. **Primer administrador** (sección 2):
   - lo crea un solo MetaServer, aunque arranquen los tres a la vez;
   - si `admin` ya existe, no se cambia su contraseña aunque `DFS_ADMIN_PASSWORD_FILE` traiga otra: el MetaServer lo avisa en su log (pasa si se regeneran los secretos y etcd conserva sus datos);
   - si el archivo de la contraseña está vacío, el MetaServer no arranca.
8. **Arranque y etcd.** Si etcd no responde mientras se crea el primer administrador, el proceso termina y Docker lo reinicia (`restart: unless-stopped`); cuando etcd vuelve, arranca solo. Sin `DFS_ADMIN_PASSWORD_FILE`, el MetaServer arranca sin consultar etcd, y sus rutas responden 503 mientras etcd no esté disponible.

## 12. Decisiones de las tareas 09 y 10

Las tareas 09 (almacén de bloques, `src/datanode/storage.py`) y 10 (verificación de pases, `src/datanode/auth.py`) no cambian rutas, mensajes ni errores. Precisan cómo se comportan:

1. **Archivo en disco.** `{DFS_DATA_DIR}/{user}/{hash}` con `nonce (12 bytes) + texto cifrado + tag (16 bytes)`: AES-256-GCM con la clave de `DFS_NODE_KEY_FILE` y `"{user}/{hash}"` como dato asociado. Un archivo copiado con otro nombre o a la carpeta de otro usuario no se descifra y da 422. El bloque se escribe por partes en `{hash}.tmp-{uuid}`, en la misma carpeta, y se publica con `fsync` y `os.replace`. Los temporales que deja una caída se borran al arrancar.
2. **Siempre se reescribe.** Un bloque que ya existe se vuelve a escribir, con un nonce nuevo. Su fecha de modificación es el instante de escritura que compara la orden `delete` (sección 7.3), así que una copia subida de nuevo sobrevive a una orden que llegue tarde.
3. **Bloques faltantes o dañados.** `GET /blocks/{hash}` anota en los `bad_blocks` del siguiente latido tanto la copia dañada (422, y la borra) como la que no está (404), sin repetir bloques (sección 4).
4. **Copia dañada que se reescribe a la vez.** Reemplazar y borrar un bloque pasan bajo un mismo candado, y la lectura solo borra la copia dañada si sigue siendo la que leyó: si un `PUT` la reemplazó mientras tanto, la copia nueva se queda. La orden `delete` también se aplica bajo ese candado.
5. **400 antes que 401 en el DataNode público.** A diferencia de las rutas con JWT del MetaServer (sección 11, punto 1), un hash inválido en la ruta da 400 antes de mirar el pase. Después, el pase se comprueba en el orden de la sección 7.2. Sin el encabezado `Authorization`, o si no es `Bearer`: 401 `E_AUTH`.
6. **Arranque.** Las APIs pública e interna del DataNode corren en el mismo proceso y comparten el almacén. Al arrancar leen `DFS_NODE_KEY_FILE` (32 bytes) y `DFS_PASS_KEY_FILE`; si falta alguno, el proceso termina y Docker lo reinicia.

## 13. Cambios y decisiones de la tarea 12

La tarea 12 (subidas, confirmación y lectura del manifiesto: `src/metaserver/uploads.py`, `blocks.py`, `placement.py` y `files.py`) cambia dos puntos del contrato y precisa cómo se comportan sus rutas.

**Cambios de contrato:**

1. **`/uploads/{upload_id}` guarda los bloques de la reserva** (`blocks: [{index, hash, size}]`, sección 8): `realloc` necesita el hash de cada índice.
2. **`file_sha256` es opcional (`null`)** en el commit y en el manifiesto (secciones 2 y 8). `put` y `open` en `w` lo envían; `a` y `rw` envían `null`, porque calcularlo obligaría al cliente a descargar el archivo entero para cambiar un solo bloque. La integridad no se pierde: cada bloque se verifica por su hash, y el orden de los bloques está en etcd. `get` compara el hash del archivo solo cuando existe.

**Decisiones:**

1. **Copias vivas en el commit.** La regla de 2 zonas vivas (G1) se aplica a todo bloque de `hashes` que no está en la lista `hashes` del manifiesto de la versión base, venga o no en `blocks` de la subida; con `base_version` 0 se aplica a todos. Los bloques que ya estaban en la versión base solo necesitan que su clave `/blocks/` exista. G1 habla de "cada bloque nuevo", y la prueba "Fallos: zona b" pide seguir escribiendo con la zona b caída: si se exigiera la regla a los bloques viejos, no se podría anexar a un archivo hasta que el líder repusiera sus copias.
2. **Orden de las comprobaciones del commit.** Lock (409 o 403); la subida (404, 403, o 409 si su ruta no es la del lock); la versión base (409); el padre, si el archivo es nuevo (404); que cada bloque de `hashes` tenga su clave (422); la forma del archivo: cantidad y tamaño de los bloques, sin truncado con `a` y `rw` y sin cambiar lo que ya estaba con `a` (400); y las 2 zonas vivas de los bloques que no están en la versión base (422). Si la transacción falla, se vuelve a leer: sin la clave del lock, 409 `E_LOCK_LOST`; con el padre cambiado o borrado, 404; si no, 409 `E_VERSION_CONFLICT`.
3. **Ubicación de los bloques (DA-05).** Con los DataNodes vivos ordenados por `node_id`, sin los de `exclude_nodes` ni los que tienen menos espacio libre que `block_size`, se toman 3 por turnos desde la posición `(start + index) mod N`. Si no cubren 2 zonas, el tercero se cambia por el siguiente nodo de otra zona. Si solo quedan 2 nodos en zonas distintas, se usan esos 2. Hacen falta al menos `DFS_W` nodos: si no se logran, 507 `E_NO_SPACE` cuando el motivo es el espacio libre, y 503 `E_UNAVAILABLE` en otro caso. `start` es aleatorio, uno por reserva y uno por cada `realloc`, porque la subida no guarda el anterior.
4. **`realloc` no pide el lock.** La subida está atada al lease del lock: si el lock vence, la subida desaparece y `realloc` responde 404.
5. **`/internal/blocks/confirm`** lee la clave del bloque y la escribe con una transacción que compara la `mod_revision` leída (o que la clave no exista); si otra escritura se cruza, lo intenta hasta 3 veces y después responde 503 `E_UNAVAILABLE`. Deja `touched_at` en el instante de la confirmación. Las mismas transacciones y reintentos se usan para actualizar `stored_on` en los latidos (tarea 13) y para el `touched_at` de los bloques presentes en una reserva.
6. **`GET /files/blocks`.** Las réplicas van primero las de la zona de `X-Client-Zone` y, dentro de cada grupo, en el orden de `stored_on`. El tamaño de cada bloque sale del manifiesto (`size` y `block_size`). Una versión anterior se entrega mientras su `replaced_at` tenga menos de `DFS_RETENTION_S`. Después de `rm`, la ruta responde 404 aunque el manifiesto siga retenido: el lector termina con los pases que tiene (sección 2).
