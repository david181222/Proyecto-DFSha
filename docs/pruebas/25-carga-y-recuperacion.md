# 25 · Carga y recuperación

## 1. Objetivo

Subir un archivo aleatorio de 512 MiB desde la VM5 y comprobar que:

1. `dfs stat --blocks` muestra los bloques repartidos en los 4 DataNodes, cada uno con 3 copias en al menos 2 zonas;
2. `dfs get` devuelve un archivo con el mismo SHA-256 que el original.

## 2. Qué comprueba

| Comprobación | Garantía o decisión | Fila de la Tabla 21 |
|---|---|---|
| Cada bloque con copias vivas en al menos 2 zonas | G1 (confirmación), DA-04 | Carga y recuperación |
| Bloques repartidos en los 4 DataNodes, 3 copias | DA-05 (ubicación de réplicas) | Carga y recuperación |
| SHA-256 igual después del `get` | G1, DA-02 (verificación por hash) | Carga y recuperación |

## 3. Entorno

| | Ensayo local | AWS |
|---|---|---|
| Dónde | `compose.local.yml`, un PC con Linux (16 núcleos, 13 GiB de RAM) | 5 VMs t3.small de us-east-1 (README, "Despliegue en AWS"); el script corre en la VM5 |
| Commit | `b31d2b9` más los scripts de la tarea 25, sin commit todavía | `7eedf22` (`main`, después del cierre), en el clúster de Edwin |
| DA-16 | `scripts/pruebas/pruebas.env` y `DFS_W=3` | normales de la Tabla 19 (`./infra/deploy.sh` sin `--times`) y `DFS_W=2` |

Con `scripts/pruebas/pruebas.env`, los tiempos son los de la columna "Pruebas" de la Tabla 19: `DFS_LEADER_GRACE_S=60`, `DFS_RETENTION_S=60`, `DFS_GC_INTERVAL_S=30` y `DFS_GC_MIN_AGE_S=120`. Los demás quedan en su valor normal (`DFS_BLOCK_MIB=8`, `DFS_PRIMARY_WAIT_S=10`, `DFS_HEARTBEAT_S=5`, `DFS_NODE_LEASE_S=15`, `DFS_REREPLICATE_AFTER_S=60`). Ninguno de esos cambios toca el camino del `put` y del `get`.

## 4. Pasos

No hace falta un script nuevo: `scripts/pruebas/smoke.sh` (tarea 22) hace exactamente esta fila de la Tabla 21. Sube el archivo, revisa `stat --blocks` y `status`, lo baja, compara el SHA-256 y borra el archivo remoto.

**AWS**, desde la máquina que despliega, en la raíz del repositorio. Así se corrió, con el despliegue final de `main`:

```bash
./infra/deploy.sh                                       # main, tiempos normales de la Tabla 19
./infra/deploy.sh --check
./infra/ssh.sh vm5 scripts/pruebas/smoke.sh --env aws   # 512 MiB por defecto
```

Para mostrar `dfs stat --blocks` completo (para el video), dentro de la VM5 (`./infra/ssh.sh vm5`):

```bash
head -c 512M /dev/urandom > /tmp/carga.bin && sha256sum /tmp/carga.bin
docker compose cp /tmp/carga.bin client:/tmp/carga.bin
docker compose exec client dfs login --user smoke           # contraseña: secrets/smoke_password
docker compose exec client dfs put /tmp/carga.bin /carga.bin
docker compose exec client dfs stat --blocks /carga.bin
docker compose exec client dfs get /carga.bin /tmp/copia.bin
docker compose exec client sha256sum /tmp/carga.bin /tmp/copia.bin
docker compose exec client dfs rm /carga.bin
```

**Ensayo local:** `scripts/pruebas/smoke.sh --mib 64`, con `DFS_W=3` en el `.env` de la raíz.

## 5. Resultado esperado y resultado obtenido

**Esperado:** `RESULTADO: PASA`, con las líneas de G1 y DA-05 en `PASA`: los 64 bloques con 3 copias vivas, en al menos 2 zonas, en dn1 a dn4. El SHA-256 del `get` es igual al original.

