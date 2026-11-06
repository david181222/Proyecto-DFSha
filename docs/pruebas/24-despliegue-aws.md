# 24 · Primer despliegue en las 5 VMs de AWS

## 1. Objetivo

Desplegar DFSha en AWS Academy con los scripts de la tarea 23 (`infra/`) y comprobar que funciona:

1. etcd con 3 miembros sanos, por TLS;
2. los 3 MetaServers en `/cluster/metaservers`;
3. `/admin/status` con líder y los 4 DataNodes vivos, cada uno en su zona;
4. el script de humo de la tarea 22, corrido desde la VM5 con 512 MiB;
5. seguridad básica de red: tráfico entre nodos por IP privadas, y desde el PC solo responde el puerto 22;
6. reinicio de una VM de control: sus contenedores vuelven solos y el nodo se reintegra;
7. nueva sesión del laboratorio: después de End Lab y Start Lab, todo vuelve. Es una comprobación rápida; la prueba formal es la tarea 27.

## 2. Qué comprueba

| Comprobación | Garantía o decisión | Fila de la Tabla 21 |
|---|---|---|
| 1 a 3 | DA-01, DA-11 (descubrimiento por etcd), DA-12 (líder), DA-13 (topología y zonas), DA-15 (despliegue reproducible), DA-16 (`/admin/status`) | — |
| 4 | G1, DA-04, DA-05 | Carga y recuperación (desde la VM5) |
| 5 | DA-06 (TLS y mTLS), DA-11 (IP privadas), DA-13 (puertos solo dentro del grupo de seguridad), RNF6 | Seguridad (solo la parte de red) |
| 6 | DA-12 (cambio de líder y su espera antes de re-replicar), DA-13 (disco de etcd) | — |
| 7 | DA-12 (sin re-réplicas durante la espera del líder) | Fallos: reinicio de sesión (comprobación rápida) |

## 3. Entorno

- **AWS Academy Learner Lab, us-east-1:** 5 VMs t3.small en la VPC por defecto, creadas por `infra/create.sh`:

  | VM | Zona | Servicios | IP privada |
  |---|---|---|---|
  | vm1 | us-east-1a | etcd1, meta1, dn1 | 172.31.33.107 |
  | vm2 | us-east-1b | etcd2, meta2, dn2 | 172.31.2.194 |
  | vm3 | us-east-1c | etcd3, meta3, dn3 | 172.31.90.48 |
  | vm4 | us-east-1b | dn4 | 172.31.8.247 |
  | vm5 | us-east-1a | cliente y pruebas | 172.31.37.93 |

- **Discos:** 20 GiB gp3 cifrado en cada VM, y 4 GiB gp3 cifrado para etcd en vm1, vm2 y vm3. Todos llevan las etiquetas `Project=dfsha` y `DfsVm=vmN`, y se borran con su VM.
- **Otras instancias:** en la cuenta había otras instancias; sumadas a las 5 de DFSha, quedaban dentro del límite de 9 del Learner Lab.
- **Sistema de las VMs:** Ubuntu 24.04 LTS, la imagen de Canonical que da el parámetro público de SSM. El user-data instaló Docker 29.1.3, Docker Compose 2.40.3 y buildx 0.30.1, los paquetes de Ubuntu.
- **Máquina que despliega:** WSL con Ubuntu 24.04, AWS CLI 2.25.13, y python3 3.12.3 con cryptography 41.0.7 para `scripts/gen_secrets.py`.
- **Commit desplegado:** `adee699`, de la rama `t24-despliegue`: `main` en `173dbea` más el arreglo de la sección 5.1.
- **Valores de DA-16:** los normales de la Tabla 19 (`./infra/deploy.sh` sin `--times`): `DFS_BLOCK_MIB=8`, `DFS_W=2`, `DFS_HEARTBEAT_S=5`, `DFS_NODE_LEASE_S=15`, `DFS_REREPLICATE_AFTER_S=60`, `DFS_LEADER_GRACE_S=300`, `DFS_RETENTION_S=600`, `DFS_GC_INTERVAL_S=600`, `DFS_GC_MIN_AGE_S=3600`.
- **Horas:** en UTC.

