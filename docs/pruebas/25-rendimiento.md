# 25 · Rendimiento de put y get

## 1. Objetivo

Medir la latencia (mediana y percentil 95) y el throughput de `dfs put` y `dfs get` en tres comparaciones:

1. tamaño del archivo: 1, 64 y 512 MiB;
2. 1 cliente frente a 4 clientes a la vez, desde la VM5;
3. confirmar con 2 copias frente a 3 (`DFS_W=2` y `DFS_W=3`). W se cambia al desplegar, sin tocar el código.

Cada condición se repite 5 veces, cada vez con contenido aleatorio nuevo, y se verifica el SHA-256 en cada repetición.

## 2. Qué comprueba

| Comparación | Decisión | Fila de la Tabla 21 |
|---|---|---|
| Tamaño de archivo | DA-03 (bloques de 8 MiB, en paralelo) | Rendimiento |
| 1 frente a 4 clientes | DA-05 (reparto de bloques entre los 4 DataNodes) | Rendimiento |
| W=2 frente a W=3 | DA-04 (confirmar con 2 copias y reponer la tercera después) | Rendimiento |
| SHA-256 en cada repetición | G1 (lo confirmado se lee entero) | — |

## 3. Entorno

| | Ensayo local | AWS |
|---|---|---|
| Dónde | `compose.local.yml`, un PC con Linux (16 núcleos, 13 GiB de RAM, Docker 29.9.2) | 5 VMs t3.small de us-east-1; el script corre en la VM5 (us-east-1a) |
| Commit | `b31d2b9` más los scripts de la tarea 25, sin commit todavía | *(se llena en la corrida)* |
| DA-16 | `scripts/pruebas/pruebas.env`, con `DFS_W=2` y después `DFS_W=3` en el `.env` de la raíz | `scripts/pruebas/pruebas.env`, con `DFS_W=2` y después `DFS_W=3` |
| Tamaños, clientes y repeticiones | 1, 16 y 64 MiB; 1 y 4 clientes; 2 repeticiones más 1 de calentamiento | 1, 64 y 512 MiB; 1 y 4 clientes; 5 repeticiones más 1 de calentamiento |
| Datos | `datos/25-rendimiento-local.csv` | `datos/25-rendimiento-aws.csv` |

`pruebas.env` trae los tiempos de la columna "Pruebas" de la Tabla 19: `DFS_LEADER_GRACE_S=60`, `DFS_RETENTION_S=60`, `DFS_GC_INTERVAL_S=30` y `DFS_GC_MIN_AGE_S=120`. Los demás quedan en su valor normal, entre ellos `DFS_BLOCK_MIB=8` y `DFS_PRIMARY_WAIT_S=10`. Esos cuatro tiempos no tocan el camino del `put` ni del `get`. Se usan para que la limpieza libere el disco durante la corrida (sección 4.3).

## 4. Pasos

### 4.1 Comandos

**Ensayo local**, desde la raíz del repositorio:

```bash
cp scripts/pruebas/pruebas.env .env
docker compose -f compose.local.yml up -d --build
scripts/pruebas/perf.sh --sizes "1 16 64" --clients "1 4" --reps 2 --csv docs/pruebas/datos/25-rendimiento-local.csv
echo DFS_W=3 >> .env && docker compose -f compose.local.yml up -d
scripts/pruebas/perf.sh --sizes "1 16 64" --clients "1 4" --reps 2 --csv docs/pruebas/datos/25-rendimiento-local.csv
```

**AWS**, desde la máquina que despliega, con la rama `t25-pruebas-carga` ya en GitHub (las VMs toman el código de ahí). El CSV queda en el clon de la VM5, y la penúltima línea lo trae:

```bash
./infra/deploy.sh --ref t25-pruebas-carga --times scripts/pruebas/pruebas.env          # W=2
./infra/deploy.sh --check                                      # y esperar 60 s (DFS_LEADER_GRACE_S)
./infra/ssh.sh vm5 scripts/pruebas/perf.sh --env aws --csv docs/pruebas/datos/25-rendimiento-aws.csv
DFS_W=3 ./infra/deploy.sh --ref t25-pruebas-carga --times scripts/pruebas/pruebas.env  # W=3
./infra/deploy.sh --check
./infra/ssh.sh vm5 scripts/pruebas/perf.sh --env aws --csv docs/pruebas/datos/25-rendimiento-aws.csv
./infra/deploy.sh --ref t25-pruebas-carga --times scripts/pruebas/pruebas.env          # vuelve a W=2
./infra/ssh.sh vm5 cat docs/pruebas/datos/25-rendimiento-aws.csv > docs/pruebas/datos/25-rendimiento-aws.csv
python3 scripts/pruebas/perf_summary.py docs/pruebas/datos/25-rendimiento-aws.csv
```

