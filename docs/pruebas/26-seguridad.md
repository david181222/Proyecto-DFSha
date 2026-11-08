# 26 · Seguridad: acceso, canal y almacenamiento

## 1. Objetivo

Comprobar la fila "Seguridad" de la Tabla 21 con dos usuarios, `ana26` y `bruno26`:

1. **Acceso autorizado y denegado:** ana26 lee lo suyo; sin token, 401; con el token de bruno26, ni la ruta, ni el lock, ni el pase de ana26 le sirven (404 o 403); y las rutas `/admin`, solo para el administrador.
2. **Canal:** TLS 1.3 en las APIs públicas, TLS 1.2 rechazado, mTLS obligatorio en las internas, y una captura con tcpdump durante un `put` y un `get` sin ningún dato en claro.
3. **Almacenamiento:** un bloque leído directo del disco de un DataNode es ilegible, y las contraseñas se guardan con Argon2id.

## 2. Qué comprueba

| Comprobación | Garantía o decisión | Fila de la Tabla 21 |
|---|---|---|
| ana26 baja su archivo con el SHA-256 original | DA-10 | Seguridad (acceso autorizado) |
| Sin JWT, o con uno inventado: 401 `E_AUTH` | DA-10 | Seguridad |
| bruno26 no ve ni lee `/secreto.txt` de ana26 | RF1, DA-10 | Seguridad ("ruta de otro usuario") |
| `/admin/*` sin rol de administrador: 403 | DA-10, DA-16 | Seguridad |
| `lock_id` de otro usuario: 403 | DA-08, DA-10 | Seguridad |
| Pase: sin pase 401; de otro bloque, alterado o de otra operación, 403 | DA-10 | Seguridad ("pase de otro usuario") |
| TLS 1.3; TLS 1.2 rechazado; mTLS en 8444 y 9444 | DA-06, RNF6 | Seguridad (canal) |
| tcpdump: el contenido no aparece en claro | DA-06, RNF6 | Seguridad (canal) |
| Bloque en disco cifrado con AES-256-GCM; la clave no está en el disco de datos | DA-09 | Seguridad (almacenamiento) |
| Contraseñas con Argon2id | DA-10 | — |

**"Ruta de otro usuario".** En DFSha no existe: toda ruta se resuelve dentro del espacio del usuario del token (contrato, sección 1). Para bruno26, `/secreto.txt` es una ruta de su propio espacio, que no existe, y responde 404. El 403 aparece donde un usuario sí puede nombrar algo de otro: un `lock_id` o un `upload_id`, las rutas `/admin` y los pases de bloque (contrato, sección 6).

La parte de red en AWS (puertos del sistema cerrados desde fuera, tráfico entre nodos por IP privadas) está en `24-despliegue-aws.md`, sección 5.4.

## 3. Entorno

| | Ensayo local | AWS |
|---|---|---|
| Dónde | `compose.local.yml` en un PC con Windows 11 y Docker Desktop 29.6.1 (Compose 5.3.0); los comandos corren en Git Bash | VM5 (contenedor `client`) y VM1 a VM4 |
| Commit | rama `limpieza-final` sobre `main` en `b81b930`, antes de su primer commit (los cambios de la rama no tocan el comportamiento) | *(pendiente: ensayo de la fase 4)* |
| DA-16 | normales, sin `.env` | normales |
| Fecha | 8 de noviembre de 2026, de 02:06 a 02:12 UTC | — |
| Herramientas | `dfs`, `curl` y `python` del contenedor `client`; `etcdctl` de etcd1; tcpdump en un contenedor auxiliar (alpine con `tcpdump`) que comparte el espacio de red del contenedor observado, como hace `fallos.sh` con iptables. `compose.local.yml` no cambia | `sudo tcpdump` en la VM (red del host) |

## 4. Pasos

Con `C="docker compose -f compose.local.yml"` y una sesión `dfs` por usuario (`$C exec -e HOME=/tmp/s26/USUARIO client dfs login --user USUARIO`, después de `dfs useradd` como admin). Ningún token se imprime: el login por `curl` deja el JWT en un archivo dentro del contenedor (`/tmp/s26/tok-USUARIO`), y los comandos lo leen de ahí.

