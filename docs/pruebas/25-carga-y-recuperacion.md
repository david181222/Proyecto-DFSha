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
| Commit | `b31d2b9` más los scripts de la tarea 25, sin commit todavía | *(se llena en la corrida)* |
| DA-16 | `scripts/pruebas/pruebas.env` y `DFS_W=3` | `scripts/pruebas/pruebas.env` y `DFS_W=2` |

Con `scripts/pruebas/pruebas.env`, los tiempos son los de la columna "Pruebas" de la Tabla 19: `DFS_LEADER_GRACE_S=60`, `DFS_RETENTION_S=60`, `DFS_GC_INTERVAL_S=30` y `DFS_GC_MIN_AGE_S=120`. Los demás quedan en su valor normal (`DFS_BLOCK_MIB=8`, `DFS_PRIMARY_WAIT_S=10`, `DFS_HEARTBEAT_S=5`, `DFS_NODE_LEASE_S=15`, `DFS_REREPLICATE_AFTER_S=60`). Ninguno de esos cambios toca el camino del `put` y del `get`.

## 4. Pasos

No hace falta un script nuevo: `scripts/pruebas/smoke.sh` (tarea 22) hace exactamente esta fila de la Tabla 21. Sube el archivo, revisa `stat --blocks` y `status`, lo baja, compara el SHA-256 y borra el archivo remoto.

**AWS**, desde la máquina que despliega, en la raíz del repositorio. Las VMs toman el código de GitHub (`git fetch` y `checkout` de `--ref`), así que la rama `t25-pruebas-carga`, con los scripts de esta tarea, tiene que estar en GitHub antes de desplegar.

```bash
./infra/deploy.sh --ref t25-pruebas-carga --times scripts/pruebas/pruebas.env
./infra/deploy.sh --check
# esperar DFS_LEADER_GRACE_S (60 s) después del arranque, para que no cuente como fallo una copia que falta
./infra/ssh.sh vm5 scripts/pruebas/smoke.sh --env aws --mib 512 --wait-copies 90
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

**Ensayo local:** `scripts/pruebas/smoke.sh --mib 64`.

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

*Pendiente de la corrida en AWS: la salida de `smoke.sh`, recortada, y las primeras líneas de `dfs stat --blocks`.*

## 6. Conclusión

*Pendiente de la corrida en AWS.*

## 7. Para el video

1. `dfs status` con el líder y los 4 DataNodes vivos, cada uno en su zona.
2. `sha256sum` del original, `dfs put` de 512 MiB y el tiempo que imprime.
3. `dfs stat --blocks`: las primeras filas, para que se vea que el bloque i va a tres nodos seguidos y que siempre hay al menos dos zonas.
4. `dfs get` y los dos SHA-256 iguales, uno al lado del otro.
