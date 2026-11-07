# 27 · Consistencia con un secundario cortado

## 1. Objetivo

Cortar con `iptables` el tráfico hacia un secundario (dn4, puerto 9444) mientras se sube una versión nueva de un archivo, con otro cliente leyéndolo una y otra vez, y comprobar que:

1. el `put` termina bien y cada bloque de la versión nueva queda en disco en al menos 2 DataNodes de zonas distintas (G1);
2. cada lectura ve una versión completa, nunca una mezcla (G2): la anterior mientras dura el `put`, y la nueva después (G3);
3. al quitar la regla, el líder deja todos los bloques con 3 copias y el clúster queda sano.

## 2. Qué comprueba

| Comprobación | Garantía o decisión | Fila de la Tabla 21 |
|---|---|---|
| `put` exitoso con un secundario sin responder; bloques en 2 zonas | G1, DA-04 (W=2 y espera del primario) | Consistencia |
| Cada lectura es la versión 1 o la 2 completa (SHA-256) | G2 | Consistencia |
| Las lecturas que empiezan después del `put` ven la versión 2 | G3 | Consistencia |
| Terceras copias repuestas al quitar la regla | DA-12 | Consistencia |

## 3. Entorno

| | Ensayo local | AWS |
|---|---|---|
| Dónde | `compose.local.yml` en un portátil con Windows 11 y Docker Desktop 29.1.2; el script corre en Git Bash | *(se llena en la corrida)* |
| Commit | rama `t27-pruebas-fallos`, sobre `main` en `b6e4d2b`, antes de su primer commit | *(se llena en la corrida)* |
| DA-16 | normales, salvo `DFS_LEADER_GRACE_S=60` (en el `.env` local) | normales de la Tabla 19 |
| Cómo se corta la red | `iptables -I INPUT -p tcp --dport 9444 -j DROP` en el espacio de red del contenedor dn4, desde un contenedor auxiliar (alpine con `iptables` y `NET_ADMIN`). `compose.local.yml` no cambia | la misma regla con `sudo iptables` en la VM4 (`network_mode: host`) |

La regla descarta lo que llega al puerto interno de dn4: lo que le reenvía un primario y las copias que le mandan otros DataNodes por orden del líder. Los latidos de dn4 salen hacia el 8444 de los MetaServers y siguen pasando, y su API pública (9443) responde. Por eso dn4 no se cae: solo deja de recibir copias.

Valores que importan aquí: `DFS_W=2`, `DFS_PRIMARY_WAIT_S=10`, `DFS_BLOCK_MIB=8`. El archivo mide 64 MiB, 8 bloques, para que quepan varias lecturas mientras dura el `put`.

## 4. Pasos

```bash
# Local
scripts/pruebas/fallos.sh consistencia            # --target dnN para cortar otro DataNode

# AWS (WSL)
scripts/pruebas/fallos.sh consistencia --env aws
```

Lo que hace `fallos.sh consistencia`:

1. Crea dos archivos aleatorios de 64 MiB, v1 y v2, y sube v1.
2. Pone la regla en dn4 y sube v2 sobre el mismo archivo, en segundo plano.
3. Mientras el `put` sigue, la sesión `reader` baja el archivo en bucle y anota la hora de inicio, la de fin y el SHA-256 de cada lectura. Después del `put` hace dos lecturas más.
4. Cuenta cuántas lecturas fueron v1, cuántas v2 y cuántas otra cosa. Revisa G1 con `dfs stat --blocks`.
5. Quita la regla, comprueba con `iptables -C` que ya no está, espera 0 bloques con menos de 3 copias y deja el clúster sano.

## 5. Resultado esperado y resultado obtenido

**Esperado:**

- El `put` tarda más, porque el primario espera hasta 10 s a dn4 en los bloques donde dn4 es secundario, pero termina bien.
- Todos los bloques quedan en 2 zonas.
- Toda lectura es v1 o v2 completa. Las que terminan durante el `put` son v1 y las que empiezan después son v2.
- Al quitar la regla, el líder repone lo que falte.

### 5.1 Ensayo local (64 MiB)

