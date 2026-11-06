# 22 · Integración local: todo junto en Compose

## 1. Objetivo

Comprobar que el sistema completo (3 etcd, 3 MetaServers, 4 DataNodes y el cliente `dfs`) funciona de punta a punta en un PC con `compose.local.yml`, antes de llevarlo a AWS:

- la prueba oficial de carga y recuperación: `put` y `get` de un archivo aleatorio de 512 MiB, con el mismo SHA-256, con el script de humo `scripts/pruebas/smoke.sh`;
- dos comprobaciones rápidas de integración, que no son las pruebas oficiales: un ciclo de RF3 en `dfs shell`, y, con los valores cortos de DA-16, la re-réplica al detener `dn4`, las copias que sobran cuando vuelve y la limpieza después de `rm`;
- una medición de referencia de `put` y `get` de 512 MiB en local, para comparar después con AWS.

## 2. Qué comprueba

| Parte | Garantía | Decisión | Fila de la Tabla 21 |
|---|---|---|---|
| Script de humo con 512 MiB | G1 (cada bloque en al menos 2 zonas) | DA-04, DA-05, DA-16 | Carga y recuperación |
| RF3 en `dfs shell` (rápida) | G2, G3 | DA-08, DA-14 | Acceso RF3 |
| Re-réplica al detener `dn4` (rápida) | G5 | DA-12 | Fallos: un DataNode (solo la parte de reponer la tercera copia) |
| Copias que sobran al volver `dn4` (rápida) | — | DA-12 | — |
| Limpieza después de `rm` (rápida) | — | DA-02, DA-14 | — |

## 3. Entorno

- **Local:** Windows 11 Pro, AMD Ryzen 7 7730U (8 núcleos, 16 hilos), 14,8 GiB de RAM, SSD NVMe. Docker Desktop con el motor 29.6.1 y Compose v5.3.0; su máquina virtual tiene 16 CPU y 7,2 GiB. Todos los contenedores comparten ese PC, y en el mismo Docker corrían contenedores de otros proyectos.
- **Commit:** el código del sistema es el de `main` en `900c790` (tareas 01 a 20 y 23). `scripts/pruebas/smoke.sh` es el de la rama `t22-integracion`.
- **Imagen y datos:** `dfsha:local` construida sin caché desde ese commit, con volúmenes nuevos (`down -v`).
- **Dónde se corrieron los scripts:** en Git Bash. Desde WSL hace falta activar en Docker Desktop la integración con la distro (Settings, Resources, WSL integration); sin ella, el script dice "Docker no responde".
- **Valores de DA-16:**

| Parte | Valores |
|---|---|
| Script de humo, medición de referencia y RF3 | los normales de la Tabla 19 (sin `.env`): `DFS_BLOCK_MIB=8`, `DFS_W=2`, `DFS_PRIMARY_WAIT_S=10`, `DFS_HEARTBEAT_S=5`, `DFS_NODE_LEASE_S=15`, `DFS_REREPLICATE_AFTER_S=60`, `DFS_LEADER_GRACE_S=300`, `DFS_RETENTION_S=600`, `DFS_GC_INTERVAL_S=600`, `DFS_GC_MIN_AGE_S=3600`, `DFS_LEADER_SCAN_S=10` |
| Re-réplica, copias que sobran y limpieza | `.env` temporal en la raíz: `DFS_LEADER_GRACE_S=60`, `DFS_REREPLICATE_AFTER_S=30`, `DFS_RETENTION_S=60`, `DFS_GC_INTERVAL_S=30`, `DFS_GC_MIN_AGE_S=120`; los demás, normales |

## 4. Pasos

Desde la raíz del repositorio. En Git Bash, antes: `export MSYS_NO_PATHCONV=1`, para que no cambie las rutas `/tmp/...` de los contenedores.

**4.1 Sistema desde cero, con el código de `main`**

```bash
docker compose -f compose.local.yml down -v
docker compose -f compose.local.yml build --no-cache
docker compose -f compose.local.yml up -d
```

**4.2 Prueba oficial y medición de referencia: 5 corridas de 512 MiB**

```bash
for i in 1 2 3 4 5; do
  scripts/pruebas/smoke.sh --mib 512 --csv docs/pruebas/datos/22-referencia-local.csv --rep $i
done
```

El script entra como admin, crea el usuario `smoke` si no existe (su contraseña queda en `secrets/smoke_password`), sube el archivo, revisa `dfs stat --blocks` y `dfs status`, lo baja, compara el SHA-256 y borra todo al final. El archivo aleatorio y el descargado se crean dentro del contenedor `client`, nunca en el repositorio.

**4.3 RF3 con dos clientes** (dos procesos `dfs shell`, cada uno con su sesión)

