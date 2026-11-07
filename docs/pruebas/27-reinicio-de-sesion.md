# 27 · Fallos: reinicio de sesión

## 1. Objetivo

Terminar la sesión del Learner Lab (End Lab) e iniciar otra (Start Lab), y comprobar que:

1. durante la espera del líder nuevo (`DFS_LEADER_GRACE_S`) no hay órdenes `copy`, aunque algún DataNode tarde en volver y haya bloques con menos de 3 copias;
2. el sistema vuelve completo: 4 DataNodes vivos, 0 bloques con menos de 3 copias y 0 órdenes;
3. las VMs y los DataNodes vuelven con las mismas IP privadas;
4. un archivo subido antes de End Lab se baja con el mismo SHA-256.

## 2. Qué comprueba

| Comprobación | Garantía o decisión | Fila de la Tabla 21 |
|---|---|---|
| Ninguna orden `copy` durante `DFS_LEADER_GRACE_S` | DA-12 | Fallos: reinicio de sesión |
| Mismas IP privadas y mismas direcciones de los DataNodes | DA-11, DA-13 | Fallos: reinicio de sesión |
| Archivo de la sesión anterior intacto | G5 | Fallos: reinicio de sesión |

## 3. Entorno

| | Ensayo local | AWS |
|---|---|---|
| Dónde | `compose.local.yml` en un portátil con Windows 11 y Docker Desktop 29.1.2; el script corre en Git Bash | *(se llena en la corrida)* |
| Commit | rama `t27-pruebas-fallos`, sobre `main` en `b6e4d2b`, antes de su primer commit | *(se llena en la corrida)* |
| DA-16 | normales, salvo `DFS_LEADER_GRACE_S=60` (en el `.env` local) | normales de la Tabla 19: `DFS_LEADER_GRACE_S=300` |
| Cómo se simula la sesión | `docker compose stop` de todo; después arrancan las VMs de control y el cliente, y dn4 40 s más tarde, como una VM que tarda en arrancar | End Lab y Start Lab |

## 4. Pasos

**AWS (WSL).** Es el cierre natural de la sesión de pruebas.

```bash
scripts/pruebas/fallos.sh sesion-antes --env aws
# End Lab. Después: Start Lab, credenciales nuevas en ~/.aws/credentials y
./infra/allow-ssh.sh
scripts/pruebas/fallos.sh sesion-despues --env aws     # lo antes posible
```

**Local.**

```bash
scripts/pruebas/fallos.sh sesion-antes
docker compose -f compose.local.yml stop
docker compose -f compose.local.yml start etcd1 etcd2 etcd3 meta1 meta2 meta3 dn1 dn2 dn3 client
scripts/pruebas/fallos.sh sesion-despues &     # empieza a observar enseguida
sleep 40; docker compose -f compose.local.yml start dn4
wait
```

Qué hace cada parte:

- **`sesion-antes`.** Sube un archivo aleatorio de 64 MiB y guarda en `~/.cache/dfsha/27-sesion-ENTORNO.env`, fuera del repositorio, su ruta, su SHA-256, las direcciones de los DataNodes que da `dfs status` y, en AWS, las IP privadas de las 5 VMs.
- **`sesion-despues`:**
  1. Espera hasta 5 minutos a que las VMs, el contenedor `client` y los MetaServers respondan.
  2. Arranca la línea de tiempo y lee el líder.
  3. Toma la hora en que arrancó el contenedor de ese MetaServer (`docker inspect`). Como el líder gana la elección después de arrancar, los `DFS_LEADER_GRACE_S` que siguen a esa hora quedan dentro de su espera.
  4. Observa hasta 2 minutos después del fin de la espera y cuenta las muestras de ese lapso con órdenes `copy`.
  5. Compara las direcciones y las IP, baja el archivo y espera el clúster sano.

## 5. Resultado esperado y resultado obtenido

**Esperado:**

- Mientras dn4 falta, hay bloques con menos de 3 copias, pero 0 órdenes `copy` hasta que termina la espera del líder.
- Si dn4 vuelve dentro de la espera, no hay ninguna re-réplica.
- Las direcciones son las mismas y el SHA-256 es igual.

