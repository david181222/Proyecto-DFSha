# Tareas 11 a 14 · Locks, subidas, registro de nodos y réplicas en el DataNode

**Quién:** Edwin. **Etapa:** 2. **Antes de empezar:** la etapa 1 completa en `main`, con los ajustes de la auditoría de las tareas 09 y 10.

Estas cuatro tareas terminan el plano de control de la escritura y el reenvío de réplicas. José (tareas 15 a 17) y Santiago (tareas 18 a 20) construyen encima, así que los nombres que fija este prompt no se cambian.

Orden: 11 → 12 → 13 → 14.

---

## Flujo de trabajo (igual en cada tarea)

**Antes de cada tarea (lo hace Edwin, no Claude Code):**

```bash
git switch main
git pull
git switch -c t11-locks    # luego t12-subidas, t13-nodos, t14-replicas
```

**Durante la tarea (lo hace Claude Code):**

1. Lee lo indicado en "Lee primero" y el código que ya existe. Si un nombre de este prompt no coincide con el del código, usa el del código y avísame.
2. Antes de escribir código, muéstrame un plan corto: los archivos que vas a tocar, las funciones nuevas y las pruebas. **Espera mi OK.**
3. Escribe primero las pruebas del comportamiento que pide el contrato y después el código. Avanza en pasos pequeños y corre `pytest` en el camino.
4. Al terminar:
   - corre `pytest` completo;
   - corre `python scripts/export_openapi.py` y revisa que `git diff docs/api` salga vacío, salvo en la tarea que cambia el contrato;
   - repasa las reglas de `CLAUDE.md`.
5. Detente y muéstrame el resultado de pytest, los archivos cambiados y los pendientes. No hagas commit.

**Después de la tarea (lo hace Edwin):**

1. Revisa el diff.
2. `git add -A`, `git commit -m "Tarea 11: locks"` y `git push -u origin t11-locks`.
3. Abre el PR y pide que lo revise José o Santiago (tarea 21).
4. Cuando se una a `main`, avisa al equipo.

**Reglas que valen para las cuatro tareas:**

- **Solo lo de la tarea.** Si hace falta cambiar otra pieza y este prompt no lo pide, anótalo en Pendientes.
- **Un cambio de contrato va completo en el mismo cambio:** el modelo, `docs/contratos.md`, `docs/api/*.json` y su prueba en `tests/test_contratos.py`. Avísame en el informe.
- **Pruebas con `FakeEtcd`** (`tests/fakes.py`). Si le falta algo que necesitas, agrégalo con su prueba, igual que en `Etcd`.
- **Nada se arranca al importar.** Secretos, clientes HTTP y tareas de fondo viven en el *lifespan*. Las tareas de fondo de un proceso van en el *lifespan* de la app pública, para que corran una sola vez por proceso.
- **Errores siempre con `DFSError`** y los códigos del contrato.
- **Tiempos solo de `config.py`.** Un número de reintentos no es un tiempo y puede ir fijo: como máximo 3.
- **Lógica separada de la entrada y salida.** Las decisiones (a qué nodos va un bloque, si un commit es válido) van en funciones puras que reciben datos y devuelven datos. Las rutas solo leen etcd, llaman a esas funciones y escriben. Así se prueban fácil y se explican fácil en la defensa.

## Lee primero

1. `CLAUDE.md`.
2. `docs/decisiones.md`: DA-02, DA-04, DA-05, DA-08, DA-10, DA-11, DA-12 y DA-16.
3. `docs/contratos.md`: secciones 1 (con "Cómo se encadenan las rutas"), 2, 3, 4, 6, 7, 8 y 10.
4. El código de la etapa 1:
   - `src/dfs_common/`;
   - `src/metaserver/`, sobre todo `etcd.py` y `auth.py`;
   - `src/datanode/`, sobre todo `storage.py` y `auth.py`;
   - `tests/fakes.py`.

---

## Tarea 11 · Locks: pedir, renovar y liberar (MetaServer)

Rutas: `POST /locks`, `PUT /locks/{lock_id}` y `DELETE /locks/{lock_id}` (sección 2).