## 4. Pasos

Desde una terminal de WSL, en la raíz del repositorio, con las credenciales del Learner Lab en `~/.aws/credentials` y la llave en `~/.ssh/labsuser.pem` (README, "Despliegue en AWS").

```bash
aws sts get-caller-identity                 # credenciales vigentes
./infra/create.sh                           # grupo de seguridad, 5 VMs y SSH abierto a la IP actual
./infra/deploy.sh --ref t24-despliegue      # secretos, disco de etcd, contenedores y salud
./infra/deploy.sh --check                   # comprobaciones 1 a 3

# 4. Humo desde la VM5, 3 corridas de 512 MiB
./infra/ssh.sh vm5 'for i in 1 2 3; do scripts/pruebas/smoke.sh --env aws --mib 512 --csv docs/pruebas/datos/24-humo-aws.csv --rep $i; done'

# 5. Red: miembros de etcd y conexiones de vm1
./infra/ssh.sh vm1 docker compose exec -T etcd etcdctl member list -w table
./infra/ssh.sh vm1 docker compose exec -T etcd etcdctl endpoint health --cluster -w table
./infra/ssh.sh vm1 'ss -tn state established'     # se filtran los puertos 2379, 2380, 8443, 8444, 9443 y 9444
# Desde el PC, a la IP pública de cada VM (se lee con aws ec2 describe-instances):
timeout 4 bash -c '</dev/tcp/IP_PUBLICA/PUERTO'    # para 22, 2379, 2380, 8443, 8444, 9443 y 9444
aws ec2 describe-security-groups --group-names dfsha-sg

# 6. Reinicio de la VM del líder (meta2, en vm2), y dfs status cada 5 s desde la VM5
aws ec2 reboot-instances --instance-ids ID_DE_VM2
./infra/ssh.sh vm5 docker compose exec -T client dfs status
./infra/deploy.sh --check

# 7. Archivo de 64 MiB guardado antes de la nueva sesión
./infra/ssh.sh vm5 'docker compose exec -T client sh -c "head -c 64M /dev/urandom > /tmp/persist.bin && sha256sum /tmp/persist.bin"'
./infra/ssh.sh vm5 docker compose exec -T client dfs put /tmp/persist.bin /persist.bin
#    End Lab, Start Lab y credenciales nuevas en ~/.aws/credentials
./infra/allow-ssh.sh
./infra/deploy.sh --check
./infra/ssh.sh vm1 docker compose exec -T etcd etcdctl get --prefix /blocks/ --print-value-only   # stored_on de cada bloque
./infra/ssh.sh vm5 docker compose exec -T client dfs get /persist.bin /tmp/persist-copia.bin
./infra/ssh.sh vm5 docker compose exec -T client sha256sum /tmp/persist.bin /tmp/persist-copia.bin
./infra/ssh.sh vm1 mountpoint /srv/dfsha/etcd
```

Los comandos `dfs` de la VM5 corren con la sesión de un usuario: `admin` para `dfs status`, y `smoke` para los archivos. Se entra antes con `dfs login`, con la contraseña por la entrada estándar desde `secrets/aws/admin_password` o `secrets/smoke_password`.

## 5. Resultado esperado y resultado obtenido

### 5.1 Tiempos del despliegue, problemas y arreglos

| Paso | Hora | Duración |
|---|---|---|
| `create.sh` | 21:25:41 | 68 s |
| `deploy.sh` (espera del user-data, montaje del disco de etcd, secretos y construcción de la imagen en las 5 VMs) | 21:27:09 | 58 s, con falla en vm3 (abajo) |
| `deploy.sh --ref t24-despliegue`, con el arreglo | 21:33:09 | 33 s, incluida la comprobación de salud |
| `deploy.sh --check` | — | 11 a 15 s |