### 5.1 Ensayo local (64 MiB, espera de 60 s)

Pasos de la simulación: todo detenido a las 17:51:10, todo menos dn4 arrancado a las 17:51:22 y dn4 arrancado a las 17:52:04.

```text
17:51:30  >> inicio: sesion-despues en local (DFS_W=2, DFS_REREPLICATE_AFTER_S=60, DFS_LEADER_GRACE_S=60)
17:51:33  vivos=dn1,dn2,dn3 caídos=dn4 líder=meta1 menos_de_3=76 copy=0 delete=0
17:51:36  >> el líder es meta1; su contenedor arrancó a las 17:51:22, así que su espera termina después de las 17:52:22
17:51:58  vivos=dn1,dn2,dn3 caídos=dn4 líder=meta1 menos_de_3=76 copy=0 delete=0
17:52:05  vivos=dn1,dn2,dn3,dn4 caídos=- líder=meta1 menos_de_3=0 copy=0 delete=0
17:52:25  vivos=dn1,dn2,dn3,dn4 caídos=- líder=meta1 menos_de_3=0 copy=0 delete=0
17:54:22  >> fin de la observación
```

```text
PASA   DA-12: 8 muestras entre 17:51:33 y 17:52:22, dentro de la espera del líder, y ninguna con órdenes copy (5 con bloques con menos de 3 copias)
PASA   los DataNodes volvieron con las mismas direcciones: dn1=10.88.0.31:9443 dn2=10.88.0.32:9443 dn3=10.88.0.33:9443 dn4=10.88.0.34:9443
PASA   archivo de la sesión anterior: get de /fallos-sesion.bin terminó bien y el SHA-256 es igual al original (5b0705da5c009304...)
PASA   sano otra vez a las 17:54:34: 4 DataNodes vivos, líder meta1, 0 bloques con menos de 3 copias, 0 órdenes
PASA   los 11 contenedores corren y los 3 etcd están healthy
RESULTADO: PASA (0 avisos)
```

Durante 32 s hubo 76 bloques con menos de 3 copias y ninguna orden `copy`. dn4 volvió dentro de la espera, sus copias contaron otra vez, y en toda la observación no hubo re-réplicas. La línea de tiempo completa está en `docs/pruebas/datos/27-sesion-despues-local.csv`.

### 5.2 AWS (64 MiB, espera de 300 s)

*Pendiente de la corrida en AWS.* En AWS la línea de tiempo empieza cuando la VM5 ya responde por SSH. Si eso pasa después de la espera del líder, el script lo dice como AVISO. En ese caso la evidencia solo puede mostrar que el clúster volvió completo.

## 6. Conclusión

En el ensayo local, el líder nuevo no ordenó copias durante su espera, aunque faltaba un DataNode y había 76 bloques con menos de 3 copias. Como el nodo volvió dentro de la espera, no hubo ninguna re-réplica, que es lo que busca DA-12 al iniciar una sesión. *La conclusión sobre AWS se llena con la corrida.*

## 7. Para el video

1. `sesion-antes`: el archivo subido y su SHA-256.
2. End Lab y Start Lab (captura de la consola del laboratorio).
3. La línea de tiempo de `sesion-despues`: el líder, bloques con menos de 3 copias y `copy=0` durante los 5 minutos.
4. `dfs status` completo al final, y los dos SHA-256 iguales.

## Pendientes

- **Los mensajes INFO del logger `dfsha` no salen en `docker logs`.** Solo salen los WARNING. Ningún módulo configura un handler, y uvicorn solo configura los suyos. Por eso no se ven "líder: meta1 ganó la elección", "líder: copia de ..." ni "limpieza: borrado ...".
  - Esta prueba no usa esos mensajes: mide desde el arranque del contenedor del líder.
  - Con ellos, la evidencia mostraría la hora exacta de la elección y de cada orden. Lo decide el equipo; sería un cambio pequeño en `src/dfs_common/serve.py` o en los `__main__` de cada servicio.