### Decisiones ya tomadas

- **Qué es `lock_id`:** el ID del lease del intento, en hexadecimal (`format(lease_id, "x")`). Es el mismo número que va en la clave del lock (tarea 06).
- **Saber de quién es un lock sin agregar claves.** Agrega a `Etcd` y a `FakeEtcd` el método `lease_keys(lease_id) -> list[str] | None`. Usa `POST /v3/lease/timetolive` con `"keys": true`, que devuelve las claves atadas al lease, o `None` si el lease no existe (TTL -1). La clave del lock tiene la forma `/locks/{user}{path}/{lease_hex}`, y de ahí salen el usuario y la ruta.
- **`resolve_lock(etcd, lock_id, user) -> LockInfo`**, en `src/metaserver/locks.py`. `LockInfo` trae `key`, `user` y `path`. La usan esta tarea y la 12.
  - `lock_id` que no es hexadecimal, lease inexistente, o lease sin clave de lock: `DFSError` `E_LOCK_LOST` (409).
  - Lock de otro usuario: `E_FORBIDDEN` (403).
- **`POST /locks`:**
  1. La raíz o un directorio responden 400 (decisión 6 de la 02c). Una ruta que no existe sí se puede bloquear, porque `w` la crea.
  2. Pide un lease de `DFS_LOCK_LEASE_S` y llama a `lock("/locks/{user}{path}", lease, wait_s=...)`.
  3. **Mientras espera en la fila, el MetaServer renueva ese lease cada `DFS_LOCK_RENEW_S`**, con una tarea que se cancela al terminar la espera. Motivo: la espera puede durar hasta 60 s y el lease dura 30; si vence, el intento pierde su puesto en la fila.
  4. Si `lock` devuelve `None`, revoca el lease y responde 423 `E_LOCKED`. Si lo obtiene, responde `{lock_id, lease_s}` con `lease_s = DFS_LOCK_LEASE_S`.
- **`PUT /locks/{lock_id}`:** `resolve_lock` y después `lease_keepalive`. Si el lease ya no existe: 409 `E_LOCK_LOST`.
- **`DELETE /locks/{lock_id}`:** es idempotente. Lease inexistente: 200 `{"ok": true}`. De otro usuario: 403. Si no, `lease_revoke` y 200.
- **Cliente que se va mientras espera:** si después el lock se obtiene, nadie lo renueva y vence solo en `DFS_LOCK_LEASE_S`. Es aceptable; déjalo dicho en un comentario.

### Pruebas

- Tomar, renovar y liberar. `DELETE` dos veces responde 200 las dos veces.
- Un segundo intento con `wait_s: 0`, mientras otro tiene el lock: 423, y su clave no queda en la fila.
- Un segundo intento con espera entra cuando el primero libera. Si `FakeEtcd` no puede simular la espera, cúbrelo en la prueba de integración con etcd real.
- La renovación durante la espera ocurre: cuenta los `keepalive` con un `DFS_LOCK_RENEW_S` pequeño.
- `lock_id` de otro usuario en `PUT` y `DELETE`: 403. Inexistente o basura en `PUT`: 409.
- Raíz y directorio: 400.
- `lease_keys`: con `MockTransport` (formato de la pasarela) y en la prueba de integración con etcd real.

---

## Tarea 12 · Subidas: reserva, realloc, confirmación, commit y lectura del manifiesto (MetaServer)

Rutas públicas: `POST /uploads`, `POST /uploads/{upload_id}/realloc`, `POST /uploads/{upload_id}/commit` y `GET /files/blocks`. Ruta interna: `POST /internal/blocks/confirm`.

`GET /files/blocks` y `confirm` no tenían dueño en la lista de tareas. Van aquí porque son la otra mitad de la subida.

### Cambios de contrato de esta tarea

Aplícalos primero y completos (modelo, `contratos.md`, OpenAPI y prueba):

