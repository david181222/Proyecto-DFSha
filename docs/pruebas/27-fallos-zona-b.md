# 27 · Fallos: zona b

## 1. Objetivo

Detener la zona b completa (VM2 y VM4: etcd2, meta2, dn2 y dn4) y comprobar que:

1. se sigue leyendo un archivo que ya estaba y se sigue escribiendo uno nuevo, con 2 copias (dn1 y dn3);
2. lo escrito sin la zona b queda en dn1 y dn3, en 2 zonas (G1, DA-05);
3. con la zona caída no hay forma de llegar a 3 copias. La tercera vuelve cuando vuelve la zona, y el clúster queda sano.

## 2. Qué comprueba

| Comprobación | Garantía o decisión | Fila de la Tabla 21 |
|---|---|---|
| Lectura sin la zona b, también después de `--hold` segundos | G5 | Fallos: zona b |
| Escritura con 2 DataNodes vivos; bloques en dn1 y dn3 | G1, DA-05 (si quedan 2 nodos en zonas distintas, el bloque va a esos 2) | Fallos: zona b |
| La tercera copia vuelve con la zona | DA-05, DA-12 | Fallos: zona b |

## 3. Entorno

| | Ensayo local | AWS |
|---|---|---|
| Dónde | `compose.local.yml` en un portátil con Windows 11 y Docker Desktop 29.1.2; el script corre en Git Bash | *(se llena en la corrida)* |
| Commit | rama `t27-pruebas-fallos`, sobre `main` en `b6e4d2b`, antes de su primer commit | *(se llena en la corrida)* |
| DA-16 | normales, salvo `DFS_LEADER_GRACE_S=60` (en el `.env` local) | normales de la Tabla 19 |
| Cómo se provoca el fallo | `docker compose stop etcd2 meta2 dn2 dn4` | `./infra/vm.sh stop vm2 vm4` |

Con la zona b caída quedan etcd1 y etcd3, que son mayoría de 3; meta1 y meta3; y dn1 (zona a) y dn3 (zona c). Si el líder era meta2, gana otro, y su re-réplica espera `DFS_LEADER_GRACE_S` (DA-12).

## 4. Pasos

```bash
# Local
scripts/pruebas/fallos.sh zona-b                 # --hold 90 por defecto

# AWS (WSL)
scripts/pruebas/fallos.sh zona-b --env aws
```

Lo que hace `fallos.sh zona-b`:

1. Arranca la línea de tiempo, espera el clúster sano, anota el líder y sube un archivo A.
2. Detiene la zona b y espera ver dn2 y dn4 caídos.
3. Baja A y compara el SHA-256. Sube otro archivo B, revisa con `dfs stat --blocks` que cada bloque esté en dn1 y dn3, y baja B.
4. Mantiene la zona caída `--hold` segundos (90) y vuelve a bajar A.
5. Enciende la zona y espera 0 bloques con menos de 3 copias; revisa que B tenga 3 copias vivas y que el clúster quede sano.

## 5. Resultado esperado y resultado obtenido

**Esperado:**

- Lectura y escritura sin errores. Los bloques de B quedan con 2 copias, en dn1 y dn3.
- Mientras dura la caída, "menos de 3 copias" no baja de lo que había. El líder solo puede copiar entre dn1 y dn3 lo que le falta a cada uno.
- Al volver la zona, las terceras copias se reponen y se borran las que sobran.

### 5.1 Ensayo local (128 MiB)

Se corrió cuatro veces: la primera y la última pasaron; la segunda y la tercera fallaron en la escritura (sección 5.2). La última, con el script final, de `docs/pruebas/datos/27-zona-b-local.csv`:

```text
18:43:39  >> deteniendo la zona b: etcd2 meta2 dn2 dn4
18:43:42  >> zona b detenida
18:43:44  vivos=dn1,dn3 caídos=dn2,dn4 líder=meta1 menos_de_3=242 copy=0 delete=0
18:44:12  >> empieza escritura con la zona b caída: put de /fallos-zb-b-20261107-134246.bin
18:44:23  vivos=dn1,dn3 caídos=dn2,dn4 líder=meta1 menos_de_3=258 copy=0 delete=0
18:44:25  >> termina escritura con la zona b caída: bien
18:45:05  vivos=dn1,dn3 caídos=dn2,dn4 líder=meta1 menos_de_3=258 copy=227 delete=0
18:46:20  vivos=dn1,dn3 caídos=dn2,dn4 líder=meta1 menos_de_3=258 copy=100 delete=0
18:46:25  >> arrancando la zona b: etcd2 meta2 dn2 dn4
18:46:40  vivos=dn1,dn2,dn3,dn4 caídos=- líder=meta1 menos_de_3=8 copy=86 delete=136
18:46:45  >> zona b encendida otra vez
18:47:30  vivos=dn1,dn2,dn3,dn4 caídos=- líder=meta1 menos_de_3=0 copy=0 delete=18
18:47:43  vivos=dn1,dn2,dn3,dn4 caídos=- líder=meta1 menos_de_3=0 copy=0 delete=0
```

```text
PASA   lectura con la zona b caída: get de /fallos-zb-a-20261107-134246.bin terminó bien y el SHA-256 es igual al original (edb2f0a54c5f74cc...)
PASA   escritura con la zona b caída: /fallos-zb-b-20261107-134246.bin: versión 1, 128.0 MiB en 16 bloques (16 enviados, 0 ya estaban), 12.07 s, 10.6 MiB/s
PASA   escritura con la zona b caída: G1, los 16 bloques tienen copias vivas en al menos 2 zonas (0 con 3 copias)
PASA   los 16 bloques nuevos quedaron con 2 copias, en dn1 y dn3 (DA-05)
PASA   lectura de lo escrito sin la zona b: get de /fallos-zb-b-20261107-134246.bin terminó bien y el SHA-256 es igual al original (57129c17df574011...)
PASA   lectura después de 90 s sin la zona b: get de /fallos-zb-a-20261107-134246.bin terminó bien y el SHA-256 es igual al original (edb2f0a54c5f74cc...)
PASA   las copias que faltaban volvieron a las 18:47:30: 65 s después de empezar a encender la zona b (18:46:25) y 45 s después de que respondió (18:46:45)
PASA   archivo escrito sin la zona b: los 16 bloques de /fallos-zb-b-20261107-134246.bin tienen 3 copias vivas
PASA   sano otra vez a las 18:47:49: 4 DataNodes vivos, líder meta1, 0 bloques con menos de 3 copias, 0 órdenes
PASA   los 11 contenedores corren y los 3 etcd están healthy
RESULTADO: PASA (0 avisos)
```

Cómo se lee:

- **"Menos de 3 copias".** Son 242 bloques al caer la zona: los de este ensayo y los de archivos borrados en corridas anteriores, que la limpieza todavía no quita (`DFS_GC_MIN_AGE_S=3600`). Sube a 258 con los 16 de B y no baja mientras dura la caída.
- **18:45:05.** Son 81 s después de ver la zona caída. El líder ordena 227 copias entre dn1 y dn3, para los bloques que solo uno de los dos tenía. Así quedan con 2 copias, pero no pueden llegar a 3.
- **Al volver la zona.** En 45 s se reponen las terceras copias y se borran las que sobran.

### 5.2 Las dos corridas que fallaron

En las corridas de las 18:22 y de las 18:28, la lectura pasó y la escritura de B falló:

```text
FALLA  escritura con la zona b caída: error E_UNAVAILABLE: ningún MetaServer respondió
```

Los logs de esa escritura (18:29) muestran la cadena completa:

