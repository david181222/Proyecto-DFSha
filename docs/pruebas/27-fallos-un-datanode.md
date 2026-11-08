# 27 · Fallos: un DataNode

## 1. Objetivo

Detener dn4 en medio de un `get` y en medio de un `put`, y comprobar que:

1. las dos operaciones terminan bien y el SHA-256 de lo leído es el original;
2. lo escrito con dn4 detenido queda con copias vivas en al menos 2 zonas (G1);
3. `dfs status` muestra la tercera copia repuesta a los ≈ `DFS_REREPLICATE_AFTER_S` de que dn4 aparece caído;
4. al volver dn4, el líder borra las copias que sobran y el clúster queda sano.

## 2. Qué comprueba

| Comprobación | Garantía o decisión | Fila de la Tabla 21 |
|---|---|---|
| `get` y `put` sin errores con dn4 detenido en medio | G5, DA-04 (`realloc` sin el primario que falló) | Fallos: un DataNode |
| Bloques escritos sin dn4 en 2 zonas | G1 | Fallos: un DataNode |
| Tercera copia a los ≈ 60 s de ver dn4 caído | DA-12 | Fallos: un DataNode |
| Copias que sobran borradas al volver dn4 | DA-12 (regla 7 de `plan_repairs`) | Fallos: un DataNode |

## 3. Entorno

| | Ensayo local | AWS |
|---|---|---|
| Dónde | `compose.local.yml` en un portátil con Windows 11 y Docker Desktop 29.1.2; el script corre en Git Bash | 5 VMs t3.small en us-east-1 (Learner Lab de Santiago, 7 de noviembre de 2026); `fallos.sh` corre en WSL y `timeline.sh` en la VM5 |
| Commit | rama `t27-pruebas-fallos`, sobre `main` en `b6e4d2b`, antes de su primer commit | `1e1d3b2` desplegado en las VMs con `./infra/deploy.sh --ref t27-pruebas-fallos`. `fallos.sh`, que corre en WSL, lleva un arreglo hecho antes de la primera corrida: su función `aws()` tapaba la AWS CLI y pasó a llamarse `on_aws()` |
| DA-16 | normales, salvo `DFS_LEADER_GRACE_S=60` (en el `.env` local) | normales de la Tabla 19 |
| Cómo se provoca el fallo | `docker compose stop dn4` | `docker compose stop dn` en la VM4 |

Valores que importan aquí: `DFS_HEARTBEAT_S=5`, `DFS_NODE_LEASE_S=15`, `DFS_REREPLICATE_AFTER_S=60`, `DFS_LEADER_SCAN_S=10`, `DFS_W=2`, `DFS_PRIMARY_WAIT_S=10`, `DFS_BLOCK_MIB=8`.

## 4. Pasos

```bash
# Local
scripts/pruebas/fallos.sh datanode                    # 128 MiB; --fault-after 2 por defecto

# AWS (WSL)
scripts/pruebas/fallos.sh datanode --env aws          # 512 MiB
```

Lo que hace `fallos.sh datanode`:

1. Arranca la línea de tiempo y espera el clúster sano. Sube un archivo aleatorio A.
2. Empieza `dfs get` de A en segundo plano y, a los 2 s, detiene dn4. Comprueba que el `get` seguía corriendo, que terminó bien y que el SHA-256 es el original. Arranca dn4 otra vez y espera verlo vivo.
3. Empieza `dfs put` de otro archivo B y, a los 2 s, detiene dn4. Comprueba que el `put` terminó bien, revisa G1 con `dfs stat --blocks` y baja B.
4. Con dn4 todavía detenido, espera 0 bloques con menos de 3 copias, y revisa que A y B tengan 3 copias vivas.
5. Arranca dn4 y espera el clúster sano dos muestras seguidas.

## 5. Resultado esperado y resultado obtenido

**Esperado:**