1. **`/uploads/{upload_id}` guarda los bloques.** Su valor en etcd gana `blocks: [{index, hash, size}]`, porque realloc necesita saber el hash de cada índice.
2. **`file_sha256` pasa a ser opcional (`null`)**, en el commit y en el manifiesto.
   - `put` y `open` en `w` lo envían.
   - `a` y `rw` envían `null`: calcularlo obligaría al cliente a descargar el archivo entero para cambiar un solo bloque.
   - La integridad no se pierde: cada bloque se verifica por su hash, y el orden de los bloques está en etcd.
   - `get` compara el hash del archivo solo cuando existe.

### Decisiones ya tomadas

**Ubicación de réplicas (DA-05).** Es una función pura en `src/metaserver/placement.py`:

```python
choose_nodes(nodes, index, start, exclude, block_size) -> list[Node]
```

- `nodes` son los DataNodes vivos de `/nodes/dn/`, ordenados por `node_id`. Primero quita los de `exclude` y los que tienen `free_bytes` menor que `block_size`.
- Recorre la lista en círculo desde la posición `(start + index) mod N` y toma los 3 primeros. Si no cubren 2 zonas, cambia el tercero por el siguiente nodo de otra zona.
- Si solo quedan 2 nodos en zonas distintas, usa esos 2: un primario y un secundario.
- Si no se logran 2 zonas: 503 `E_UNAVAILABLE`. Si el motivo es que los nodos vivos no tienen espacio: 507 `E_NO_SPACE`.
- `start` es aleatorio, uno por reserva.

**`POST /uploads`**, en este orden:

1. La raíz o un directorio responden 400. Si el padre no existe o es un archivo: 404.
2. `resolve_lock`. Además, la ruta del lock debe ser la de la subida; si no, 409 `E_LOCK_LOST`.
3. Lee la versión actual (0 si el archivo no existe). Si `base_version` es distinta: 409 `E_VERSION_CONFLICT`.
4. Comprueba `block_size` según la decisión 2 de la 02c.
5. Comprueba cada bloque: índice mayor o igual a 0 y sin repetir, y `0 < size <= block_size`. Si falla, 400. Los huecos se detectan en el commit.
6. Para cada bloque:
   - si `/blocks/{user}/{hash}` existe, va a `present` y se actualiza su `touched_at`, para que la limpieza no lo borre (DA-02);
   - si no existe, `choose_nodes` y un pase `w` (`sign_pass`, con `exp = ahora + DFS_PASS_TTL_S`) que lleva las direcciones **internas** de los secundarios. Al cliente se le entrega la dirección **pública** del primario.
7. Guarda `/uploads/{upload_id}` (un uuid4) atado al lease del lock.

**`POST /uploads/{upload_id}/realloc`:**

- Lee la subida: si no existe, 404; si es de otro usuario, 403.
- Cada índice pedido debe estar en la subida; si no, 400.
- Elige destinos nuevos con `choose_nodes`, excluyendo `exclude_nodes`, y entrega pases nuevos.
- Con `exclude_nodes: []`, solo renueva los pases (decisión 3 de la 02c).

**`POST /internal/blocks/confirm`** (decisión 1 de la 02c):

- Busca las zonas de `stored_on` en `/nodes/dn/`. Si cubren menos de 2: 422 `E_REPLICAS`, sin escribir nada.
- Si no, crea la clave `{size, stored_on, touched_at}`, o une `stored_on` con la que ya existía.
- Lectura y escritura van en una transacción que compara la `mod_revision` leída. Si falla, reintenta hasta 3 veces.
- Haz una función común `update_stored_on(etcd, user, hash, add=(), remove=())` en `src/metaserver/blocks.py`, con esa misma transacción y esos reintentos, para claves que ya existen. La usarán los latidos (tarea 13) y el líder (tarea 19).

**`POST /uploads/{upload_id}/commit`:**

1. Primero `resolve_lock` (409 o 403). Después la subida: si no existe, 404; si es de otro usuario, 403; si su ruta no es la del lock, 409 `E_LOCK_LOST`.
2. Comprueba la forma del archivo:
   - `len(hashes) == ceil(size / block_size)`;
   - cada bloque mide `block_size`, salvo el último, que mide lo que falta. El tamaño de cada bloque sale de su clave en `/blocks/`;
   - si no cuadra: 400. Así se ve un hueco.
   - Un archivo vacío es `size 0` con `hashes []`.