```bash
C="docker compose -f compose.local.yml exec -T"
pw=$(cat secrets/smoke_password)
printf '%s\n' "$pw" | $C -e HOME=/tmp/rf3/a client dfs login --user smoke
printf '%s\n' "$pw" | $C -e HOME=/tmp/rf3/b client dfs login --user smoke

# A crea la versión 1
printf 'open /rf3.txt w\nwrite 3 0 hola mundo\nclose 3\n' | $C -e HOME=/tmp/rf3/a client dfs shell

# A abre en rw, escribe en el offset 5, lee lo suyo y tarda 15 s en cerrar
(printf 'open /rf3.txt rw\nwrite 3 5 MUNDO\nread 3 0 100\n'; sleep 15; printf 'close 3\n') |
  $C -e HOME=/tmp/rf3/a client dfs shell | sed 's/^/A: /' &
sleep 7

# B lee mientras A no ha cerrado, y otra vez después del close
printf 'open /rf3.txt r\nread 3 0 100\nclose 3\n' | $C -e HOME=/tmp/rf3/b client dfs shell | sed 's/^/B: /'
wait
printf 'open /rf3.txt r\nread 3 0 100\nclose 3\n' | $C -e HOME=/tmp/rf3/b client dfs shell | sed 's/^/B: /'

$C -e HOME=/tmp/rf3/a client dfs rm /rf3.txt </dev/null
```

**4.4 Re-réplica, copias que sobran y limpieza, con los valores cortos**

```bash
printf 'DFS_LEADER_GRACE_S=60\nDFS_REREPLICATE_AFTER_S=30\nDFS_RETENTION_S=60\nDFS_GC_INTERVAL_S=30\nDFS_GC_MIN_AGE_S=120\n' > .env
docker compose -f compose.local.yml up -d          # recrea los contenedores con esos valores

C="docker compose -f compose.local.yml exec -T"
$C -e HOME=/tmp/rr/admin client dfs login --user admin < secrets/local/admin_password
$C -e HOME=/tmp/rr/user client dfs login --user smoke < secrets/smoke_password
$C client sh -c 'head -c 64M /dev/urandom > /tmp/rr/rr.bin'
$C -e HOME=/tmp/rr/user client dfs put /tmp/rr/rr.bin /rr.bin
$C -e HOME=/tmp/rr/user client dfs stat --blocks /rr.bin

# Re-réplica: se detiene dn4 y se mira dfs status cada 10 s
docker compose -f compose.local.yml stop dn4
$C -e HOME=/tmp/rr/admin client dfs status

# Copias que sobran: vuelve dn4 y se cuentan los archivos de /rr.bin en el /data de cada DataNode
docker compose -f compose.local.yml start dn4
$C -e HOME=/tmp/rr/user client dfs stat --blocks /rr.bin
$C dn4 sh -c "ls /data/smoke | grep -cE '^(7db5ecd099ad|295cd961f094|01ca75bfdc30|e89c9671f710|da0058fb09d4|68769f1e3d5b|5ada3476441d|53594ffb950a)'"

# Limpieza: rm y, cada 10 s, el mismo conteo en dn1, dn2, dn3 y dn4
$C -e HOME=/tmp/rr/user client dfs rm /rr.bin

# Vuelta a los valores normales
rm .env
docker compose -f compose.local.yml up -d
scripts/pruebas/smoke.sh --mib 32
```

Los prefijos del `grep` son los 12 primeros caracteres de los hashes que muestra `dfs stat --blocks /rr.bin` (resultado 5.4).

## 5. Resultado esperado y resultado obtenido

### 5.1 Script de humo con 512 MiB

**Esperado:**
- todo en PASA, sin FALLA;
- 64 bloques con 3 copias, en al menos 2 zonas y repartidos en los 4 DataNodes;
- el mismo SHA-256 al bajar el archivo;
- `dfs status` con líder y ningún bloque con menos de 3 copias.

**Obtenido:** las 5 corridas terminaron en `RESULTADO: PASA (0 avisos)`. La primera:

```text
== Resumen: local, 512 MiB, usuario smoke, commit 900c790, DFS_W=2 ==
PASA   admin entró y el usuario smoke está listo
PASA   put: /smoke-20261106-153327-393.bin: versión 1, 512.0 MiB en 64 bloques (64 enviados, 0 ya estaban), 16.60 s, 30.8 MiB/s
PASA   G1: los 64 bloques tienen copias vivas en al menos 2 zonas
PASA   DA-05: bloques repartidos en los 4 DataNodes (dn1 dn2 dn3 dn4)
PASA   los 64 bloques tienen 3 copias vivas
PASA   get: 11.35 s, 45.1 MiB/s; SHA-256 igual al original (901c8654520e8716...)
PASA   hay líder: meta1
PASA   dfs status: ningún bloque con menos de 3 copias vivas
PASA   limpieza: borrado /smoke-20261106-153327-393.bin
RESULTADO: PASA (0 avisos)
```

