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
| Dónde | `compose.local.yml` en un portátil con Windows 11 y Docker Desktop 29.1.2; el script corre en Git Bash | 5 VMs t3.small en us-east-1 (Learner Lab de Santiago, 7 de noviembre de 2026); `fallos.sh` corre en WSL y `timeline.sh` en la VM5 |
| Commit | rama `t27-pruebas-fallos`, sobre `main` en `b6e4d2b`, antes de su primer commit | `1e1d3b2` desplegado en las VMs con `./infra/deploy.sh --ref t27-pruebas-fallos`. `fallos.sh`, que corre en WSL, lleva un arreglo hecho antes de la primera corrida: su función `aws()` tapaba la AWS CLI y pasó a llamarse `on_aws()` |
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

Horas de CloudTrail (hora de Colombia, UTC−5): `sesion-antes` terminó a las 15:36; End Lab detuvo las VMs a las 15:38:37, y Start Lab las encendió a las 16:53:09. El SSH a la VM5 ya respondía a las 16:55, y entonces empezó `sesion-despues`. Línea de tiempo recortada (UTC), de `docs/pruebas/datos/27-sesion-despues-aws.csv`:

```text
21:55:50  >> inicio: sesion-despues en aws (DFS_W=2, DFS_REREPLICATE_AFTER_S=60, DFS_LEADER_GRACE_S=300)
21:55:53  vivos=dn1,dn2,dn3,dn4 caídos=- líder=meta2 menos_de_3=0 copy=0 delete=0
21:55:57  >> el líder es meta2; su contenedor arrancó a las 21:53:21, así que su espera termina después de las 21:58:21
21:58:20  vivos=dn1,dn2,dn3,dn4 caídos=- líder=meta2 menos_de_3=0 copy=0 delete=0
21:58:26  vivos=dn1,dn2,dn3,dn4 caídos=- líder=meta2 menos_de_3=0 copy=0 delete=0
22:00:19  vivos=dn1,dn2,dn3,dn4 caídos=- líder=meta2 menos_de_3=0 copy=0 delete=0
22:00:21  >> fin de la observación
```

Las 46 muestras de la observación, de 21:55:53 a 22:00:19, son iguales: 4 DataNodes vivos, 0 bloques con menos de 3 copias y 0 órdenes.

```text
PASA   DA-12: 26 muestras entre 21:55:53 y 21:58:21, dentro de la espera del líder, y ninguna con órdenes copy (0 con bloques con menos de 3 copias)
PASA   los DataNodes volvieron con las mismas direcciones: dn1=172.31.86.98:9443 dn2=172.31.16.163:9443 dn3=172.31.47.32:9443 dn4=172.31.24.157:9443
PASA   mismas IP privadas: vm1=172.31.86.98 vm2=172.31.16.163 vm3=172.31.47.32 vm4=172.31.24.157 vm5=172.31.84.17
PASA   archivo de la sesión anterior: get de /fallos-sesion.bin terminó bien y el SHA-256 es igual al original (440aa315f89eaafb...)
PASA   sano otra vez a las 22:00:37: 4 DataNodes vivos, líder meta2, 0 bloques con menos de 3 copias, 0 órdenes
PASA   ./infra/deploy.sh --check: todo ok (etcd con 3 miembros sanos)
RESULTADO: PASA (0 avisos)
```

Cómo se lee:

- **Qué se vio.** Start Lab enciende las 5 VMs a la vez. Cuando empezó la observación, 2 min 32 s después de que arrancó el contenedor del líder, los 4 DataNodes ya latían, y no había bloques con menos de 3 copias. Durante el resto de la espera y los 2 minutos siguientes no hubo ninguna orden: ni `copy` ni `delete`.
- **Qué no se vio.** Los primeros 2 min 32 s de la espera no tienen muestras, porque la VM5 todavía no respondía por SSH. Lo que pasó en ese lapso no está medido.
- **A diferencia del ensayo local,** en AWS ningún DataNode llegó tarde, así que no hubo bloques con menos de 3 copias que pusieran a prueba la espera. Esa parte la muestra el ensayo local (sección 5.1), con dn4 arrancado 40 s después.

## 6. Conclusión

En el ensayo local, el líder nuevo no ordenó copias durante su espera, aunque faltaba un DataNode y había 76 bloques con menos de 3 copias. Como el nodo volvió dentro de la espera, no hubo ninguna re-réplica, que es lo que busca DA-12 al iniciar una sesión. En AWS, después de End Lab y Start Lab, el sistema volvió completo sin ninguna re-réplica, con las mismas IP privadas en las 5 VMs, las mismas direcciones de los DataNodes y el archivo de la sesión anterior intacto. En AWS los 4 DataNodes volvieron antes de que la observación pudiera empezar, así que allí no hubo bloques con menos de 3 copias durante la espera.

## 7. Para el video

1. `sesion-antes`: el archivo subido y su SHA-256.
2. End Lab y Start Lab (captura de la consola del laboratorio).
3. La línea de tiempo de `sesion-despues`: el líder, bloques con menos de 3 copias y `copy=0` durante los 5 minutos.
4. `dfs status` completo al final, y los dos SHA-256 iguales.

## Pendientes

- **Resuelto después de las corridas: los mensajes INFO del logger `dfsha` no salían en `docker logs`.** Solo salían los WARNING, porque ningún módulo configuraba un handler y uvicorn solo configura los suyos. Por eso no se veían "líder: meta1 ganó la elección", "líder: copia de ..." ni "limpieza: borrado ...".
  - Ahora `configure_logging()` en `src/dfs_common/serve.py` los manda a stderr desde INFO, con la hora. Lo usan el MetaServer y el DataNode al arrancar.
  - Son 6 mensajes y salen solo cuando pasa algo: la elección, una copia, un borrado o el registro de un DataNode. Ninguno se repite por latido.
  - Prueba: `test_info_messages_of_dfsha_reach_stderr_once_for_docker_logs` en `tests/test_serve.py`.
  - Esta prueba no usaba esos mensajes: mide desde el arranque del contenedor del líder. En una corrida nueva, `docker logs meta1` mostraría la hora exacta de la elección y de cada orden. Las corridas de este documento son de antes del cambio y no los tienen.