3. Cada hash debe tener su clave en `/blocks/{user}/`, y esa clave solo existe si el bloque quedó en 2 zonas. Si falta alguna: 422 `E_REPLICAS`. Esto se comprueba **antes** de la transacción, no dentro, por dos motivos: etcd limita a 128 las comparaciones de una transacción, y la limpieza nunca borra una clave tocada hace menos de `DFS_GC_MIN_AGE_S`.
4. Una transacción (DA-08 y regla de creación de la sección 8):
   - **Antes de armarla:** si el archivo existe, su `version` debe ser `base_version`; si no, 409 `E_VERSION_CONFLICT`.
   - **Compara:**
     - que la clave del lock exista;
     - si `base_version` es 0: que `/ns/...` no exista y que la `mod_revision` del padre no haya cambiado (la raíz no se compara);
     - si no: que la `mod_revision` de `/ns/...` sea la leída.
   - **Si pasa:**
     - escribe `/ns/...` con `version + 1`, y un `file_id` nuevo (uuid4) si `base_version` es 0;
     - escribe el manifiesto nuevo, con `created_at` = ahora y `replaced_at` = `null`;
     - reescribe el manifiesto anterior con `replaced_at` = ahora;
     - borra `/uploads/{upload_id}`.
   - **Si falla:** vuelve a leer y responde según lo que encuentre: 409 `E_LOCK_LOST`, 409 `E_VERSION_CONFLICT` o 404 (el padre ya no está).
5. El commit no libera el lock; eso lo hace el cliente con `DELETE /locks`.

**`GET /files/blocks`:**

- Entrega la versión actual, o la `version` pedida si es la actual o sigue retenida (`replaced_at` hace menos de `DFS_RETENTION_S`). Si no, 404. Un directorio: 400.
- Por bloque, las réplicas son `stored_on` ∩ nodos vivos, con su dirección pública. Primero van las de la zona de `X-Client-Zone`. Un bloque sin réplicas vivas va con la lista vacía.
- Un pase `r` por bloque.
- Después de un `rm`, `/ns` ya no existe y responde 404 aunque el manifiesto siga retenido. Es una limitación aceptada: el lector reabre. Déjala en un comentario.

### Pruebas

- **`choose_nodes`:** rotación con distintos `start` e `index`; corrección de zona; caso de 2 nodos; `exclude`; sin espacio → 507; sin nodos suficientes → 503. Con la topología de DA-13, todo trío cubre 2 zonas.
- **Reserva:**
  - un bloque presente no recibe pase y su `touched_at` cambia;
  - al decodificar el pase de un bloque faltante: los secundarios llevan puerto interno, y al cliente se le entrega el primario con puerto público;
  - cada error de la lista: versión, lock de otro usuario, lock de otro archivo, directorio, padre, `block_size` y bloques inválidos.
- **Realloc:** respeta `exclude_nodes`; un índice ajeno da 400; con `[]` renueva los pases.
- **Confirm:** una sola zona da 422 y no escribe nada; crea la clave; une `stored_on`; los reintentos funcionan cuando otra escritura se cruza.
- **Commit:**
  - camino feliz: versiones, manifiestos y `/uploads` borrada;
  - `base_version` 0 crea el `file_id`;
  - falta un bloque → 422; forma inconsistente → 400; archivo vacío;
  - versión vieja → 409; lock revocado → 409; padre borrado justo antes → 404.
- **`GET /files/blocks`:** orden por zona; nodos caídos excluidos; versión retenida sí; versión vencida 404; después de `rm` 404; pase `r` válido.
- **Una prueba de punta a punta en memoria**, con `TestClient` y `FakeEtcd`: lock → reserva → confirm (llamando a la app interna) → commit → `GET /files/blocks` → `DELETE /locks`.

---

## Tarea 13 · Nodos: registro, latidos, lista de MetaServers y /admin/status

Rutas: `GET /cluster/metaservers`, `GET /admin/status`, `POST /internal/register` y `POST /internal/heartbeat`.

### Decisiones ya tomadas