- **`get`.** Si una réplica no responde, el cliente pasa a la siguiente.
- **`put`.** Un bloque cuyo primario es dn4 se reenvía a otro con `realloc`. Si dn4 era secundario, el bloque se confirma con 2 copias.
- **Las copias.** Los bloques que quedaron con 2 copias al confirmarse se reponen enseguida. Los que tenía dn4 se reponen unos 60 s después de que dn4 aparece caído, más lo que tarde en copiarse.

### 5.1 Ensayo local (128 MiB)

Línea de tiempo recortada (UTC), de `docs/pruebas/datos/27-datanode-local.csv`:

```text
18:19:19  >> empieza get de /fallos-dn-a-20261107-131854.bin
18:19:22  >> deteniendo dn4
18:19:36  >> dn4 detenido (docker compose stop)
18:19:36  >> termina get de /fallos-dn-a-20261107-131854.bin (código 0)
18:19:38  >> dn4 arrancado otra vez
18:19:39  >> empieza put de /fallos-dn-b-20261107-131854.bin
18:19:41  >> deteniendo dn4
18:19:44  >> dn4 detenido (docker compose stop)
18:20:03  vivos=dn1,dn2,dn3 caídos=dn4 líder=meta1 menos_de_3=23 copy=4 delete=0
18:20:15  vivos=dn1,dn2,dn3 caídos=dn4 líder=meta1 menos_de_3=21 copy=2 delete=0
18:20:27  >> termina put de /fallos-dn-b-20261107-131854.bin (código 0)
18:20:56  vivos=dn1,dn2,dn3 caídos=dn4 líder=meta1 menos_de_3=19 copy=0 delete=0
18:21:02  vivos=dn1,dn2,dn3 caídos=dn4 líder=meta1 menos_de_3=19 copy=19 delete=0
18:21:10  vivos=dn1,dn2,dn3 caídos=dn4 líder=meta1 menos_de_3=10 copy=10 delete=0
18:21:17  vivos=dn1,dn2,dn3 caídos=dn4 líder=meta1 menos_de_3=0 copy=0 delete=0
18:21:25  >> dn4 arrancado otra vez
18:21:36  vivos=dn1,dn2,dn3,dn4 caídos=- líder=meta1 menos_de_3=0 copy=0 delete=19
18:21:42  vivos=dn1,dn2,dn3,dn4 caídos=- líder=meta1 menos_de_3=0 copy=0 delete=0
```

```text
PASA   put de /fallos-dn-a-20261107-131854.bin con todo vivo: /fallos-dn-a-20261107-131854.bin: versión 1, 128.0 MiB en 16 bloques (16 enviados, 0 ya estaban), 10.30 s, 12.4 MiB/s
PASA   get con dn4 detenido en medio: terminó bien y el SHA-256 es igual al original
PASA   put con dn4 detenido en medio: terminó bien: /fallos-dn-b-20261107-131854.bin: versión 1, 128.0 MiB en 16 bloques (16 enviados, 0 ya estaban), 46.93 s, 2.7 MiB/s
PASA   put con dn4 detenido: G1, los 16 bloques tienen copias vivas en al menos 2 zonas (15 con 3 copias)
PASA   lectura de lo escrito sin dn4: get de /fallos-dn-b-20261107-131854.bin terminó bien y el SHA-256 es igual al original (fe7a5ea42d4b7a1a...)
PASA   tercera copia repuesta sin dn4 a las 18:21:17: 96 s después de empezar el fallo (18:19:41) y 74 s después de que dfs status mostró dn4 caído (18:20:03); DFS_NODE_LEASE_S=15, DFS_REREPLICATE_AFTER_S=60
PASA   archivo de antes, sin dn4: los 16 bloques de /fallos-dn-a-20261107-131854.bin tienen 3 copias vivas
PASA   archivo escrito durante el fallo, sin dn4: los 16 bloques de /fallos-dn-b-20261107-131854.bin tienen 3 copias vivas
PASA   sano otra vez a las 18:21:42: 4 DataNodes vivos, líder meta1, 0 bloques con menos de 3 copias, 0 órdenes
PASA   los 11 contenedores corren y los 3 etcd están healthy
RESULTADO: PASA (0 avisos)
```

