# 26 · Concurrencia: dos escritores sobre el mismo archivo

## 1. Objetivo

Con dos clientes `dfs shell` del mismo usuario, A y B, que escriben el mismo archivo y el mismo bloque (el bloque 0 de `/conc.txt`), comprobar los tres casos de la fila "Concurrencia" de la Tabla 21:

1. **Con espera:** A abre en `w`; B pide el lock con `--wait 30`, espera en la fila, entra cuando A cierra y parte de la versión que A publicó.
2. **Con espera 0:** mientras A tiene el lock, B recibe 423 `E_LOCKED` al instante, en el shell y con `dfs put --wait 0`.
3. **Commit tardío:** A abre sin renovar el lock (`--no-renew`), su lease de `DFS_LOCK_LEASE_S` vence, B escribe y publica, y el `close` tardío de A recibe 409 `E_LOCK_LOST` sin publicar nada.

## 2. Qué comprueba

| Comprobación | Garantía o decisión | Fila de la Tabla 21 |
|---|---|---|
| B espera en la fila y entra cuando A suelta el lock; su versión base es la que publicó A | DA-08 (fila de etcd, un escritor por archivo) | Concurrencia |
| Con `wait_s` 0 y el lock ocupado, 423 sin esperar | DA-08 | Concurrencia |
| Un `close` con el lock vencido responde 409 y no publica; la versión de B se conserva | G4 (sin escrituras perdidas), DA-08 | Concurrencia |
| Cada versión publicada es completa y la lee otro cliente | G2, G3 | — |

El enunciado pide "dos clientes sobre un mismo archivo/bloque". En DFSha los dos coinciden: los dos escriben en el offset 0, dentro del bloque 0. La política es un escritor por archivo (DA-08), así que dos escritores nunca modifican el mismo bloque a la vez: el segundo espera o recibe 423.

## 3. Entorno

| | Ensayo local | AWS |
|---|---|---|
| Dónde | `compose.local.yml` en un PC con Windows 11 y Docker Desktop 29.6.1 (Compose 5.3.0); los comandos corren en Git Bash. Clientes: tres sesiones del usuario `conc26` en el contenedor `client` (A, B y R para leer) | VM5, contenedor `client`; dos terminales `./infra/ssh.sh vm5` |
| Commit | rama `limpieza-final` sobre `main` en `b81b930`, antes de su primer commit. Los cambios de la rama no tocan el comportamiento: docstrings, versión 1.0.0 y documentación del OpenAPI | *(pendiente: ensayo de la fase 4)* |
| DA-16 | normales, sin `.env`: `DFS_LOCK_LEASE_S=30`, `DFS_LOCK_RENEW_S=10`, `DFS_W=2`, `DFS_BLOCK_MIB=8` | normales |
| Fecha | 8 de noviembre de 2026, de 02:03 a 02:06 UTC | — |

## 4. Pasos

No hay un script nuevo: son los comandos del shell de la tarea 18 (`open`, `write` y `close`, con `--wait N` y `--no-renew`), en dos terminales. `--no-renew` existe para este caso: deja que el lock venza sin cerrar el archivo (docstring de `src/dfs_client/shell.py`).

**Preparación** (una vez). Crea el usuario y abre una sesión por cliente; cada sesión tiene su propio `HOME` en el contenedor:

```bash
C="docker compose -f compose.local.yml"            # en la VM5: C="docker compose"
$C exec client dfs login --user admin              # contraseña: secrets/local/admin_password
$C exec client dfs useradd conc26
for s in a b r; do $C exec -e HOME=/tmp/s26/$s client dfs login --user conc26; done
```

Terminal A: `$C exec -e HOME=/tmp/s26/a client dfs shell`. Terminal B: `$C exec -e HOME=/tmp/s26/b client dfs shell`.

**Caso 1, con espera:**

```text
A: open /conc.txt w
A: write 3 0 escrito-por-A
B: open /conc.txt w --wait 30          <- queda esperando
A: close 3                             <- publica la versión 1; B entra
B: write 3 0 escrito-por-B
B: close 3                             <- publica la versión 2
```

**Caso 2, con espera 0:**

```text
A: open /conc.txt rw
A: write 3 0 cambio-de-A
B: open /conc.txt w --wait 0           <- error E_LOCKED al instante
B (otra terminal): dfs put --wait 0 /tmp/s26/otro.txt /conc.txt   <- error E_LOCKED
A: close 3
```

**Caso 3, commit tardío:**

```text
A: open /conc.txt w --no-renew
A: write 3 0 tarde-A
   ... más de DFS_LOCK_LEASE_S (30 s): el lease de A vence ...
B: open /conc.txt w --wait 30          <- entra enseguida
B: write 3 0 a-tiempo-B
B: close 3                             <- publica
A: close 3                             <- error E_LOCK_LOST
```

Después de cada caso, R lee: `$C exec -e HOME=/tmp/s26/r client sh -c 'dfs stat /conc.txt && dfs get /conc.txt /tmp/s26/c.txt && cat /tmp/s26/c.txt'`.

En el ensayo, los comandos de A y B le llegaron a cada `dfs shell` por una tubería, con `sleep` entre ellos para fijar el orden (A cierra a los 12 s en los casos 1 y 2, y a los 48 s en el 3; B empieza a los 3 s y a los 36 s), y cada línea de salida lleva la hora UTC en que apareció. La salida completa, con el log de acceso de los MetaServers, está en `datos/26-concurrencia-local.txt`.

## 5. Resultado esperado y resultado obtenido