- **`DFS_NODE_IP` es obligatoria para el MetaServer.** Sin ella no arranca, y el mensaje lo dice claro.
- **Autorregistro del MetaServer**, en el *lifespan* de la app pública:
  - pide un lease de `DFS_NODE_LEASE_S` y escribe `/nodes/meta/{DFS_NODE_ID}` = `{ip, public_port, internal_port}`;
  - una tarea de fondo renueva el lease cada `DFS_HEARTBEAT_S`. Si el lease ya no existe, se registra de nuevo. Si etcd no responde, lo deja en el log y lo intenta en la siguiente vuelta;
  - si etcd no responde **al arrancar**, el arranque falla y Docker lo reinicia, como en la tarea 07.
- **`GET /cluster/metaservers`:** las claves de `/nodes/meta/`, ordenadas por `meta_id`.
- **`POST /internal/register`:**
  - pide un lease de `DFS_NODE_LEASE_S` y escribe `/nodes/dn/{node_id}` atada a ese lease. Si ya había una clave de un registro anterior, se sobrescribe; el lease viejo vence solo;
  - responde `lease_id` en hexadecimal y `heartbeat_s = DFS_HEARTBEAT_S`;
  - la identidad del nodo la da su certificado firmado por la CA (decidido en la etapa 1).
- **`POST /internal/heartbeat`**, en este orden:
  1. `lease_keepalive`. Si el lease no existe, o si `/nodes/dn/{node_id}` no está atada a ese lease: 404 `E_NOT_FOUND`, y el DataNode se registra de nuevo.
  2. Reescribe `/nodes/dn/{node_id}` con el `free_bytes` nuevo, en el mismo lease.
  3. `done_orders`: por cada orden que siga en `/orders/{node_id}/`, si es `copy` agrega `target.node_id` al `stored_on` del bloque con `update_stored_on`. Si la clave del bloque ya no existe, no hace nada. Después borra la orden. Una orden que ya no existe se ignora.
  4. `bad_blocks`: quita este nodo del `stored_on` de cada bloque.
  5. Responde las órdenes pendientes de `/orders/{node_id}/`.
- **`GET /admin/status`**, con `require_admin`:
  - `leader`: el valor de `leader("/election/leader")`, que será el `meta_id` con que la tarea 19 hace campaña, o `null` si no hay líder. Si el modelo no admite `null`, cámbialo: es un cambio de contrato.
  - `datanodes`: los vivos de `/nodes/dn/` y, como caídos, los `node_id` que aparecen en algún `stored_on` sin clave viva (sus campos en `null` y `alive: false`). Todos ordenados por `node_id`.
  - `under_replicated`: los bloques con menos de 3 copias vivas, con su `alive_on`.
  - `pending_orders`: todo `/orders/`.
  - Recorrer todo `/blocks/` en cada consulta es aceptable a la escala del proyecto.

### Pruebas

- `register` escribe la clave atada al lease y responde `heartbeat_s`.
- El latido renueva el lease y devuelve las órdenes.
- Una `copy` cumplida agrega el destino a `stored_on` y borra la orden. Un bloque dañado quita el nodo.
- Lease inexistente: 404. Lease de otro nodo: 404.
- Lista de MetaServers.
- Autorregistro al arrancar, y nuevo registro cuando se pierde el lease.
- Sin `DFS_NODE_IP` el MetaServer no arranca.
- `/admin/status`: carga en `FakeEtcd` el estado del ejemplo del contrato (dn4 caído, dos bloques con 2 copias, dos órdenes) y compara con el JSON del ejemplo.

### Comprobación en Compose

Con `docker compose -f compose.local.yml up -d --build`, desde el contenedor `client`:

- `GET https://10.88.0.21:8443/api/v1/cluster/metaservers` muestra los 3 MetaServers;
- después de entrar como `admin`, `/admin/status` muestra los 3 MetaServers. La lista de DataNodes sigue vacía hasta la tarea 15.

---

## Tarea 14 · DataNode: reenviar a los secundarios y confirmar

Ruta: `PUT /api/v1/blocks/{hash}` (pública), completa.

### Decisiones ya tomadas