### 5.1 Ensayo local (64 MiB)

```text
PASA   put: /smoke-20261106-173251-694883.bin: versión 1, 64.0 MiB en 8 bloques (8 enviados, 0 ya estaban), 1.20 s, 53.5 MiB/s
PASA   G1: los 8 bloques tienen copias vivas en al menos 2 zonas
PASA   DA-05: bloques repartidos en los 4 DataNodes (dn1 dn2 dn3 dn4)
PASA   los 8 bloques tienen 3 copias vivas
PASA   get: 0.43 s, 147.5 MiB/s; SHA-256 igual al original (0f97aa95f56f1f03...)
PASA   hay líder: meta3
PASA   dfs status: ningún bloque con menos de 3 copias vivas
PASA   limpieza: borrado /smoke-20261106-173251-694883.bin
RESULTADO: PASA (0 avisos)
```

### 5.2 AWS (512 MiB)

Humo del 8 de noviembre de 2026, de 13:56:14 a 13:56:48 UTC, justo después del despliegue final (`DFS_W=2`, tiempos normales):

```text
== Resumen: aws, 512 MiB, usuario smoke, commit 7eedf22, DFS_W=2 ==
PASA   admin entró y el usuario smoke está listo
PASA   put: /smoke-20261108-135619-3051.bin: versión 1, 512.0 MiB en 64 bloques (64 enviados, 0 ya estaban), 8.37 s, 61.2 MiB/s
PASA   G1: los 64 bloques tienen copias vivas en al menos 2 zonas
PASA   DA-05: bloques repartidos en los 4 DataNodes (dn1 dn2 dn3 dn4)
PASA   los 64 bloques tienen 3 copias vivas
PASA   get: 5.43 s, 94.4 MiB/s; SHA-256 igual al original (bdff6308c5347a0a...)
PASA   hay líder: meta3
PASA   dfs status: ningún bloque con menos de 3 copias vivas
PASA   limpieza: borrado /smoke-20261108-135619-3051.bin
RESULTADO: PASA (0 avisos)
```

El humo no imprime `dfs stat --blocks`. Las primeras filas, de otro archivo de 512 MiB subido en el ensayo de la demostración del mismo día (14:20 UTC, también con `DFS_W=2`, y con los tiempos de `pruebas.env`, que no tocan el `put`):

```text
/demo/a512.bin  versión 1  536870912 bytes  bloques de 8388608 bytes  sha256 f108e7fe347a
índice  hash              tamaño  nodos
     0  c123f5e85a13     8388608  dn2, dn3, dn4
     1  2e6309788bab     8388608  dn3, dn4, dn1
     2  805dce0609a6     8388608  dn4, dn1, dn2
     3  40bb3b4e58f9     8388608  dn1, dn2, dn3
     4  24a46661b66d     8388608  dn2, dn3, dn4
```

El bloque i va a tres DataNodes seguidos desde un inicio aleatorio (aquí dn2), y cada trío cubre al menos 2 zonas: la b tiene dos nodos, así que ningún trío queda en una sola zona (DA-05). Ese `put` tardó 8,96 s y el `get`, 5,69 s, con el mismo SHA-256.

## 6. Conclusión

Se cumple la fila "Carga y recuperación" de la Tabla 21. En AWS, un archivo de 512 MiB queda en 64 bloques repartidos en los 4 DataNodes, cada uno con 3 copias en al menos 2 zonas (G1, DA-05), y el `get` devuelve el mismo SHA-256. El ensayo local (64 MiB, `DFS_W=3`) dio el mismo resultado; los tiempos no se comparan, porque cambian el tamaño, W y la máquina.

## 7. Para el video

1. `dfs status` con el líder y los 4 DataNodes vivos, cada uno en su zona.
2. `sha256sum` del original, `dfs put` de 512 MiB y el tiempo que imprime.
3. `dfs stat --blocks`: las primeras filas, para que se vea que el bloque i va a tres nodos seguidos y que siempre hay al menos dos zonas.
4. `dfs get` y los dos SHA-256 iguales, uno al lado del otro.