**Esperado:** en el caso 1, el `open` de B termina en el mismo segundo que el `close` de A, con la versión 1 como base, y B publica la 2. En el caso 2, B recibe `E_LOCKED` mientras A sigue con el archivo abierto. En el caso 3, B publica y el `close` de A responde `E_LOCK_LOST`; la versión final es la de B.

### 5.1 Ensayo local

**Caso 1.** B pidió el lock a las 02:03:51 y su `open` volvió a las 02:04:00, el mismo segundo en que A publicó la versión 1. Partió de esa versión (`versión 1, 0 bytes`: en `w` el archivo empieza vacío) y publicó la 2.

```text
02:03:50  A  dfs> fd 3: /conc.txt en modo w, versión 0, 0 bytes
02:03:50  A  dfs> 13 bytes escritos
02:03:51  --  B pide el lock (open /conc.txt w --wait 30)
02:04:00  A  dfs> fd 3 cerrado: /conc.txt versión 1
02:04:00  B  dfs> fd 3: /conc.txt en modo w, versión 1, 0 bytes
02:04:00  B  dfs> 13 bytes escritos
02:04:01  B  dfs> fd 3 cerrado: /conc.txt versión 2
02:04:04  R  versión:     2
02:04:05  R  contenido: escrito-por-B
```

**Caso 2.** Con A dentro, los dos intentos de B con espera 0 fallaron en el acto. El CLI cambia el mensaje del 423 por uno que dice qué hacer. A publicó después la versión 3: en `rw` reescribió los primeros 11 bytes de la versión 2.

```text
02:04:18  A  dfs> fd 3: /conc.txt en modo rw, versión 2, 13 bytes
02:04:18  A  dfs> 11 bytes escritos
02:04:21  B  dfs> error E_LOCKED: otro cliente tiene el lock de /conc.txt
02:04:24  B  error E_LOCKED: El archivo está bloqueado por otro cliente; intenta más tarde
02:04:24  B  (código de salida de dfs put: 1)
02:04:28  A  dfs> fd 3 cerrado: /conc.txt versión 3
02:04:31  R  contenido: cambio-de-A-B
```

**Caso 3.** El lease de A venció sin renovarse. B entró sin esperar, a las 02:05:22, y publicó la versión 4. A intentó publicar 46 s después de abrir y recibió `E_LOCK_LOST`: su cambio no se publicó y la versión 4 de B quedó intacta.

```text
02:04:46  A  dfs> fd 3: /conc.txt en modo w, versión 3, 0 bytes
02:04:46  A  dfs> 7 bytes escritos
02:05:20  --  B pide el lock (open /conc.txt w --wait 30)
02:05:22  B  dfs> fd 3: /conc.txt en modo w, versión 3, 0 bytes
02:05:22  B  dfs> 10 bytes escritos
02:05:22  B  dfs> fd 3 cerrado: /conc.txt versión 4
02:05:32  A  dfs> fd 3: /conc.txt modo w versión 3 7 bytes, 1 bloques sin publicar
02:05:32  A  dfs> error E_LOCK_LOST: perdiste el lock o alguien publicó otra versión; tus cambios no se publicaron
02:05:34  R  versión:     4
02:05:36  R  contenido: a-tiempo-B
```

**Códigos HTTP** (log de acceso de los MetaServers): los dos intentos de B con espera 0 fueron `POST /api/v1/locks` → `423 Locked` (02:04:21 y 02:04:24), y el `close` tardío de A, `POST /api/v1/uploads` → `409 Conflict` (02:05:32).

Cómo se lee:

- **Dónde salió el 409.** `close()` publica en tres pasos: reserva (`POST /uploads`), envío de bloques y commit. La reserva ya exige el lock (contrato, sección 2) para fallar antes de transferir bloques, así que el 409 salió ahí. La transacción del commit vuelve a comprobar la clave del lock (G4); ese caso, con el lock perdido justo antes del commit, lo prueban `test_commit_lock_and_upload_errors` y `test_a_commit_whose_transaction_fails_answers_what_changed[lock-perdido]` en `tests/test_uploads.py`.
- **El lock de A sí venció.** B no esperó: su `open` con `--wait 30` volvió en el mismo segundo. A esa hora, el lease de A llevaba unos 36 s sin renovarse, más que sus 30 s.
- **Ninguna escritura se perdió.** Las versiones 1 a 4 se publicaron en orden, cada una sobre la anterior, y la única que no se publicó fue la del escritor que ya no tenía el lock.

### 5.2 AWS

*Pendiente: ensayo de la fase 4.*

## 6. Conclusión

En el ensayo local, DFSha cumple la política de DA-08 con dos clientes sobre el mismo archivo y el mismo bloque: un solo escritor a la vez; el segundo espera en la fila y parte de la versión publicada, o recibe 423 si pidió espera 0; y un escritor cuyo lock venció recibe 409 y no pisa la versión de otro (G4). *La corrida en AWS queda para el ensayo de la fase 4.*

## 7. Para el video

Dos terminales una al lado de la otra, cada una con `dfs shell`:

1. Caso 1: A abre en `w` y escribe; B pide el lock y se queda esperando; A cierra y en ese momento B entra con la **versión 1** como base.
2. Caso 2: con A dentro, B pide con `--wait 0` y recibe `E_LOCKED` al instante.
3. Caso 3, si hay tiempo (toma unos 40 s): A abre con `--no-renew`; pasados 30 s, B escribe y publica; A cierra y recibe `E_LOCK_LOST`. `dfs get` muestra el contenido de B.