- **Cliente del MetaServer para el DataNode.** Clase `MetaClient(settings, ssl_context)` en `src/datanode/meta_client.py`, con el método `async post(path, json) -> dict`. La usan esta tarea (confirm) y la 15 (register y heartbeat), así que el nombre queda fijo.
  - **Descubrimiento:** prueba `DFS_META_SEEDS` en orden con `GET https://{ip}:{DFS_META_PUBLIC_PORT}/api/v1/cluster/metaservers` y guarda la lista.
  - **Llamadas:** van a `https://{ip}:{internal_port}{path}` de cada MetaServer, empezando por el último que respondió. Ante un error de conexión o un 503, pasa al siguiente. Si todos fallan, vuelve a descubrir una vez y reintenta una vez; si no, `DFSError` `E_UNAVAILABLE`.
  - **Errores:** un 4xx del MetaServer se convierte en `DFSError` con el mismo código, para que quien llama decida.
  - **mTLS:** con `client_ssl_context(ca, cert, key)` del nodo.
- **`PUT /blocks/{hash}`** (DA-04):
  1. `require_pass("w")`, antes de leer el cuerpo (ya está así).
  2. `store.write(pase.user, hash, request.stream())`. Si falla, responde ese error y no reenvía nada.
  3. Lee el bloque guardado con `store.read`, sin bloquear el bucle de eventos, y lo envía **a la vez** a cada dirección de `pase.secondaries`, con `PUT /api/v1/internal/blocks/{user}/{hash}` por mTLS. Espera como máximo `DFS_PRIMARY_WAIT_S` en total. Cuenta los que respondieron 201; el `node_id` sale de su respuesta.
  4. `stored_on` = este nodo más los secundarios que respondieron. Si son menos que `DFS_W`: 422 `E_REPLICAS`, sin confirmar.
  5. `POST /internal/blocks/confirm` con `{user, hash, size, stored_on}`. Si responde 422 `E_REPLICAS`, se lo pasa al cliente. Si ningún MetaServer responde: 503 `E_UNAVAILABLE`.
  6. Responde 201 `{hash, stored_on}`.
- **Clientes HTTP:** los clientes httpx hacia DataNodes y MetaServers se crean una vez en el *lifespan*, y las pruebas los reemplazan con `MockTransport`.
- **Copias huérfanas:** las copias de un bloque que no se confirmó quedan en disco sin clave en etcd, y la limpieza (que recorre etcd) no las ve. Es una limitación aceptada, que cuesta solo espacio en disco. Déjala en un comentario.

### Pruebas

- Los 2 secundarios responden: 201 con 3 nodos en `stored_on`.
- Falla uno con `DFS_W = 2`: 201 con 2. Fallan los dos: 422, y no se llama a confirm.
- Un secundario que tarda más que `DFS_PRIMARY_WAIT_S` no cuenta.
- Confirm responde 422: el cliente recibe 422. Ningún MetaServer responde: 503.
- `MetaClient`: pasa al siguiente MetaServer y vuelve a descubrir.
- Cuerpo con hash equivocado: 422, y no se reenvía nada.
- Pase `r`: 403 antes de leer el cuerpo.

La prueba de punta a punta en Compose llega con la tarea 17 de José, porque sin la 15 los DataNodes no están registrados.

---

## Pendientes

Si algo no está decidido aquí ni en los documentos, no lo inventes: anótalo en "Pendientes" al final de tu respuesta.

## Criterios de aceptación (cada tarea)

- `pytest` pasa completo, en Windows y en Linux.
- Las rutas de la tarea cumplen el contrato; las demás siguen como estaban.
- El OpenAPI solo cambia en la tarea 12 (y en la 13, si `leader` pasa a admitir `null`). En ese caso, `contratos.md` y `tests/test_contratos.py` cambian en el mismo cambio.
- Ningún tiempo queda fijo en el código.
- Las decisiones viven en funciones puras con sus propias pruebas: `choose_nodes`, la validación del commit y la construcción de `/admin/status`.

Al terminar cada tarea, detente: muéstrame el resultado de pytest, los archivos cambiados y los pendientes; no hagas commit.