**Acceso autorizado y denegado:**

```bash
# ana26 sube un archivo de 13,3 MiB con la marca SECRETO-DFSHA-26 en cada línea, y lo baja
$C exec -e HOME=/tmp/s26/ana26 client dfs put /tmp/s26/secreto.txt /secreto.txt
$C exec -e HOME=/tmp/s26/ana26 client sh -c 'dfs get /secreto.txt /tmp/s26/copia.txt && sha256sum /tmp/s26/secreto.txt /tmp/s26/copia.txt'
# Sin token y con un token inventado
$C exec client curl -s --cacert /run/secrets/ca.crt "https://10.88.0.21:8443/api/v1/fs/ls?path=/"
$C exec client curl -s --cacert /run/secrets/ca.crt -H "Authorization: Bearer token-inventado" "https://10.88.0.21:8443/api/v1/fs/ls?path=/"
# bruno26 frente a lo de ana26
$C exec -e HOME=/tmp/s26/bruno26 client dfs ls /
$C exec -e HOME=/tmp/s26/bruno26 client dfs get /secreto.txt /tmp/s26/robado.txt
$C exec -e HOME=/tmp/s26/bruno26 client dfs status
# lock_id de ana26 usado por bruno26: ana26 toma el lock con curl (POST /locks); bruno26 lo renueva (PUT) y lo suelta (DELETE)
```

**Pases de bloque.** bruno26 sube `/b.txt` y pide su manifiesto (`GET /files/blocks`), que trae el pase de lectura de su bloque. Con ese pase, contra el DataNode:

```bash
curl --cacert /run/secrets/ca.crt -H "Authorization: Bearer $PASE_B" https://DN/api/v1/blocks/$HASH_B     # su bloque: 200
curl --cacert /run/secrets/ca.crt https://DN/api/v1/blocks/$HASH_A                                         # sin pase: 401
curl --cacert /run/secrets/ca.crt -H "Authorization: Bearer $PASE_B" https://DN/api/v1/blocks/$HASH_A     # bloque de ana26: 403
curl --cacert /run/secrets/ca.crt -H "Authorization: Bearer $PASE_ALTERADO" https://DN/api/v1/blocks/$HASH_A   # 403
curl --cacert /run/secrets/ca.crt -X PUT -H "Authorization: Bearer $PASE_B" --data-binary @bloque https://DN/api/v1/blocks/$HASH_B   # 403
```

`$PASE_ALTERADO` es el pase de bruno26 con `user` cambiado a `ana26` y `hash` al del bloque de ana26, conservando la firma original (un `python -c` decodifica el JSON en base64url, lo cambia y lo vuelve a codificar).

**Canal:**

```bash
$C exec client curl -sv --cacert /run/secrets/ca.crt -o /dev/null https://10.88.0.21:8443/api/v1/health   # "SSL connection using TLSv1.3"
$C exec client curl --tls-max 1.2 --cacert /run/secrets/ca.crt https://10.88.0.21:8443/api/v1/health       # falla
$C exec client curl --cacert /run/secrets/ca.crt -X POST https://10.88.0.21:8444/api/v1/internal/register  # sin certificado: falla
$C exec dn1 curl --cacert /run/secrets/ca.crt --cert /run/secrets/tls.crt --key /run/secrets/tls.key -X POST https://10.88.0.21:8444/api/v1/internal/register   # con el de dn1: 400 (falta el cuerpo)

# tcpdump en el espacio de red del cliente y en el de dn1, mientras ana26 sube y baja un archivo nuevo de 25,8 MiB con la marca
printf 'FROM alpine:3.20\nRUN apk add --no-cache tcpdump\n' | docker build -q -t dfsha-tcpdump -
docker run --rm --net container:$($C ps -q client) dfsha-tcpdump sh -c \
  "timeout 25 tcpdump -i any -nn -s 0 -w /tmp/c.pcap 'tcp port 8443 or tcp port 9443'; tcpdump -r /tmp/c.pcap -nn -A | grep -c SECRETO-DFSHA-26"
# lo mismo con --net container:$($C ps -q dn1) y 'tcp port 8444 or tcp port 9443 or tcp port 9444'
```