### 5.2 Medición de referencia

Los datos crudos están en `datos/22-referencia-local.csv`: una fila por operación, con las columnas que reutiliza la tarea 25. Los tiempos son los que mide `dfs`; en `put` incluyen calcular el SHA-256 de cada bloque, tomar el lock, enviar los 64 bloques y el commit.

| Operación | Mínimo | Mediana | Máximo | Mediana en MiB/s |
|---|---|---|---|---|
| `put` 512 MiB | 15,82 s | 21,91 s | 30,14 s | 23,4 |
| `get` 512 MiB | 11,03 s | 11,76 s | 12,79 s | 43,5 |

`put` es más lento y varía más que `get`, por dos razones:
- escribe 3 copias cifradas con `fsync`, mientras que `get` lee una sola;
- en local, los 4 DataNodes, el cliente y los otros contenedores comparten la misma CPU y el mismo disco.

Por eso estos números solo son una referencia para comparar con AWS, donde cada nodo tiene su VM.

### 5.3 RF3 con dos clientes

**Esperado:** mientras A no cierra, B lee la versión 1 y A ve su propio cambio sin publicar. Después del `close`, todo `open` nuevo ve la versión 2 (G2 y G3).

**Obtenido:** las líneas de A salen al final porque `sed` las imprime cuando termina su proceso, pero B leyó durante la espera de 15 s.

```text
### B lee mientras A no ha cerrado
B: dfs> fd 3: /rf3.txt en modo r, versión 1, 10 bytes
B: dfs> hola mundo
A: dfs> fd 3: /rf3.txt en modo rw, versión 1, 10 bytes
A: dfs> 5 bytes escritos
A: dfs> hola MUNDO
A: dfs> fd 3 cerrado: /rf3.txt versión 2
### B lee después del close de A
B: dfs> fd 3: /rf3.txt en modo r, versión 2, 10 bytes
B: dfs> hola MUNDO
```

### 5.4 Re-réplica al detener `dn4`

**Esperado:**
- `dn4` aparece caído a los ~15 s, cuando vence su lease;
- el líder ordena copias cuando `dn4` lleva caído `DFS_REREPLICATE_AFTER_S` (30 s) y él ya pasó su espera `DFS_LEADER_GRACE_S` (60 s);
- al final, ningún bloque con menos de 3 copias vivas.

**Obtenido:** los contenedores se recrearon a las 15:41:08 y el líder fue `meta3`. `/rr.bin` (8 bloques) quedó así, con el reparto por turnos de DA-05 (el bloque i, en el nodo de inicio + i y los dos siguientes); 6 bloques tenían copia en `dn4`:

```text
índice  hash              tamaño  nodos
     0  7db5ecd099ad     8388608  dn2, dn3, dn4
     1  295cd961f094     8388608  dn3, dn4, dn1
     2  01ca75bfdc30     8388608  dn4, dn1, dn2
     3  e89c9671f710     8388608  dn1, dn2, dn3
     4  da0058fb09d4     8388608  dn2, dn3, dn4
     ...
```

| Hora | Evento |
|---|---|
| 15:42:25 | se detiene `dn4` |
| 15:42:50 | `dfs status`: `dn4` caído, 6 bloques con menos de 3 copias |
| 15:43:17 | el líder empieza a ordenar las copias (4 y después 7 órdenes pendientes) |
| 15:43:43 | 0 bloques con menos de 3 copias: unos 78 s después de detener `dn4` |

Después, `dfs stat --blocks` muestra la copia nueva de cada bloque y la de `dn4`, marcada como caída:

```text
     0  7db5ecd099ad     8388608  dn2, dn3, dn4 (caído), dn1
     1  295cd961f094     8388608  dn3, dn4 (caído), dn1, dn2
     2  01ca75bfdc30     8388608  dn4 (caído), dn1, dn2, dn3
```

La única orden que quedó pendiente era un `delete` de un bloque del usuario `smoke` para `dn4`, que dejó la limpieza de alguno de los archivos borrados antes. La limpieza también le escribe órdenes a los nodos caídos, y `dn4` la cumplió al volver: en 5.6 no queda ninguna orden pendiente (contrato, sección 7.3).

### 5.5 Copias que sobran al volver `dn4`

**Esperado:** al volver `dn4`, los 6 bloques quedan con 4 copias vivas. El líder lo quita de su `stored_on` y le ordena borrar esas copias.

**Obtenido:** las copias de `/rr.bin` en el `/data` de cada DataNode:

| Hora | Evento | dn1 | dn2 | dn3 | dn4 |
|---|---|---|---|---|---|
| 15:49:46 | `dn4` vuelve | 8 | 8 | 8 | 6 |
| 15:50:07 | ningún bloque con más de 3 copias | 8 | 8 | 8 | 0 |

`dfs stat --blocks` volvió a mostrar 3 nodos por bloque, sin `dn4`.

### 5.6 Limpieza después de `rm`

**Esperado:** pasada la retención (60 s), con una vuelta de la limpieza (cada 30 s) y con bloques sin cambios hace más de 120 s, la limpieza borra el manifiesto, después las claves de los bloques, y los DataNodes borran los archivos.

**Obtenido:**

| Hora | Evento | dn1 | dn2 | dn3 | dn4 |
|---|---|---|---|---|---|
| 15:50:14 | `rm /rr.bin` | 8 | 8 | 8 | 0 |
| 15:51:38 | — | 8 | 8 | 8 | 0 |
| 15:51:52 | — | 0 | 0 | 0 | 0 |

A las 15:51:52 no quedaba ninguno, 98 s después del `rm`. La misma limpieza borró los bloques de las 5 corridas de 512 MiB, unos 7,5 GiB: al final, `/data/smoke` estaba vacío en los 4 DataNodes. El estado final, con 0 bloques con menos de 3 copias y 0 órdenes pendientes:

```text
Líder: meta3
DataNodes:
  dn1      vivo   us-east-1a   10.88.0.31:9443        934.1 GiB libres
  dn2      vivo   us-east-1b   10.88.0.32:9443        934.1 GiB libres
  dn3      vivo   us-east-1c   10.88.0.33:9443        934.1 GiB libres
  dn4      vivo   us-east-1b   10.88.0.34:9443        934.1 GiB libres

Bloques con menos de 3 copias vivas: 0
Órdenes pendientes: 0
```

Al borrar el `.env` y recrear los contenedores, los cinco valores volvieron a los normales y `scripts/pruebas/smoke.sh --mib 32` terminó en `PASA (0 avisos)`.

### 5.7 Comprobaciones del propio script

| Caso | Resultado |
|---|---|
| Zona b caída (`dn2` y `dn4` detenidos) durante el humo, con `--wait-copies 10` | `G1` PASA (2 copias en `dn1` y `dn3`); AVISO por el reparto en 2 DataNodes; AVISO "2 bloques siguen con 2 copias después de 10 s"; `RESULTADO: PASA (3 avisos)`, código 0 |
| Lo mismo, pero encendiendo la zona b durante la espera (`--wait-copies 90`) | AVISO "2 bloques quedaron con 2 copias; la tercera llegó a los 10 s"; código 0 |
| El usuario existe pero no está `secrets/smoke_password` | FALLA "el usuario smoke ya existe, pero no está secrets/smoke_password con su contraseña. Usa --user con otro nombre"; código 1; no crea el archivo ni cambia contraseñas |
| `secrets/smoke_password` con otra contraseña | FALLA "la contraseña de secrets/smoke_password no sirve para el usuario smoke (¿es de otro clúster?)"; código 1 |
| Opciones inválidas (`--env gcp`, `--mib` sin valor, `--user admin`) | mensaje de uso y código 2 |
| Desde WSL, sin la integración de Docker Desktop | FALLA "Docker no responde. ... WSL integration"; código 1 |

## 6. Conclusión

El sistema completo funciona de punta a punta en local, con TLS 1.3 y mTLS:
- el script de humo pasa con 512 MiB en las 5 corridas, y es reproducible;
- el ciclo de RF3 cumple la semántica declarada (G2 y G3);
- con los valores cortos, el líder repone las copias de un DataNode caído, borra las que sobran cuando vuelve, y la limpieza saca de los discos los bloques de un archivo borrado.

No apareció ningún fallo de integración que obligara a cambiar el código del sistema. La medición de referencia en local es de 23,4 MiB/s en `put` y 43,5 MiB/s en `get` (medianas con 512 MiB).

## 7. Para el video

1. `docker compose -f compose.local.yml ps`: los 11 contenedores, y los 3 etcd en `healthy`.
2. `scripts/pruebas/smoke.sh --mib 512`: el progreso y el resumen con `RESULTADO: PASA`.
3. Durante una subida, `dfs stat --blocks` de un archivo: el reparto por turnos en los 4 DataNodes (DA-05).
4. Dos terminales con `dfs shell`: B lee la versión 1 mientras A tiene el archivo abierto en `rw`, y la versión 2 después del `close`.
5. Con los valores cortos: `docker compose stop dn4` y `dfs status` cada pocos segundos, hasta que llegue a "Bloques con menos de 3 copias vivas: 0".
6. `dfs rm` y `ls /data/smoke | wc -l` en un DataNode, antes y después de la limpieza.