Cómo se leen los tiempos:

- **El `get`.** Terminó en el mismo segundo en que acabó de detenerse dn4 (14 s de `docker compose stop` en este portátil). Seguía corriendo cuando empezó el paro, si no el script habría dado un AVISO.
- **El `put` sin dn4.** Tardó 47 s contra 10 s con todo vivo. Se espera que tarde más, por los `realloc` de los bloques cuyo primario era dn4 y la espera de hasta 10 s por dn4 como secundario. No medí cuánto aporta cada cosa.
- **18:20:03 a 18:20:15.** Las órdenes `copy` de esos segundos son de bloques de B que se confirmaron con 2 copias. El líder las ordena sin esperar, porque su `stored_on` tiene menos de 3 nodos (regla 3 de `plan_repairs`).
- **18:21:02.** Son 59 s después de la primera muestra con dn4 caído. El líder ordena las 19 copias de los bloques que tenía dn4 (`DFS_REREPLICATE_AFTER_S`), y a las 18:21:17 ya están.
- **Al volver dn4.** Sobran 19 copias y el líder las borra en una vuelta.

En otras dos corridas del mismo ensayo, una de 128 MiB y otra de 32 MiB, la re-réplica terminó 70 y 80 s después de ver dn4 caído.

### 5.2 AWS (512 MiB)

Línea de tiempo recortada (UTC), de `docs/pruebas/datos/27-datanode-aws.csv`:

```text
20:15:03  >> empieza get de /fallos-dn-a-20261107-151425.bin
20:15:05  >> deteniendo dn4
20:15:17  >> dn4 detenido (docker compose stop)
20:15:17  >> termina get de /fallos-dn-a-20261107-151425.bin (código 0)
20:15:19  >> dn4 arrancado otra vez
20:15:30  >> empieza put de /fallos-dn-b-20261107-151425.bin
20:15:32  >> deteniendo dn4
20:15:34  >> dn4 detenido (docker compose stop)
20:15:40  vivos=dn1,dn2,dn3,dn4 caídos=- líder=meta3 menos_de_3=23 copy=0 delete=0
20:15:43  >> termina put de /fallos-dn-b-20261107-151425.bin (código 0)
20:15:46  vivos=dn1,dn2,dn3 caídos=dn4 líder=meta3 menos_de_3=80 copy=32 delete=0
20:15:53  vivos=dn1,dn2,dn3 caídos=dn4 líder=meta3 menos_de_3=48 copy=0 delete=0
20:16:41  vivos=dn1,dn2,dn3 caídos=dn4 líder=meta3 menos_de_3=48 copy=0 delete=0
20:16:47  vivos=dn1,dn2,dn3 caídos=dn4 líder=meta3 menos_de_3=48 copy=48 delete=0
20:16:53  vivos=dn1,dn2,dn3 caídos=dn4 líder=meta3 menos_de_3=32 copy=32 delete=0
20:16:59  vivos=dn1,dn2,dn3 caídos=dn4 líder=meta3 menos_de_3=0 copy=0 delete=0
20:17:12  >> dn4 arrancado otra vez
20:17:17  vivos=dn1,dn2,dn3,dn4 caídos=- líder=meta3 menos_de_3=0 copy=0 delete=48
20:17:23  vivos=dn1,dn2,dn3,dn4 caídos=- líder=meta3 menos_de_3=0 copy=0 delete=23
20:17:29  vivos=dn1,dn2,dn3,dn4 caídos=- líder=meta3 menos_de_3=0 copy=0 delete=0
```