De la primera orden al sistema sano pasaron 8 minutos, contando el análisis del problema. Los scripts corrieron en total unos 2 minutos y medio.

**Problema 1: la llave SSH no se podía leer.** Antes de crear nada, `~/.ssh/labsuser.pem` estaba a nombre de `root`: se había copiado con `sudo`. Con permisos 400, el usuario de WSL no la podía leer, y `require_ssh_key` solo miraba los permisos, así que `deploy.sh` habría pasado minutos diciendo "no responde por SSH".
- **Arreglo:** `sudo chown`. Además, `require_ssh_key` (`infra/lib.sh`) ahora comprueba que la llave se pueda leer, y si no, dice de quién es y qué `chown` correr.

**Problema 2: la imagen no se construyó en vm3.** El primer `deploy.sh` terminó bien en vm1, vm2, vm4 y vm5; en vm3, `docker compose up --build` falló así:

```text
#12 naming to docker.io/library/dfsha:aws done
#12 ERROR: image "docker.io/library/dfsha:aws": already exists
target meta: failed to solve: image "docker.io/library/dfsha:aws": already exists
```

- **Por qué:** en las VMs de control, `meta` y `dn` salen del mismo Dockerfile con el mismo nombre de imagen (`dfsha:aws`), y Compose las construye a la vez. Docker 29 guarda las imágenes en el almacén de containerd, y ahí dos construcciones que terminan al mismo tiempo con el mismo nombre chocan. Es una carrera: en vm1 y vm2 no chocaron, y en local (Docker Desktop, Compose 5.3.0) tampoco.
- **Arreglo:** en `compose.aws.yml`, cada servicio tiene su propia etiqueta (`dfsha:aws-meta`, `dfsha:aws-dn` y `dfsha:aws-client`). Las capas se comparten, y la segunda construcción sale de la caché. Con ese cambio (`adee699`), `deploy.sh` terminó bien en las 5 VMs.
- **Ya comprobado:** en vm3 el disco de etcd ya estaba montado y los secretos copiados. Antes del arreglo ya se veía que lo pendiente de la tarea 23 funciona: el parámetro de SSM, los discos cifrados, la instalación de Docker con el user-data y el disco de etcd encontrado por su ID en `/dev/disk/by-id/`.

### 5.2 Comprobaciones 1 a 3: etcd, MetaServers y `/admin/status`

**Esperado:** etcd con 3 miembros sanos por TLS; los 3 MetaServers listados; un líder; y los 4 DataNodes vivos, en us-east-1a, 1b, 1c y 1b.

**Obtenido** (`./infra/deploy.sh --check`, después del arreglo):

```text
https://172.31.33.107:2379 is healthy: successfully committed proposal: took = 7.096009ms
https://172.31.2.194:2379 is healthy: successfully committed proposal: took = 7.519256ms
https://172.31.90.48:2379 is healthy: successfully committed proposal: took = 10.504709ms
ok     etcd: 3 miembros sanos, por TLS
ok     MetaServer 172.31.33.107:8443 responde por TLS
ok     MetaServer 172.31.2.194:8443 responde por TLS
ok     MetaServer 172.31.90.48:8443 responde por TLS
ok     DataNode 172.31.33.107:9443 responde por TLS
ok     DataNode 172.31.2.194:9443 responde por TLS
ok     DataNode 172.31.90.48:9443 responde por TLS
ok     DataNode 172.31.8.247:9443 responde por TLS
ok     /cluster/metaservers lista los 3 MetaServers
ok     líder: meta2
ok     dn1 vivo en us-east-1a
ok     dn2 vivo en us-east-1b
ok     dn3 vivo en us-east-1c
ok     dn4 vivo en us-east-1b

Líder: meta2

MetaServers:
  meta1    172.31.33.107    8443/8444
  meta2    172.31.2.194     8443/8444
  meta3    172.31.90.48     8443/8444

DataNodes:
  dn1      vivo   us-east-1a   172.31.33.107:9443     15.2 GiB libres
  dn2      vivo   us-east-1b   172.31.2.194:9443      15.2 GiB libres
  dn3      vivo   us-east-1c   172.31.90.48:9443      15.2 GiB libres
  dn4      vivo   us-east-1b   172.31.8.247:9443      15.2 GiB libres

Bloques con menos de 3 copias vivas: 0

Órdenes pendientes: 0
```

