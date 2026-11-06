# 25 · Acceso RF3 con dos clientes

## 1. Objetivo

Con dos clientes `dfs shell` en la VM5, A y B:

1. A abre un archivo de 3 bloques en `rw`, escribe en un offset a mitad del bloque 1 y todavía no cierra;
2. B lee antes del `close` de A y ve la versión anterior **completa**: el texto viejo en el offset y el SHA-256 del archivo entero igual al de la versión 1;
3. A cierra, y un `open` nuevo de B ve la versión nueva completa: el texto nuevo y el SHA-256 esperado;
4. además, `a` (anexar) y `w` (reemplazar).

## 2. Qué comprueba

| Comprobación | Garantía o decisión | Fila de la Tabla 21 |
|---|---|---|
| B, con A abierto, ve la versión 1 entera (SHA-256) | G2 (atomicidad), DA-14 | Acceso RF3 |
| Después del `close`, un `open` nuevo ve la versión 2 entera | G3 (close-to-open), DA-14 | Acceso RF3 |
| `write` en un offset dentro de un bloque | DA-14 (el cliente reescribe el bloque completo y publica una versión nueva) | Acceso RF3 |
| Modos `a` y `w` | DA-14 | — (opcional) |

Comparar el SHA-256 del archivo entero es más fuerte que mirar solo los bytes escritos. Si B viera una mezcla, por ejemplo el bloque 1 nuevo con los demás viejos, el SHA-256 no sería ni el de v1 ni el de v2.

## 3. Entorno

| | Ensayo local | AWS |
|---|---|---|
| Dónde | `compose.local.yml`, un PC con Linux | VM5, contenedor `client` |
| Commit | `b31d2b9` más los scripts de la tarea 25, sin commit todavía | *(se llena en la corrida)* |
| DA-16 | `scripts/pruebas/pruebas.env` y `DFS_W=3`; bloques de 8 MiB | `scripts/pruebas/pruebas.env` y `DFS_W=2`; bloques de 8 MiB |

## 4. Pasos

### 4.1 Con el script (evidencia reproducible)

```bash
scripts/pruebas/rf3.sh                              # local
./infra/ssh.sh vm5 scripts/pruebas/rf3.sh --env aws # AWS, después de ./infra/deploy.sh --ref t25-pruebas-carga --times scripts/pruebas/pruebas.env
```

`scripts/pruebas/rf3.sh` corre los dos `dfs shell` en el contenedor `client`, cada uno con su propia sesión. A recibe sus comandos por una FIFO: queda abierto en `rw` mientras B lee, y su `close` ocurre solo cuando B terminó, sin depender de un `sleep`. El archivo es de 20 MiB (3 bloques de 8 MiB), con `VERSION-1` en el offset 12 582 912, a mitad del bloque 1. A escribe ahí `VERSION-2`.

### 4.2 A mano, con dos terminales (para el video)

En dos terminales de la máquina que despliega: `./infra/ssh.sh vm5`. Primero, en una sola de ellas, se prepara un archivo de texto de 16 MiB más 9 bytes (3 bloques; `VERSION-1` a mitad del bloque 1):

```bash
docker compose exec client sh -c 'python -c "import sys; sys.stdout.write(\"a\"*(12*1024*1024) + \"VERSION-1\" + \"b\"*(4*1024*1024))" > /tmp/rf3.txt'
docker compose exec client dfs login --user rf3            # contraseña: secrets/rf3_password (la crea rf3.sh: córrelo antes)
docker compose exec client dfs put /tmp/rf3.txt /rf3.txt
```

Terminal A, `docker compose exec client dfs shell`:

```text
dfs> open /rf3.txt rw                 -> fd 3 ... versión 1
dfs> write 3 12582912 VERSION-2
dfs> read 3 12582912 9                -> VERSION-2 (A ve su propio cambio)
```

Terminal B, `docker compose exec client dfs shell`, **sin que A haya cerrado**:

```text
dfs> open /rf3.txt r                  -> fd 3 ... versión 1
dfs> read 3 12582912 9                -> VERSION-1
dfs> close 3
```

Terminal A: `close 3`, que responde `fd 3 cerrado: /rf3.txt versión 2`.

Terminal B:

```text
dfs> open /rf3.txt r                  -> fd 3 ... versión 2
dfs> read 3 12582912 9                -> VERSION-2
dfs> close 3
```