```text
PASA   put de /fallos-dn-a-20261107-151425.bin con todo vivo: /fallos-dn-a-20261107-151425.bin: versión 1, 512.0 MiB en 64 bloques (64 enviados, 0 ya estaban), 11.07 s, 46.3 MiB/s
PASA   get con dn4 detenido en medio: terminó bien y el SHA-256 es igual al original
PASA   put con dn4 detenido en medio: terminó bien: /fallos-dn-b-20261107-151425.bin: versión 1, 512.0 MiB en 64 bloques (64 enviados, 0 ya estaban), 10.18 s, 50.3 MiB/s
PASA   put con dn4 detenido: G1, los 64 bloques tienen copias vivas en al menos 2 zonas (32 con 3 copias)
PASA   lectura de lo escrito sin dn4: get de /fallos-dn-b-20261107-151425.bin terminó bien y el SHA-256 es igual al original (93561723e98449b5...)
PASA   tercera copia repuesta sin dn4 a las 20:16:59: 87 s después de empezar el fallo (20:15:32) y 73 s después de que dfs status mostró dn4 caído (20:15:46); DFS_NODE_LEASE_S=15, DFS_REREPLICATE_AFTER_S=60
PASA   archivo de antes, sin dn4: los 64 bloques de /fallos-dn-a-20261107-151425.bin tienen 3 copias vivas
PASA   archivo escrito durante el fallo, sin dn4: los 64 bloques de /fallos-dn-b-20261107-151425.bin tienen 3 copias vivas
PASA   sano otra vez a las 20:17:35: 4 DataNodes vivos, líder meta3, 0 bloques con menos de 3 copias, 0 órdenes
PASA   ./infra/deploy.sh --check: todo ok (etcd con 3 miembros sanos)
RESULTADO: PASA (0 avisos)
```

Cómo se leen los tiempos:

- **El `put` sin dn4.** Tardó 10,2 s, casi lo mismo que con todo vivo (11,1 s). En el portátil pasó de 10 a 47 s.
- **20:15:46.** Las 32 órdenes `copy` son de bloques de B que se confirmaron con 2 copias. El líder las ordena sin esperar (regla 3 de `plan_repairs`).
- **20:16:47.** Son 61 s después de la primera muestra con dn4 caído. El líder ordena las 48 copias de los bloques que tenía dn4 (`DFS_REREPLICATE_AFTER_S`), y 12 s después ya están.
- **Al volver dn4.** El líder borra las copias que sobran en dos vueltas (48 órdenes `delete` pendientes y después 23), y a los 17 s de arrancar dn4 no queda ninguna.
- **El `get`.** Tardó unos 14 s, cuando un `get` de 512 MiB con todo vivo tarda de 6 a 9 s (tarea 24), y terminó en el mismo segundo en que acabó `docker compose stop`. En el ensayo local pasó lo mismo. No revisé si el cliente esperó a dn4, que mientras se detiene sigue atendiendo las conexiones abiertas, o si fue el timeout de 5 s por bloque antes de pasar a otra réplica (docstring de `src/dfs_client/transfer.py`). El resultado es el esperado: terminó bien y con el SHA-256 original.

## 6. Conclusión

Detener dn4 en medio de un `get` o de un `put` no interrumpe ninguna operación, ni en local ni en AWS. Lo escrito queda en 2 zonas, y el líder repone la tercera copia unos 60 s después de ver dn4 caído, como dice DA-12: a los 59 s en local y a los 61 s en AWS, con los tiempos normales de la Tabla 19. Al volver dn4, el líder borra las copias que sobran y el clúster queda sano.

## 7. Para el video

1. `dfs status` con los 4 DataNodes vivos.
2. Un `dfs get` de 512 MiB y, mientras corre, `./infra/ssh.sh vm4 docker compose stop dn`. El `get` termina y los dos SHA-256 son iguales.
3. Lo mismo con un `dfs put`, y `dfs stat --blocks` del archivo nuevo, sin copias en dn4.
4. La línea de tiempo: dn4 caído, las órdenes `copy` unos 60 s después y "Bloques con menos de 3 copias vivas: 0".
5. `docker compose start dn` en la VM4: las órdenes `delete` de las copias que sobran y `dfs status` sano.