### 5.3 Comprobación 4: humo de 512 MiB desde la VM5

**Esperado:** `RESULTADO: PASA`.

**Obtenido:** las 3 corridas terminaron en `RESULTADO: PASA (0 avisos)`. La primera:

```text
== Resumen: aws, 512 MiB, usuario smoke, commit adee699, DFS_W=2 ==
PASA   admin entró y el usuario smoke está listo
PASA   put: /smoke-20261106-213427-4855.bin: versión 1, 512.0 MiB en 64 bloques (64 enviados, 0 ya estaban), 8.94 s, 57.3 MiB/s
PASA   G1: los 64 bloques tienen copias vivas en al menos 2 zonas
PASA   DA-05: bloques repartidos en los 4 DataNodes (dn1 dn2 dn3 dn4)
PASA   los 64 bloques tienen 3 copias vivas
PASA   get: 8.98 s, 57.0 MiB/s; SHA-256 igual al original (ef0f749ccfd8e1aa...)
PASA   hay líder: meta2
PASA   dfs status: ningún bloque con menos de 3 copias vivas
PASA   limpieza: borrado /smoke-20261106-213427-4855.bin
RESULTADO: PASA (0 avisos)
```

Los datos crudos están en `datos/24-humo-aws.csv`. Frente a la referencia local de la tarea 22, también con 512 MiB:

| Operación | AWS, mediana de 3 | Local, mediana de 5 |
|---|---|---|
| `put` | 8,94 s (57,3 MiB/s) | 21,91 s (23,4 MiB/s) |
| `get` | 5,61 s (91,2 MiB/s) | 11,76 s (43,5 MiB/s) |

En AWS cada nodo tiene su VM, mientras que en local todos comparten el mismo PC. No es la prueba de rendimiento (tarea 25): son 3 corridas, para tener una referencia.

### 5.4 Comprobación 5: seguridad básica de red

**Esperado:** etcd y los servicios se hablan por IP privadas; desde el PC, solo el puerto 22 responde en las IP públicas.

**Obtenido:**
- **etcd:** sus miembros anuncian solo IP privadas, por `https`.

  ```text
  +------------------+---------+-------+----------------------------+----------------------------+------------+
  |        ID        | STATUS  | NAME  |         PEER ADDRS         |        CLIENT ADDRS        | IS LEARNER |
  +------------------+---------+-------+----------------------------+----------------------------+------------+
  | 2e2141c6ba0fa274 | started | etcd3 |  https://172.31.90.48:2380 |  https://172.31.90.48:2379 |      false |
  | 9058cd90714bdd04 | started | etcd1 | https://172.31.33.107:2380 | https://172.31.33.107:2379 |      false |
  | ae767a65369a0baf | started | etcd2 |  https://172.31.2.194:2380 |  https://172.31.2.194:2379 |      false |
  +------------------+---------+-------+----------------------------+----------------------------+------------+
  ```

- **Conexiones de vm1** en los puertos 2379, 2380, 8443, 8444, 9443 y 9444:
  - mientras corría el humo, todas iban a IP privadas. Había conexiones entre pares de etcd (2380), de los MetaServers a etcd (2379), de los DataNodes a la API interna (8444), de réplica entre DataNodes (9444) y del cliente de vm5 a la API pública (`172.31.33.107:8443 -> 172.31.37.93`);
  - un conteo poco después dio 34 conexiones: 32 con otras VMs de la VPC (172.31.0.0/16) y 2 con la misma VM (127.0.0.1). Ninguna con una IP pública.