Si queda tiempo, en A: `open /rf3.txt a`, `write 3 0 +COLA` y `close 3`, que da la versión 3, 5 bytes más larga. Después `open /rf3.txt w`, `write 3 0 contenido nuevo` y `close 3`, que da la versión 4, de 15 bytes. B lee después de cada `close`. Al final, `dfs rm /rf3.txt`.

## 5. Resultado esperado y resultado obtenido

**Esperado:** antes del `close`, B lee `versión 1`, `VERSION-1` y el SHA-256 de v1. Después del `close`, lee `versión 2`, `VERSION-2` y el SHA-256 de v2 (v1 con `VERSION-2` en el offset). Con `a`, la versión 3 tiene 20 971 525 bytes y termina en `+COLA`; con `w`, la versión 4 tiene 15 bytes. `RESULTADO: PASA`.

### 5.1 Ensayo local

Salida de `scripts/pruebas/rf3.sh`, sin el saludo del shell ni los prompts vacíos:

```text
==> commit b31d2b9, DFS_W=3, bloques de 8 MiB
==> archivo de 20 MiB; A escribe en el offset 12582912, dentro del bloque 1
PASA   put de la versión 1: /rf3-20261106-173832-708997.bin: versión 1, 20.0 MiB en 3 bloques (3 enviados, 0 ya estaban), 0.80 s, 25.1 MiB/s

### A abre en rw y escribe; no cierra
A: dfs> fd 3: /rf3-20261106-173832-708997.bin en modo rw, versión 1, 20971520 bytes
A: dfs> 9 bytes escritos
A: dfs> VERSION-2

### B lee mientras A no ha cerrado
B: dfs> fd 3: /rf3-20261106-173832-708997.bin en modo r, versión 1, 20971520 bytes
B: dfs> VERSION-1
B: dfs> 20971520 bytes en /tmp/dfs-rf3-…/b-antes.bin
B: dfs> fd 3 cerrado sin publicar (no hubo cambios)
PASA   G2: con A abierto, B ve la versión 1 completa (VERSION-1, SHA-256 732c221fb7c1b5a0... de la versión 1)

### A cierra
A: dfs> fd 3 cerrado: /rf3-20261106-173832-708997.bin versión 2
PASA   A publicó la versión 2

### B lee después del close de A
B: dfs> fd 3: /rf3-20261106-173832-708997.bin en modo r, versión 2, 20971520 bytes
B: dfs> VERSION-2
B: dfs> 20971520 bytes en /tmp/dfs-rf3-…/b-despues.bin
PASA   G3: después del close, B ve la versión 2 completa (VERSION-2, SHA-256 4ac61ce3535402fa... esperado)

### A anexa (modo a) y B lee el final
A: dfs> fd 3 cerrado: /rf3-20261106-173832-708997.bin versión 3
B: dfs> fd 3: /rf3-20261106-173832-708997.bin en modo r, versión 3, 20971525 bytes
B: dfs> +COLA
PASA   modo a: versión 3, 20971525 bytes, con +COLA al final

### A reemplaza (modo w) y B lee
A: dfs> fd 3: /rf3-20261106-173832-708997.bin en modo w, versión 3, 0 bytes
A: dfs> fd 3 cerrado: /rf3-20261106-173832-708997.bin versión 4
B: dfs> fd 3: /rf3-20261106-173832-708997.bin en modo r, versión 4, 15 bytes
B: dfs> contenido nuevo
PASA   modo w: versión 4, 15 bytes, solo el contenido nuevo

RESULTADO: PASA
```

### 5.2 AWS

*Pendiente de la corrida en AWS: la salida de `rf3.sh --env aws`, recortada como en 5.1.*

## 6. Conclusión

En el ensayo local se cumple la semántica de DA-14. Mientras A tiene el archivo abierto en `rw` con un cambio sin publicar, B ve la versión 1 entera, comprobada con el SHA-256 de todo el archivo (G2). Apenas A cierra, un `open` nuevo ve la versión 2 entera (G3). `a` y `w` publican las versiones 3 y 4 con el tamaño esperado. *Lo de AWS queda pendiente de la corrida.*

## 7. Para el video

Las dos terminales una al lado de la otra (sección 4.2):

1. A: `open rw`, `write` y `read`; A ve `VERSION-2`.
2. B, con A todavía abierto: `open r` con **versión 1**, y `read` con `VERSION-1`.
3. A: `close`, que responde **versión 2**.
4. B: `open r` con **versión 2**, y `read` con `VERSION-2`.
5. Si hay tiempo, `a` y `w`, con el número de versión y el tamaño de cada `open` de B.