Control opcional "en frío" (sección 4.4), dentro de una sesión `./infra/ssh.sh vm5`:

```bash
scripts/pruebas/perf.sh --env aws --cold --sizes 512 --csv docs/pruebas/datos/25-rendimiento-aws.csv
```

### 4.2 Cómo mide `scripts/pruebas/perf.sh`

- **Contenido nuevo en cada repetición y en cada cliente**, con `head -c NM /dev/urandom`. Los bloques se nombran por el SHA-256 de su contenido (DA-02). Si se subiera dos veces el mismo archivo, el segundo `put` no transferiría nada: la reserva respondería `present` para todos los bloques, y la medición saldría falsa.
- **Una repetición = put, get, comparación del SHA-256 y `rm`.** El SHA-256 se calcula en el contenedor cliente, antes del `put` y después del `get`.
- **4 clientes a la vez:** son 4 procesos `dfs` en el contenedor cliente de la VM5, lanzados juntos. Cada uno tiene su propia sesión (`HOME` distinto, con su `session.json`), su archivo local (`c1.bin` a `c4.bin`) y su ruta remota (`/perf-<corrida>-<MiB>m-n<clientes>-r<rep>-c<cliente>.bin`). Así no comparten ni lock ni archivo. Todos usan el mismo usuario, `perf`.
- **Tiempos:** "segundos" es lo que imprime el propio `dfs put` o `dfs get`. Va desde el inicio del comando hasta el `commit` o hasta el último byte verificado, e incluye el cálculo del SHA-256 de los bloques en el `put`. No incluye el arranque de Python ni de `docker exec` (≈ 1 s, sección 5.3). MiB/s es el tamaño entre esos segundos.
- **Agregado con 4 clientes:** la fila `cliente=todos` del CSV divide el total de MiB entre el tiempo del cliente más lento. Supone que los 4 arrancan a la vez: se lanzan en el mismo instante, y el desfase es de décimas de segundo. Ese desfase solo pesa con 1 MiB, donde lo que importa es la latencia y no el throughput. En pantalla se muestra también el tiempo de pared, con los arranques incluidos.
- **Calentamiento:** antes de la primera repetición de cada condición se corre una más, que va al CSV con `repeticion=0` y no entra en el resumen. En la tarea 24, el primer `get` de 512 MiB tardó 8,98 s y los siguientes 5,6 s (`datos/24-humo-aws.csv`).
- **Orden intercalado:** primero la repetición 1 de todas las condiciones, después la 2, y así. Las t3.small tienen créditos de CPU y de red que se gastan con el uso sostenido. Si cada condición se corriera entera antes de la siguiente, la última pagaría el agotamiento. Intercalando, el efecto se reparte. La hora de cada fila queda en la columna `fecha`, para ver si los tiempos crecen a lo largo de la corrida.

### 4.3 Disco

Cada repetición borra sus archivos remotos apenas termina el `get`. Pero `dfs rm` no borra los bloques en el acto: la limpieza del líder (DA-02) solo los borra cuando ninguna versión los usa y su clave lleva `DFS_GC_MIN_AGE_S` sin cambios.

- Con los tiempos normales (3600 s), nada se liberaría durante la corrida. Una pasada completa con un valor de W sube 14,4 GiB: 2,9 GiB con 1 cliente y 11,5 GiB con 4. Con 3 copias repartidas en 4 nodos, son unos **10,8 GiB por DataNode**, y cada disco es de 20 GiB, con el sistema y las imágenes de Docker adentro.
- Con `pruebas.env` (120 s, y una limpieza cada 30 s), un bloque se libera unos 2,5 a 3 minutos después del `rm`. Como una repetición con todas las condiciones dura unos 3 minutos, quedan como mucho una o dos repeticiones sin limpiar: unos 2 a 4 GiB por DataNode.
- Antes de cada repetición, `perf.sh` lee el espacio libre de cada DataNode en `dfs status`. Si alguno tiene menos de `--min-free-gib` (4 GiB por defecto), espera a la limpieza, revisando cada 30 s durante a lo más 15 min.
- Al terminar, conviene revisar `df -h /` en un DataNode: `./infra/ssh.sh vm4 df -h /`.

### 4.4 Caché de página

Sin `--cold`, el `get` corre justo después del `put`. Los bloques se acaban de escribir y es probable que sigan en la caché de página de los DataNodes. Entonces el `get` mide red, descifrado y verificación, pero no la lectura del disco gp3. Por eso la medición principal se declara **"en caliente"** (columna `cache`). Ese es el caso más común en este sistema, donde se lee poco después de escribir.