**Almacenamiento:**

```bash
$C exec dn1 sh -c 'cd /data/ana26 && ls -l && head -c 48 HASH | od -A d -c'   # el bloque guardado
$C exec dn1 sh -c 'grep -rl SECRETO-DFSHA-26 /data | wc -l'                   # en dn1 a dn4
$C exec etcd1 etcdctl get /users/ana26 --print-value-only                       # $argon2id$...
```

La salida completa, con la hora UTC de cada línea, está en `datos/26-seguridad-local.txt`.

## 5. Resultado esperado y resultado obtenido

**Esperado:** ana26 lee lo suyo; todo lo demás responde 401 o 403 (o 404 en su propio espacio); el canal negocia TLS 1.3 y no deja ver el contenido; en el disco no está el contenido en claro.

### 5.1 Ensayo local

**Acceso autorizado.**

```text
ana26  /secreto.txt: versión 1, 13.3 MiB en 2 bloques (2 enviados, 0 ya estaban), 0.94 s, 14.2 MiB/s
ana26  87e575f2041b786bda98c410ec6769ca2e622a33f2998dbcd94927d63a03cfb1  /tmp/s26/secreto.txt
ana26  87e575f2041b786bda98c410ec6769ca2e622a33f2998dbcd94927d63a03cfb1  /tmp/s26/secreto-copia.txt
```

**Acceso denegado.**

| Intento | Respuesta |
|---|---|
| `GET /fs/ls` sin token | 401 `E_AUTH`: falta el JWT en Authorization: Bearer |
| `GET /fs/ls` con un token inventado | 401 `E_AUTH`: JWT ausente, inválido o vencido |
| `dfs ls /` sin sesión | `error E_AUTH: Tu sesión no existe o venció: corre dfs login` |
| bruno26: `dfs ls /` | sin entradas: su espacio está vacío; ana26, en cambio, ve `secreto.txt` |
| bruno26: `dfs get /secreto.txt` | `error E_NOT_FOUND: /secreto.txt no existe` |
| bruno26: `dfs status` y `dfs useradd` | 403 `E_FORBIDDEN`: esta ruta exige rol de administrador |
| bruno26: `PUT` y `DELETE /locks/{lock_id de ana26}` | 403 `E_FORBIDDEN`: el lock 8ffa1b8e47fbe8d es de otro usuario (ana26 lo soltó después: 200) |
| Pase de bruno26 contra su propio bloque | 200, con el contenido de `/b.txt` |
| Sin pase | 401 `E_AUTH`: falta el pase del bloque |
| Pase de bruno26 contra el bloque de ana26 | 403 `E_FORBIDDEN`: el pase es de otro bloque |
| Pase de bruno26 alterado (`user` ana26 y el hash de ana26) | 403 `E_FORBIDDEN`: pase con firma inválida |
| Pase de lectura usado en un `PUT` | 403 `E_FORBIDDEN`: el pase es de otra operación |

**Canal.**

```text
curl    * SSL connection using TLSv1.3 / TLS_AES_256_GCM_SHA384 / X25519MLKEM768 / id-ecPublicKey
curl    *  subjectAltName: host "10.88.0.21" matched cert's IP address!
curl    *  SSL certificate verify ok.
curl    --tls-max 1.2 contra 8443 y contra 9443: curl salió con código 35 (falla del handshake)
client  sin certificado de cliente, contra 8444 y contra 9444: curl salió con código 56 (el servidor cortó la conexión)
dn1     con el certificado de dn1, contra 8444: {"error":"E_BAD_REQUEST","message":"body: Field required",...}  (HTTP 400)
```

Captura con tcpdump mientras ana26 subía y bajaba `/secreto3.txt` (27 101 634 bytes, 645 277 líneas con la marca, 4 bloques):

| Dónde | Paquetes | Descartados | Carga TCP | Con la marca en claro | Puertos |
|---|---|---|---|---|---|
| contenedor `client` | 2817 | 0 | 54 469 509 bytes | **0** | 8443: 84; 9443: 2733 |
| `dn1` | 2008 | 0 | 54 480 460 bytes | **0** | 8444: 90; 9443: 1159; 9444: 759 |

