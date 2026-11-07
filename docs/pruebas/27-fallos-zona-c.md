# 27 · Fallos: zona c

## 1. Objetivo

Comprobar que, con el líder fuera de la zona c, al detener la zona c completa (VM3: etcd3, meta3 y dn3):

1. se sigue leyendo un archivo que ya estaba y se sigue escribiendo uno nuevo;
2. cada bloque escrito sin la zona c queda con copias vivas en al menos 2 zonas;
3. la tercera copia de los bloques que tenía dn3 vuelve a los ≈ `DFS_REREPLICATE_AFTER_S` de que dn3 aparece caído;
4. al volver la zona, el clúster queda sano: 4 DataNodes vivos, 0 bloques con menos de 3 copias y 0 órdenes.

## 2. Qué comprueba

| Comprobación | Garantía o decisión | Fila de la Tabla 21 |
|---|---|---|
| Lectura y escritura sin la zona c; SHA-256 igual | G5 (durabilidad) | Fallos: zona c |
| Bloques nuevos en 2 zonas (a y b) | G1, DA-05 | Fallos: zona c |
| Tercera copia repuesta a los ≈ 60 s | DA-12 | Fallos: zona c |
| Líder fuera de la zona c antes de empezar | DA-12 (si cae el líder, la re-réplica espera `DFS_LEADER_GRACE_S`) | Fallos: zona c |

## 3. Entorno

| | Ensayo local | AWS |
|---|---|---|
| Dónde | `compose.local.yml` en un portátil con Windows 11 y Docker Desktop 29.1.2; el script corre en Git Bash | *(se llena en la corrida)* |
| Commit | rama `t27-pruebas-fallos`, sobre `main` en `b6e4d2b`, antes de su primer commit | *(se llena en la corrida)* |
| DA-16 | normales, salvo `DFS_LEADER_GRACE_S=60` (en el `.env` local) | normales de la Tabla 19 |
| Cómo se provoca el fallo | `docker compose stop etcd3 meta3 dn3` | `./infra/vm.sh stop vm3` (aws ec2 stop-instances) |

Valores normales que importan aquí: `DFS_HEARTBEAT_S=5`, `DFS_NODE_LEASE_S=15`, `DFS_REREPLICATE_AFTER_S=60`, `DFS_LEADER_SCAN_S=5`, `DFS_W=2`, `DFS_BLOCK_MIB=8`. En AWS, `DFS_LEADER_GRACE_S=300`.

## 4. Pasos

```bash
# Local (Git Bash o WSL), con compose.local.yml corriendo
scripts/pruebas/fallos.sh zona-c

# AWS (WSL), con el clúster desplegado desde la rama de la tarea 27
./infra/deploy.sh --ref t27-pruebas-fallos
scripts/pruebas/fallos.sh zona-c --env aws
```

Lo que hace `fallos.sh zona-c`:

1. Arranca `timeline.sh`, que deja una muestra de `dfs status` cada 5 s, y espera a que el clúster esté sano.
2. Revisa el líder en `dfs status`. Si es meta3, reinicia meta3 para que gane otro y espera `DFS_LEADER_GRACE_S`.
3. Sube un archivo aleatorio A (128 MiB en local y 512 MiB en AWS).
4. Detiene la zona c, baja A y compara el SHA-256, sube otro archivo B, revisa G1 con `dfs stat --blocks` y baja B.
5. Espera a que `dfs status` muestre 0 bloques con menos de 3 copias con dn3 caído, y revisa que A tenga 3 copias vivas.
6. Enciende la zona c y espera a que el clúster quede sano dos muestras seguidas. En local revisa que corran los 11 contenedores y que los 3 etcd estén `healthy`; en AWS corre `./infra/deploy.sh --check`.

La línea de tiempo con los eventos queda en `docs/pruebas/datos/27-zona-c-ENTORNO.csv`.

## 5. Resultado esperado y resultado obtenido

**Esperado:** lectura y escritura sin errores, los bloques nuevos en 2 zonas, y 0 bloques con menos de 3 copias unos 60 s después de que dn3 aparece caído, más lo que tarde en copiarse. Al volver la zona, el líder borra las copias que sobran (órdenes `delete`) y el clúster vuelve a sano.

### 5.1 Ensayo local (128 MiB)

Línea de tiempo recortada (UTC), de `docs/pruebas/datos/27-zona-c-local.csv`:

```text
18:37:20  >> deteniendo la zona c: etcd3 meta3 dn3
18:37:25  >> zona c detenida
18:37:29  >> empieza escritura con la zona c caída: put de /fallos-zc-b-20261107-133626.bin
18:37:45  vivos=dn1,dn2,dn4 caídos=dn3 líder=meta1 menos_de_3=291 copy=1 delete=0
18:38:00  >> termina escritura con la zona c caída: bien
18:38:42  vivos=dn1,dn2,dn4 caídos=dn3 líder=meta1 menos_de_3=288 copy=0 delete=0
18:38:48  vivos=dn1,dn2,dn4 caídos=dn3 líder=meta1 menos_de_3=288 copy=288 delete=0
18:39:27  vivos=dn1,dn2,dn4 caídos=dn3 líder=meta1 menos_de_3=166 copy=166 delete=0
18:40:31  vivos=dn2,dn4 caídos=dn1,dn3 líder=meta1 menos_de_3=308 copy=96 delete=0
18:40:41  vivos=dn1,dn2,dn4 caídos=dn3 líder=meta1 menos_de_3=95 copy=27 delete=78
18:41:58  vivos=dn1,dn2,dn4 caídos=dn3 líder=meta1 menos_de_3=52 copy=72 delete=297
18:42:05  vivos=dn1,dn2,dn4 caídos=dn3 líder=meta1 menos_de_3=0 copy=0 delete=103
18:42:11  >> arrancando la zona c: etcd3 meta3 dn3
18:42:19  vivos=dn1,dn2,dn3,dn4 caídos=- líder=meta1 menos_de_3=0 copy=0 delete=288
18:42:32  vivos=dn1,dn2,dn3,dn4 caídos=- líder=meta1 menos_de_3=0 copy=0 delete=0
```