Desde la VM5 no se puede vaciar la caché de otra VM. El control `--cold` lo resuelve con un paso a mano:

1. sube las 5 repeticiones sin bajarlas;
2. se detiene y muestra el comando que vacía la caché de vm1 a vm4, que se corre desde la máquina que despliega:

   ```bash
   for vm in vm1 vm2 vm3 vm4; do ./infra/ssh.sh $vm "sudo sh -c 'sync; echo 3 > /proc/sys/vm/drop_caches'"; done
   ```

3. después de Enter, baja las 5 y compara el SHA-256.

Esas filas van al CSV con `cache=frio`. La diferencia entre las medianas del `get` en frío y en caliente con 512 MiB es lo que pesa la caché.

### 4.5 Percentil 95 con 5 repeticiones

`scripts/pruebas/perf_summary.py` calcula el p95 por **rango más cercano**: ordena los n tiempos y toma el de la posición ⌈0,95 · n⌉.

- Con 1 cliente, n = 5 y ⌈4,75⌉ = 5, así que **el p95 es el máximo**, la peor de las 5 repeticiones.
- Con 4 clientes, n = 20 (4 clientes × 5 repeticiones) y ⌈19⌉ = 19, así que el p95 es el penúltimo valor.

Con tan pocas muestras, el p95 no estima bien la cola de la distribución. Se reporta como lo que es: la peor repetición (o casi), y sirve para ver si hubo una repetición anómala. La mediana (el valor 3 de 5) es la cifra estable. Interpolar, como hace `statistics.quantiles`, inventaría un valor entre dos muestras sin mejorar la estimación, así que no se usa.

## 5. Resultado esperado y resultado obtenido

**Esperado:**

- El SHA-256 coincide en todas las repeticiones y el script termina en `RESULTADO: PASA`.
- El throughput crece con el tamaño: con 1 MiB pesa la latencia fija del lock, la reserva y el commit; con 64 y 512 MiB, los bloques viajan en paralelo.
- Con 4 clientes, el throughput de cada cliente baja pero el agregado sube, hasta que se satura algo: la red de la VM5, la CPU de los DataNodes que cifran o los discos.
- W=3 hace el `put` más lento que W=2, porque el primario espera a los 2 secundarios en vez de a 1 (DA-04). El `get` no cambia, porque lee una sola copia.

### 5.1 Ensayo local

`datos/25-rendimiento-local.csv`, resumido con `python3 scripts/pruebas/perf_summary.py`. Son 2 repeticiones por condición, así que el p95 es el máximo de 2 (con 1 cliente) o de 8 (con 4). Es un ensayo del método, no la medición.

| Entorno | W | Caché | Operación | MiB | Clientes | n | Mediana (s) | p95 (s) | MiB/s por cliente (mediana) | MiB/s agregado (mediana) | SHA-256 |
|---|---|---|---|---|---|---|---|---|---|---|---|
| local | 2 | caliente | put | 1 | 1 | 2 | 0,22 | 0,29 | 5,1 | - | - |
| local | 2 | caliente | get | 1 | 1 | 2 | 0,04 | 0,04 | 26,6 | - | 2/2 |
| local | 2 | caliente | put | 1 | 4 | 8 | 0,32 | 0,42 | 3,2 | 10,7 | - |
| local | 2 | caliente | get | 1 | 4 | 8 | 0,07 | 0,12 | 14,3 | 45,2 | 8/8 |
| local | 2 | caliente | put | 16 | 1 | 2 | 0,36 | 0,39 | 44,0 | - | - |
| local | 2 | caliente | get | 16 | 1 | 2 | 0,13 | 0,13 | 122,3 | - | 2/2 |
| local | 2 | caliente | put | 16 | 4 | 8 | 1,06 | 1,15 | 15,2 | 59,9 | - |
| local | 2 | caliente | get | 16 | 4 | 8 | 0,30 | 0,36 | 54,4 | 185,9 | 8/8 |
| local | 2 | caliente | put | 64 | 1 | 2 | 0,97 | 1,00 | 65,5 | - | - |
| local | 2 | caliente | get | 64 | 1 | 2 | 0,39 | 0,41 | 164,4 | - | 2/2 |
| local | 2 | caliente | put | 64 | 4 | 8 | 3,08 | 3,54 | 20,9 | 77,7 | - |
| local | 2 | caliente | get | 64 | 4 | 8 | 1,29 | 1,48 | 49,7 | 185,7 | 8/8 |
| local | 3 | caliente | put | 1 | 1 | 2 | 0,23 | 0,31 | 4,8 | - | - |
| local | 3 | caliente | get | 1 | 1 | 2 | 0,04 | 0,05 | 24,2 | - | 2/2 |
| local | 3 | caliente | put | 1 | 4 | 8 | 0,34 | 0,94 | 3,0 | 8,1 | - |
| local | 3 | caliente | get | 1 | 4 | 8 | 0,06 | 0,08 | 16,1 | 53,5 | 8/8 |
| local | 3 | caliente | put | 16 | 1 | 2 | 0,38 | 0,40 | 43,3 | - | - |
| local | 3 | caliente | get | 16 | 1 | 2 | 0,14 | 0,14 | 113,7 | - | 2/2 |
| local | 3 | caliente | put | 16 | 4 | 8 | 1,60 | 2,23 | 11,5 | 45,7 | - |
| local | 3 | caliente | get | 16 | 4 | 8 | 0,29 | 0,37 | 54,1 | 193,2 | 8/8 |
| local | 3 | caliente | put | 64 | 1 | 2 | 1,18 | 1,42 | 57,0 | - | - |
| local | 3 | caliente | get | 64 | 1 | 2 | 0,39 | 0,41 | 163,8 | - | 2/2 |
| local | 3 | caliente | put | 64 | 4 | 8 | 3,31 | 3,95 | 19,4 | 73,5 | - |
| local | 3 | caliente | get | 64 | 4 | 8 | 1,30 | 1,42 | 49,3 | 195,1 | 8/8 |