```text
17:43:21  >> iptables: cortado el puerto 9444 de dn4 (lo que le reenvían el primario y las órdenes copy)
17:43:21  >> empieza el put de la versión 2
17:43:24  vivos=dn1,dn2,dn3,dn4 caídos=- líder=meta1 menos_de_3=0 copy=0 delete=0
17:43:39  vivos=dn1,dn2,dn3 caídos=dn4 líder=meta1 menos_de_3=65 copy=1 delete=0
17:43:56  vivos=dn1,dn2,dn3,dn4 caídos=- líder=meta1 menos_de_3=1 copy=0 delete=0
17:44:02  >> termina el put de la versión 2 (código 0)
17:44:08  >> lectura que terminó a las 17:43:32: versión 1
17:44:09  >> lectura que terminó a las 17:43:54: versión 1
17:44:09  >> lectura que terminó a las 17:43:57: versión 1
17:44:09  >> lectura que terminó a las 17:44:00: versión 1
17:44:09  >> lectura que terminó a las 17:44:02: versión 2
17:44:10  >> lectura que terminó a las 17:44:05: versión 2 (después del put)
17:44:10  >> lectura que terminó a las 17:44:08: versión 2 (después del put)
17:44:14  >> 6 de 8 bloques de la versión 2 no tienen copia viva en dn4
17:44:17  >> iptables: regla quitada en dn4
17:44:18  vivos=dn1,dn2,dn3,dn4 caídos=- líder=meta1 menos_de_3=0 copy=0 delete=0
```

```text
PASA   put con el tráfico a dn4 cortado: terminó bien: /fallos-cons-20261107-124234.bin: versión 2, 64.0 MiB en 8 bloques (8 enviados, 0 ya estaban), 38.53 s, 1.7 MiB/s
PASA   G2: 7 lecturas, todas una versión completa (4 de la 1 y 3 de la 2), ninguna mezcla
PASA   G3: las lecturas que empezaron después del put ven la versión 2
PASA   mientras duraba el put, los lectores veían la versión 1 (4 lecturas)
PASA   versión 2 con dn4 cortado: G1, los 8 bloques tienen copias vivas en al menos 2 zonas (7 con 3 copias)
PASA   sin bloques con menos de 3 copias a las 17:44:18, 1 s después de quitar la regla
PASA   versión 2 después de desbloquear: los 8 bloques de /fallos-cons-20261107-124234.bin tienen 3 copias vivas
PASA   sano otra vez a las 17:44:31: 4 DataNodes vivos, líder meta1, 0 bloques con menos de 3 copias, 0 órdenes
PASA   los 11 contenedores corren y los 3 etcd están healthy
RESULTADO: PASA (0 avisos)
```

Cómo se lee:

- **Lecturas.** La lectura que terminó a las 17:44:02 ya vio la versión 2. Empezó después del commit, mientras `dfs put` todavía imprimía su resultado. Ninguna lectura vio algo distinto de v1 o v2.
- **Copias sin dn4.** El líder repuso las terceras copias mientras la regla seguía puesta. Las mandó a otros DataNodes, porque prefiere un nodo de una zona que las copias vivas no cubren y, si empatan, el de menor `node_id` (regla 5 de `plan_repairs`). Al revisar G1, 6 de los 8 bloques no tenían copia en dn4 y 7 ya tenían 3 copias. En la muestra siguiente a quitar la regla ya no quedaba ninguno con menos de 3.
- **dn4 caído a las 17:43:39 y dn1 caído antes, durante el `put` de v1 (en el CSV).** No los causó la regla, que no toca los latidos. Con los 11 contenedores y subidas de 64 MiB, el portátil va a unos 2 MiB/s, y un DataNode pierde latidos por unos segundos. También pasó con dn1 en la corrida final de la zona c, a las 18:40:31, con 288 copias en curso. En AWS cada DataNode tiene su VM; si allí se repite, es un hallazgo.

### 5.2 AWS (64 MiB)

*Pendiente de la corrida en AWS.*

## 6. Conclusión

En el ensayo local, cortar el tráfico hacia un secundario no rompe ninguna garantía. El `put` termina con cada bloque en 2 zonas (G1), los lectores ven la versión 1 completa hasta el commit y la 2 completa después (G2 y G3), y el clúster vuelve a sano. El líder no tuvo que esperar a que se quitara la regla: repuso las copias en los otros DataNodes. *La conclusión sobre AWS se llena con la corrida.*

## 7. Para el video

1. En la VM4, `sudo iptables -S INPUT` con la regla puesta.
2. La subida de v2, más lenta, y al lado las lecturas del otro cliente con su SHA-256: el de v1 hasta que termina el `put` y el de v2 después.
3. `dfs stat --blocks` de v2: cada bloque en 2 zonas, algunos sin dn4.
4. La regla quitada (`sudo iptables -S INPUT` sin ella) y `dfs status` sano.