- **Desde el PC, a la IP pública de cada VM** (4 s de espera por puerto):

  ```text
  vm1  22:abierto  2379:cerrado  2380:cerrado  8443:cerrado  8444:cerrado  9443:cerrado  9444:cerrado
  vm2  22:abierto  2379:cerrado  2380:cerrado  8443:cerrado  8444:cerrado  9443:cerrado  9444:cerrado
  vm3  22:abierto  2379:cerrado  2380:cerrado  8443:cerrado  8444:cerrado  9443:cerrado  9444:cerrado
  vm4  22:abierto  2379:cerrado  2380:cerrado  8443:cerrado  8444:cerrado  9443:cerrado  9444:cerrado
  vm5  22:abierto  2379:cerrado  2380:cerrado  8443:cerrado  8444:cerrado  9443:cerrado  9444:cerrado
  ```

- **Reglas de entrada del grupo `dfsha-sg`:**

  ```text
  tcp 2379-2380 desde el mismo grupo
  tcp 8443-8444 desde el mismo grupo
  tcp 9443-9444 desde el mismo grupo
  tcp 22-22 desde <IP de quien trabaja>/32
  ```

### 5.5 Comprobación 6: reinicio de la VM del líder

**Esperado:** al reiniciar vm2 (`etcd2`, `meta2`, que era el líder, y `dn2`), otro MetaServer pasa a ser líder; mientras el líder nuevo está en su espera (`DFS_LEADER_GRACE_S`, 300 s) no hay órdenes de copia; y los contenedores de vm2 vuelven solos.

**Obtenido** (`dfs status` cada 5 s desde vm5):

| Hora | Desde el reinicio | Líder | DataNodes vivos | Bloques con menos de 3 copias | Órdenes `copy` |
|---|---|---|---|---|---|
| 21:36:51 | 0 s: `reboot-instances` de vm2 | meta2 | 4 | 0 | 0 |
| 21:36:57 | +6 s | **meta1** | 4 | 0 | 0 |
| 21:37:12 | +20 s | meta1 | 3 (dn2 caído) | 144 | 0 |
| 21:37:25 | +34 s | meta1 | 4 | 0 | 0 |
| 21:37:47 | +56 s | meta1 | 4 | 0 | 0 |

- **Líder nuevo en 6 s o menos** (el primer sondeo): al apagarse, `meta2` cedió el liderazgo y ganó el siguiente de la fila.
- **Ninguna orden `copy`.** Los 144 bloques con 2 copias vivas eran los de los archivos del humo, ya borrados pero aún sin limpiar.
- **dn2 volvió a los 34 s.**

Después del reinicio, en vm2 el disco de etcd seguía montado (lo monta `/etc/fstab`), y `dn`, `etcd` (`healthy`) y `meta` llevaban 34 s arriba, sin que nadie los levantara. `./infra/deploy.sh --check` dio todo `ok`, con `meta2` de vuelta en la lista de MetaServers.

### 5.6 Comprobación 7: nueva sesión del laboratorio

**Esperado:** después de End Lab y Start Lab, las VMs arrancan solas, los contenedores vuelven, el sistema queda sano sin redesplegar, no hay re-réplicas durante la espera del líder nuevo, y un archivo guardado antes se baja igual.

**Obtenido:**
- **Antes de End Lab:** `/persist.bin`, de 64 MiB en 8 bloques, con SHA-256 `336cec485d4798cf13a6dfb8d40ecc95734c41d242efddd438b407156fae6d37`.
- **Start Lab:** las 5 VMs arrancaron a las 21:42:49.
- **21:50:15:** `./infra/allow-ssh.sh` (la IP no había cambiado) y `./infra/deploy.sh --check`, todo `ok` al primer intento: etcd con 3 sanos, 3 MetaServers, líder `meta1`, 4 DataNodes vivos en su zona, 0 bloques con menos de 3 copias y 0 órdenes pendientes.
- **Ninguna re-réplica en la sesión.** Una orden `copy` cumplida agrega su destino a `stored_on` sin quitar el origen, así que un bloque copiado tendría más de 3 nodos. En etcd, los 200 bloques registrados tenían exactamente 3, y los bloques de `/persist.bin` estaban en los mismos nodos que antes de End Lab:

  ```text
    bloques: 200 | nodos en stored_on -> cantidad de bloques: {3: 200}
  ```