Las dos corridas terminaron en `RESULTADO: PASA`, con los 80 SHA-256 iguales. Cada una tardó unos 2 min 15 s. Con W=3, el `put` es más lento, sobre todo con 4 clientes (16 MiB: de 59,9 a 45,7 MiB/s agregados), y el `get` no cambia, como se esperaba.

**Caso grande** (no está en el CSV): con W=3 se corrió una vez 512 MiB, con 1 y con 4 clientes, para comprobar que los 4 × 512 MiB caben en memoria y disco y para estimar los tiempos de AWS. Con 1 cliente: `put` 7,07 s (72,4 MiB/s) y `get` 4,09 s (125,3 MiB/s). Con 4 a la vez: `put` de 2048 MiB en 25,3 s (80,9 MiB/s agregados) y `get` en 10,7 s (191,4 MiB/s). Los SHA-256 coincidieron. La repetición entera de 4 × 512 MiB, contando la generación de los archivos y los SHA-256, tardó unos 70 s.

**Comparación con la tarea 22.** La medición local de referencia (`datos/22-referencia-local.csv`, 512 MiB, W=2, 5 repeticiones) dio medianas de 23,4 MiB/s en `put` y 43,5 MiB/s en `get`. Aquí, con 512 MiB, salió unas tres veces más rápido. La diferencia es de máquina, no de código: el commit de la tarea 22 (`900c790`) y este tienen el mismo camino de `put` y `get`, y la tarea 22 se midió en otro PC, con Docker Desktop en Windows. En local, los 4 DataNodes, los 3 MetaServers y el cliente comparten la CPU y el disco de un solo PC, así que esas cifras sirven para comparar condiciones entre sí, no para predecir AWS.

### 5.2 AWS

*Pendiente de la corrida en AWS: el resumen de `datos/25-rendimiento-aws.csv` con W=2 y W=3, la hora de inicio y de fin de cada corrida, `df -h /` en un DataNode al final y, si se hace, el control en frío.*

### 5.3 Lo que se corrigió en el ensayo

En el primer ensayo, la fila "todos" usaba el tiempo de pared. Con 1 MiB y 4 clientes, cada `get` tardaba 0,07 s según `dfs`, pero la pared marcaba 0,97 s: casi todo era el arranque de `docker exec` y de Python, y el agregado salía en 4 MiB/s, cuando en realidad es de unos 45. Desde entonces, el agregado usa el tiempo del cliente más lento según `dfs`, y la pared solo se muestra en pantalla. Los datos de `datos/25-rendimiento-local.csv` son de después del cambio.

## 6. Conclusión

*Pendiente de la corrida en AWS.* En local, el método funciona: contenido nuevo en cada repetición, SHA-256 verificado siempre, 4 clientes sin pisarse y W cambiado sin tocar el código. Las tendencias ya se ven: el throughput sube con el tamaño, el agregado sube con 4 clientes, y W=3 solo encarece el `put`.

## 7. Para el video

1. El comando de `perf.sh` corriendo en la VM5, con las líneas de `put` y `get` de una repetición de 4 clientes.
2. La tabla del resumen con W=2 y W=3, señalando el `put` con 4 clientes (W) y la fila de 512 MiB (tamaño).
3. Una frase sobre el p95: con 5 repeticiones es la peor de las 5.
4. Si se hizo el control en frío: el `get` de 512 MiB en caliente frente a en frío.