En el cliente se capturaron unos 54 MB: lo que suman el `put` y el `get` de 25,8 MiB. Ninguno de esos bytes tiene la marca, que en claro aparece 645 277 veces en el archivo. En dn1 se ve también la réplica entre DataNodes (9444) y la confirmación y los latidos hacia el MetaServer (8444), todo cifrado.

**Almacenamiento.** El bloque 0 de `/secreto.txt` (hash `446669919b7b1197...`), en el cliente y en el disco de dn1:

```text
client  en el cliente: 8388608 bytes, sha256 446669919b7b1197..., 209715 líneas con la marca
client  0000000   S   E   C   R   E   T   O   -   D   F   S   H   A   -   2   6
dn1     en dn1: /data/ana26/446669919b7b1197... 8388636 bytes (8388608 + 12 de nonce + 16 de tag), sha256 del archivo f9cb27b9f312fc11..., líneas con la marca: 0
dn1     0000000 342   X   7   l 325 206 233 200 033  \n   q  \0 036 352 237 023
```

- En los 4 DataNodes, ningún archivo de `/data` tiene la marca en claro (dn1 y dn4 guardan 7 bloques de ana26; dn2 y dn3, 8).
- `/data` de dn1 solo tiene `.disk_id` y una carpeta por usuario. La clave AES del nodo está en `/run/secrets/node_key` (32 bytes), un secreto de Docker, fuera del disco de datos.
- En etcd, la contraseña de ana26 está como `{"password_hash":"$argon2id$v=19$m=65536,t=3,p=4$<sal y hash omitidos>","is_admin":false}`.

Cómo se lee:

- **El tamaño del bloque guardado** es el del bloque más 28 bytes: el nonce de 12 bytes y el tag de 16 de AES-256-GCM (contrato, sección 12). Su SHA-256 no es el de su nombre: el nombre es el hash del contenido en claro (DA-02), y lo guardado es el cifrado.
- **Un archivo cifrado copiado con otro nombre o a la carpeta de otro usuario** no se descifra, porque `"{user}/{hash}"` va como dato asociado; tampoco lo descifra otro DataNode, que tiene otra clave. Lo prueban `test_file_copied_under_another_name_is_422_and_reported` y `test_another_node_key_cannot_read_the_block` en `tests/test_storage.py`; aquí no se repitió.

### 5.2 AWS

*Pendiente: ensayo de la fase 4.*

### 5.3 Lo que se corrigió en el ensayo

En el primer intento de la captura, tcpdump escribía el archivo `.pcap` en una carpeta compartida de Windows. Capturó solo unos 13 MB de unos 54 MB y no informó cuántos paquetes perdió. Tampoco mostró la marca, pero una captura incompleta no sirve como prueba. En la captura que vale, tcpdump escribe dentro de su propio contenedor y anota sus estadísticas: 0 paquetes descartados. Las dos están en el archivo de datos.

## 6. Conclusión

En el ensayo local se cumple lo que piden la Tabla 21 y RNF6. Solo el dueño accede a sus archivos; sin token, la respuesta es 401, y con lo de otro usuario, 404 en su propio espacio o 403. Un pase de bloque no se puede reutilizar para otro bloque ni alterar. El canal usa TLS 1.3 y rechaza versiones anteriores, la API interna exige certificado de cliente, y en 54 MB de tráfico capturado no aparece nada del contenido. En el disco de los DataNodes los bloques están cifrados y la clave está fuera del disco de datos; las contraseñas, con Argon2id. *La corrida en AWS queda para el ensayo de la fase 4.*

## 7. Para el video

1. `curl` sin token: 401. bruno26: `dfs get /secreto.txt` da 404 y `dfs status` da 403.
2. El pase de bruno26 contra el bloque de ana26: 403 "el pase es de otro bloque".
3. `curl -v`: "SSL connection using TLSv1.3"; con `--tls-max 1.2`, falla.
4. tcpdump durante un `put`: la cuenta de la marca en claro da 0.
5. `head -c 48` del bloque en el disco de un DataNode, al lado del mismo bloque en claro en el cliente.