```text
dn1    18:29:22.883  el secundario 10.88.0.33:9444 no respondió en 10 s
dn1    18:29:22.975  "PUT /api/v1/blocks/9a4d6293... HTTP/1.1" 422 Unprocessable Entity
meta1  18:29:22.994  "POST /api/v1/uploads/7afa4ef1-.../realloc HTTP/1.1" 503 Service Unavailable
meta3  18:29:23.028  "POST /api/v1/uploads/7afa4ef1-.../realloc HTTP/1.1" 503 Service Unavailable
```

Paso a paso:

1. Con solo dn1 y dn3 vivos, cada bloque va a esos dos. Para uno, dn3 no respondió como secundario en `DFS_PRIMARY_WAIT_S` (10 s), y dn1 respondió 422 `E_REPLICAS`: una copia no alcanza para W=2.
2. El cliente pidió `realloc` sin el primario que falló, como dicen el contrato y `src/dfs_client/transfer.py`. Sin dn1 solo queda dn3, que no cubre 2 zonas, así que el MetaServer respondió 503 `E_UNAVAILABLE` ("no hay 2 DataNodes vivos que cubran 2 zonas", `src/metaserver/placement.py`).
3. El cliente trata un 503 como un MetaServer que no responde (`_send_to_any` en `src/dfs_client/api.py`). Probó los MetaServers, volvió a pedir la lista y los probó otra vez (dos 503 en meta1 y dos en meta3). Terminó con "ningún MetaServer respondió". Ese mensaje no es la causa.

Por qué dn3 no respondió en 10 s no está comprobado. En esas corridas, las muestras de `dfs status` llegaban cada 6 a 19 s, cuando lo normal es cada 6 a 7 s, y el líder estaba copiando. Lo más probable es la carga del portátil. Después, con la zona b todavía caída y sin la línea de tiempo, 3 `put` a mano con datos nuevos pasaron: 2 de 16 MiB y 1 de 128 MiB. La corrida final también pasó.

### 5.3 AWS (512 MiB)

*Pendiente de la corrida en AWS.*

## 6. Conclusión

En el ensayo local, con la zona b caída la lectura funcionó siempre. La escritura funcionó en 2 de 4 corridas. Cuando funciona, deja cada bloque en dn1 y dn3, como dice DA-05, y la tercera copia vuelve con la zona. Cuando falla, es porque con solo 2 DataNodes un secundario lento hace fallar el `put` completo (Pendientes). G1 nunca se rompió: el `put` que falló no se publicó. *La conclusión sobre AWS se llena con la corrida.*

## 7. Para el video

1. `dfs status` con el líder y los 4 DataNodes; `./infra/vm.sh stop vm2 vm4`.
2. La línea de tiempo: dn2 y dn4 caídos, y "menos de 3 copias" que sube y no baja.
3. `dfs get` del archivo de antes, `dfs put` de uno nuevo y `dfs stat --blocks`, con cada bloque en dn1 y dn3.
4. `./infra/vm.sh start vm2 vm4` y la línea de tiempo hasta "Bloques con menos de 3 copias vivas: 0".

## Pendientes

- **Con solo 2 DataNodes vivos, un secundario lento hace fallar el `put`** (sección 5.2). El cliente pide `realloc` sin el primario ante un 422 `E_REPLICAS`, y con 2 nodos eso deja uno solo.
  - Cualquiera de estas dos opciones lo evitaría: volver a intentar con los mismos dos nodos cuando el `realloc` no tiene a dónde ir, o no excluir al primario cuando el que falló fue el secundario.
  - Toca el cliente y quizá la sección 7.2 del contrato. Lo decide el equipo.
- **El cliente esconde el error real detrás de "ningún MetaServer respondió".** `_send_to_any` (`src/dfs_client/api.py`) salta toda respuesta 503 como si el MetaServer no hubiera respondido, también cuando trae el cuerpo `{error, message, retryable}` del contrato. Así se pierde "no hay 2 DataNodes vivos que cubran 2 zonas". Saltar solo los 503 sin ese cuerpo, o guardar el último y mostrarlo, daría el mensaje correcto.