- **`/persist.bin`:** se bajó con el mismo SHA-256.

  ```text
  /persist.bin: versión 1, 64.0 MiB en 8 bloques, 0.97 s, 65.7 MiB/s -> /tmp/persist-copia.bin
  336cec485d4798cf13a6dfb8d40ecc95734c41d242efddd438b407156fae6d37  /tmp/persist.bin
  336cec485d4798cf13a6dfb8d40ecc95734c41d242efddd438b407156fae6d37  /tmp/persist-copia.bin
  ```

- **Disco de etcd y contenedores:** en vm1, vm2 y vm3 el disco seguía montado (`/dev/nvme1n1  3.9G  123M  3.6G  4% /srv/dfsha/etcd`), y todos los contenedores llevaban 7 u 8 minutos arriba, desde el arranque.

Después se borraron `/persist.bin` y los archivos temporales del contenedor cliente.

## 6. Conclusión

El sistema corre en AWS con los scripts de la tarea 23, y las siete comprobaciones pasan:
- etcd, los MetaServers y los DataNodes quedan sanos y en sus zonas;
- el humo de 512 MiB pasa desde la VM5;
- el tráfico entre nodos va por IP privadas y, desde fuera, solo responde el SSH;
- al reiniciar la VM del líder, otro toma su lugar en segundos, sin órdenes de copia durante su espera, y el nodo se reintegra solo;
- después de End Lab y Start Lab, todo vuelve sin redesplegar y los datos se conservan.

En el camino aparecieron dos problemas. Los dos tienen su arreglo en el repositorio (sección 5.1).

**Decisión sobre el clúster.** El clúster se conserva en el Learner Lab de Edwin para la demostración y el video, y se detiene entre sesiones (End Lab). Cada integrante despliega en su propio laboratorio para sus pruebas: José, las tareas 25 y 26; Santiago, la 27.

**Último paso, pendiente.** Cuando la tarea 24 esté unida a `main`, el clúster se redespliega desde `main`, para que la demostración corra el código oficial:

```bash
./infra/deploy.sh
./infra/deploy.sh --check
```

## 7. Para el video

1. `./infra/create.sh` y `./infra/deploy.sh`: la lista de `ok` y el `dfs status` final.
2. `./infra/ssh.sh vm5 scripts/pruebas/smoke.sh --env aws`: el resumen con `RESULTADO: PASA`.
3. `etcdctl member list -w table` en vm1: los 3 miembros con IP privadas y `https`.
4. Desde el PC, un intento a un puerto del sistema que no responde y el SSH que sí; y las reglas de `dfsha-sg` en la consola de EC2.
5. `aws ec2 reboot-instances` de la VM del líder y `dfs status` cada pocos segundos: el líder cambia, dn2 cae y vuelve, y no aparecen órdenes `copy`.
6. Después de End Lab y Start Lab: `./infra/deploy.sh --check` en verde.

## Pendientes

- **Redesplegar desde `main`** cuando la tarea 24 esté unida (sección 6, "Último paso").
- **`compose.local.yml`** usa una sola etiqueta (`dfsha:local`) para los 8 servicios que se construyen. Con Docker Desktop y Compose 5.3.0 funciona, y por eso no se cambió. Con un Compose que construya los servicios en paralelo sobre el almacén de containerd podría aparecer la misma carrera del problema 2 (sección 5.1). Queda para la auditoría de Santiago.
- **`./infra/destroy.sh`** no se ha usado todavía en AWS: el clúster se conserva.