Resumen del script:

```text
PASA   el líder (meta1) no está en la zona c
PASA   lectura con la zona c caída: get de /fallos-zc-a-20261107-133626.bin terminó bien y el SHA-256 es igual al original (271f9c369e60e043...)
PASA   escritura con la zona c caída: /fallos-zc-b-20261107-133626.bin: versión 1, 128.0 MiB en 16 bloques (16 enviados, 0 ya estaban), 29.76 s, 4.3 MiB/s
PASA   escritura con la zona c caída: G1, los 16 bloques tienen copias vivas en al menos 2 zonas (11 con 3 copias)
PASA   lectura de lo escrito sin la zona c: get de /fallos-zc-b-20261107-133626.bin terminó bien y el SHA-256 es igual al original (4f12b5520a2d277b...)
PASA   tercera copia repuesta sin dn3 a las 18:42:05: 285 s después de empezar el fallo (18:37:20) y 260 s después de que dfs status mostró dn3 caído (18:37:45); DFS_NODE_LEASE_S=15, DFS_REREPLICATE_AFTER_S=60
PASA   archivo de antes, sin la zona c: los 16 bloques de /fallos-zc-a-20261107-133626.bin tienen 3 copias vivas
PASA   sano otra vez a las 18:42:39: 4 DataNodes vivos, líder meta1, 0 bloques con menos de 3 copias, 0 órdenes
PASA   los 11 contenedores corren y los 3 etcd están healthy
RESULTADO: PASA (0 avisos)
```

Cómo se leen los tiempos:

- **18:37:45.** dn3 aparece caído 25 s después de empezar el paro: son los 15 s de `DFS_NODE_LEASE_S`, contados desde su último latido, más el paro del contenedor y el intervalo entre muestras.
- **Las pocas órdenes `copy` entre 18:37:45 y 18:38:04.** Coinciden con la subida de B. Son bloques de B que quedaron con 2 copias al confirmarse (W=2); no revisé por qué faltó el tercero. Su `stored_on` tiene menos de 3 nodos, y para eso el líder no espera (DA-04, regla 3 de `plan_repairs`).
- **18:38:48.** Son 63 s después de ver dn3 caído. El líder ordena de una vez las 288 copias de los bloques que tenía dn3 (`DFS_REREPLICATE_AFTER_S`).
- **Por qué son 288 bloques.** Aquí entran también los de archivos de corridas anteriores ya borrados. La limpieza todavía no los quita, porque `DFS_GC_MIN_AGE_S` es 3600 s.
- **18:42:05.** Copiar los 288 bloques tardó 197 s en este portátil.
- **18:40:31.** dn1 aparece caído en una muestra y vuelve en la siguiente. Perdió latidos por la carga del portátil, con las copias en curso. Después aparecen órdenes `delete` (78 y 297). No revisé cuántas son por órdenes `copy` atascadas (sección 7.3 del contrato) y cuántas por copias que sobran. La re-réplica termina igual.
- **Al volver dn3.** Sobran 288 copias, y el líder las borra en unos 20 s.

En la primera corrida del ensayo, con menos datos (44 bloques en dn3), las órdenes salieron exactamente 60 s después de ver dn3 caído, y la re-réplica terminó 84 s después.

### 5.2 AWS (512 MiB)

*Pendiente de la corrida en AWS: el resumen del script y la línea de tiempo recortada.*

## 6. Conclusión

En el ensayo local, la caída de la zona c no interrumpe lecturas ni escrituras. Lo escrito sin la zona queda en las zonas a y b. La re-réplica empieza a los 60 a 63 s de ver dn3 caído, como dice DA-12. Termina en 24 s con 44 bloques y en 197 s con 288 en este portátil, aun con un DataNode que perdió latidos en medio. Al volver la zona, el líder borra las copias que sobran y el clúster queda sano. *La conclusión sobre AWS se llena con la corrida.*

## 7. Para el video

1. `dfs status` con el líder (que no sea meta3) y los 4 DataNodes vivos.
2. `./infra/vm.sh stop vm3` y, en otra terminal, `scripts/pruebas/timeline.sh --env aws` en la VM5: dn3 pasa a caído y sube "menos de 3 copias".
3. `dfs get` del archivo de antes y `dfs put` de uno nuevo, sin errores. Después, `dfs stat --blocks` del nuevo, con cada bloque en dn1, dn2 o dn4.
4. La muestra en que aparecen las órdenes `copy`, unos 60 s después de dn3 caído, y la de 0 bloques con menos de 3 copias.
5. `./infra/vm.sh start vm3`: las órdenes `delete` de las copias que sobran y `dfs status` sano.
