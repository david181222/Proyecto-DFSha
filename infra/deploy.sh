#!/usr/bin/env bash
# Despliega DFSha en las 5 VMs, o lo actualiza, y comprueba que funcione (tarea 23; DA-11, DA-13,
# DA-15 y DA-16).
#
# Uso, desde la raíz del repositorio (en Windows, desde WSL), con las VMs creadas
# (./infra/create.sh) y encendidas:
#   ./infra/deploy.sh                          rama main, con los tiempos normales de DA-16
#   ./infra/deploy.sh --ref t24-despliegue     otra rama, o un commit, que ya esté en GitHub
#   ./infra/deploy.sh --times pruebas.env      tiempos de DA-16 de un archivo con líneas VAR=valor
#   DFS_W=3 ./infra/deploy.sh                  o de variables del entorno, que mandan sobre el archivo
#   ./infra/deploy.sh --check                  solo la comprobación de salud, sin desplegar
#
# Qué hace:
#   1. Encuentra las VMs por sus etiquetas y lee sus IP privadas.
#   2. Genera la CA, los certificados y los secretos en secrets/aws/ (git lo ignora), con
#      scripts/gen_secrets.py y esas IP. Si ya existen con las mismas IP, los reutiliza. Nunca
#      los regenera: una node_key nueva dejaría ilegibles los bloques guardados (DA-09).
#   3. En cada VM, en paralelo y con su log:
#      - espera a que termine el user-data (Docker instalado y repositorio clonado);
#      - en las VMs de control, formatea el disco de etcd, solo la primera vez, y lo monta;
#      - copia por scp solo sus secretos, a ~/dfsha/secrets/aws/, con permisos 700 y 600;
#      - escribe ~/dfsha/.env: perfil de Compose, nodos, IP, zona, semillas, clúster de etcd y
#        tiempos de DA-16;
#      - deja el repositorio en la rama o el commit pedido (git fetch y checkout) y corre
#        docker compose up -d --build.
#   4. Comprueba la salud: 3 miembros de etcd sanos (desde la VM1); y, desde el cliente de la
#      VM5, que los 3 MetaServers y los 4 DataNodes respondan por TLS, que /cluster/metaservers
#      liste los 3, y que dfs status muestre un líder y los 4 DataNodes vivos, cada uno en su zona.
#
# Requisitos en la máquina que despliega: AWS CLI, ssh y Python 3 con cryptography (en WSL:
# sudo apt install python3-cryptography). La llave del Learner Lab, en DFS_SSH_KEY o en
# ~/.ssh/labsuser.pem con chmod 400.
set -euo pipefail
cd "$(dirname "$0")/.."
source infra/config.sh
source infra/lib.sh

# --- Opciones ---

ref=main
times_file=""
check_only=false
while (($#)); do
  case $1 in
    --ref)
      (($# >= 2)) || die "falta la rama o el commit de --ref"
      ref=$2
      shift 2
      ;;
    --times)
      (($# >= 2)) || die "falta el archivo de --times"
      times_file=$2
      shift 2
      ;;
    --check)
      check_only=true
      shift
      ;;
    *) die "opción desconocida: $1 (el uso está en el comentario del inicio de infra/deploy.sh)" ;;
  esac
done
[[ $ref =~ ^[A-Za-z0-9._/-]+$ ]] || die "--ref inválida: $ref"

# --- Tiempos de DA-16: los de --times y, encima, los de las variables del entorno ---

declare -A times=()

is_time_var() {
  local var
  for var in "${TIME_VARS[@]}"; do
    if [[ $var == "$1" ]]; then return 0; fi
  done
  return 1
}

read_times_file() {
  local line name value
  [[ -f $1 ]] || die "no existe el archivo $1"
  while IFS= read -r line || [[ -n $line ]]; do
    line=${line%$'\r'} # por si el archivo se guardó en Windows
    if [[ $line =~ ^[[:space:]]*(#.*)?$ ]]; then continue; fi
    [[ $line =~ ^([A-Z0-9_]+)=(.*)$ ]] || die "$1: línea inválida: $line"
    name=${BASH_REMATCH[1]}
    value=${BASH_REMATCH[2]}
    if ! is_time_var "$name"; then
      echo "aviso: $1: se ignora $name, que no es un tiempo de DA-16" >&2
      continue
    fi
    if [[ -z $value ]]; then continue; fi # vacío: el valor por defecto
    [[ $value =~ ^[0-9]+$ ]] || die "$1: $name=$value no es un número"
    times[$name]=$value
  done <"$1"
}

if [[ -n $times_file ]]; then read_times_file "$times_file"; fi
for var in "${TIME_VARS[@]}"; do
  if [[ -n ${!var:-} ]]; then
    [[ ${!var} =~ ^[0-9]+$ ]] || die "$var=${!var} no es un número"
    times[$var]=${!var}
  fi
done

# --- VMs y direcciones (DA-11): las mismas en los certificados y en la configuración ---

require_aws
require_ssh_key
load_vms
require_running "${VMS[@]}"

nodes="" seeds="" endpoints="" initial_cluster="" dn_ips="" dn_zones=""
for vm in "${VMS[@]}"; do
  ip=${VM_PRIVATE_IP[$vm]}
  for node in ${VM_NODES[$vm]}; do
    nodes+="$node=$ip,"
    case $node in
      etcd*)
        endpoints+="https://$ip:2379,"
        initial_cluster+="$node=https://$ip:2380,"
        ;;
      meta*) seeds+="$ip," ;;
      dn*)
        dn_ips+="$ip,"
        dn_zones+="$node=${VM_ZONE[$vm]},"
        ;;
    esac
  done
done
nodes=${nodes%,} seeds=${seeds%,} endpoints=${endpoints%,}
initial_cluster=${initial_cluster%,} dn_ips=${dn_ips%,} dn_zones=${dn_zones%,}

# Nombre del nodo de un tipo (etcd, meta o dn) que corre en la VM; vacío si no tiene.
node_named() {
  local node
  for node in ${VM_NODES[$1]}; do
    if [[ $node == "$2"* ]]; then echo "$node"; fi
  done
}

# --- Secretos (DA-10, DA-15) ---

prepare_secrets() {
  if [[ -d $SECRETS_DIR ]]; then
    [[ -f $SECRETS_DIR/nodes.txt ]] || die "$SECRETS_DIR/ existe pero está incompleta (falta nodes.txt). Si no hay un clúster con datos que conservar, bórrala (rm -r $SECRETS_DIR) y vuelve a desplegar"
    if [[ $(<"$SECRETS_DIR/nodes.txt") != "$nodes" ]]; then
      die "las IP privadas de las VMs no son las de $SECRETS_DIR/ (¿se recrearon las VMs?). Los certificados llevan las IP, así que no sirven, y no los regenero solo: la node_key de cada DataNode es la única forma de leer sus bloques. Para un clúster nuevo, el camino es ./infra/destroy.sh (borra también $SECRETS_DIR/), ./infra/create.sh y ./infra/deploy.sh"
    fi
    info "reutilizo los secretos de $SECRETS_DIR/ (mismas IP privadas)"
    return
  fi
  "$PYTHON" -c 'import cryptography' 2>/dev/null ||
    die "$PYTHON no tiene el paquete cryptography, que usa scripts/gen_secrets.py. En WSL: sudo apt install python3-cryptography (o DFS_PYTHON=ruta de un Python que lo tenga)"
  info "generando la CA, los certificados y los secretos en $SECRETS_DIR/"
  "$PYTHON" scripts/gen_secrets.py --out "$SECRETS_DIR" --nodes "$nodes" >/dev/null
  echo "$nodes" >"$SECRETS_DIR/nodes.txt"
}

# Lo que necesita cada VM de secrets/aws/. La CA privada (ca.key) nunca sale de esta máquina.
secret_files() {
  case ${VM_ROLE[$1]} in
    control) echo "ca.crt jwt_secret pass_key admin_password ${VM_NODES[$1]}" ;;
    data) echo "ca.crt pass_key ${VM_NODES[$1]}" ;;
    client) echo "ca.crt admin_password" ;; # admin_password: la comprobación de salud y las pruebas
  esac
}

# --- Despliegue de una VM ---

# ~/dfsha/.env de la VM: lo lee docker compose (COMPOSE_FILE, COMPOSE_PROFILES y las variables
# de compose.aws.yml). No lleva secretos.
vm_env() {
  local vm=$1 var
  cat <<EOF
# Escrito por infra/deploy.sh: configuración de $vm. Git lo ignora; no se edita a mano.
COMPOSE_FILE=compose.aws.yml
COMPOSE_PROFILES=${VM_ROLE[$vm]}
DFS_NODE_IP=${VM_PRIVATE_IP[$vm]}
DFS_ZONE=${VM_ZONE[$vm]}
ETCD_NAME=$(node_named "$vm" etcd)
META_ID=$(node_named "$vm" meta)
DN_ID=$(node_named "$vm" dn)
DFS_META_SEEDS=$seeds
DFS_ETCD_ENDPOINTS=$endpoints
ETCD_INITIAL_CLUSTER=$initial_cluster
EOF
  for var in "${TIME_VARS[@]}"; do
    if [[ -n ${times[$var]:-} ]]; then echo "$var=${times[$var]}"; fi
  done
}

wait_for_ssh() {
  for _ in {1..30}; do
    if vm_ssh "$1" true 2>/dev/null; then return 0; fi
    sleep 10
  done
  die "$1 no responde por SSH. Si tu IP pública cambió, corre ./infra/allow-ssh.sh"
}

# Los scripts que corren en la VM le llegan por la entrada de bash -s. Por eso cada comando que lee
# la entrada (docker compose exec, up) la recibe de /dev/null: si no, se comería el resto del script.
deploy_vm() {
  local vm=$1 volume file files paths=()
  wait_for_ssh "$vm"

  info "$vm: esperando a que termine el user-data"
  vm_ssh "$vm" bash -s <<'EOF'
cloud-init status --wait >/dev/null
code=$?
# 0: terminó bien; 2: terminó con avisos que no impiden seguir.
if [ "$code" -ne 0 ] && [ "$code" -ne 2 ]; then cloud-init status --long; exit 1; fi
EOF

  # Desde aquí, conexiones nuevas: ya entran con el grupo docker que agregó el user-data.
  if [[ ${VM_ROLE[$vm]} == control ]]; then
    volume=$(aws ec2 describe-instances --instance-ids "${VM_ID[$vm]}" \
      --query "Reservations[0].Instances[0].BlockDeviceMappings[?DeviceName=='$ETCD_DEVICE'].Ebs.VolumeId | [0]" \
      --output text)
    [[ $volume == vol-* ]] || die "$vm no tiene el disco de etcd ($ETCD_DEVICE)"
    info "$vm: montando el disco de etcd $volume en $ETCD_MOUNT"
    # En las t3 los discos son NVMe y su orden puede cambiar; el enlace by-id lleva el ID del volumen.
    vm_ssh "$vm" bash -s -- "$volume" "$ETCD_MOUNT" <<'EOF'
set -euo pipefail
volume=$1 mnt=$2
dev=/dev/disk/by-id/nvme-Amazon_Elastic_Block_Store_${volume/-/}
for _ in $(seq 30); do [ -e "$dev" ] && break; sleep 2; done
[ -e "$dev" ] || { echo "no aparece el disco $volume en $dev" >&2; exit 1; }
if ! sudo blkid "$dev" >/dev/null; then
  echo "formateando el disco de etcd $volume (solo la primera vez)"
  sudo mkfs.ext4 -q -L dfsha-etcd "$dev" </dev/null
fi
uuid=$(sudo blkid -s UUID -o value "$dev")
sudo mkdir -p "$mnt"
if ! grep -q "UUID=$uuid" /etc/fstab; then
  # nofail: si algún día el disco no aparece, la VM arranca igual (y etcd no, ver compose.aws.yml).
  echo "UUID=$uuid $mnt ext4 defaults,nofail 0 2" | sudo tee -a /etc/fstab >/dev/null
  sudo systemctl daemon-reload
fi
mountpoint -q "$mnt" || sudo mount "$mnt"
sudo mkdir -p "$mnt/data"
EOF
  fi

  read -ra files <<<"$(secret_files "$vm")"
  for file in "${files[@]}"; do paths+=("$SECRETS_DIR/$file"); done
  info "$vm: copiando sus secretos (${files[*]})"
  vm_ssh "$vm" "umask 077 && rm -rf $REMOTE_DIR/$SECRETS_DIR && mkdir -p $REMOTE_DIR/$SECRETS_DIR"
  vm_scp "$vm" "$REMOTE_DIR/$SECRETS_DIR/" "${paths[@]}"
  vm_ssh "$vm" "find $REMOTE_DIR/secrets -type d -exec chmod 700 {} + && find $REMOTE_DIR/secrets -type f -exec chmod 600 {} +"

  info "$vm: escribiendo ~/$REMOTE_DIR/.env"
  vm_env "$vm" | vm_ssh "$vm" "cat > $REMOTE_DIR/.env"

  info "$vm: actualizando el repositorio y levantando los contenedores"
  vm_ssh "$vm" bash -s -- "$REMOTE_DIR" "$ref" <<'EOF'
set -euo pipefail
cd "$1"
git fetch --quiet origin
# Una rama de GitHub (origin/rama) o, si no lo es, un commit.
if git rev-parse --verify --quiet "origin/$2^{commit}" >/dev/null; then target=origin/$2; else target=$2; fi
git checkout --quiet --detach "$target"
echo "commit $(git rev-parse --short HEAD) ($2)"
docker compose up -d --build </dev/null
docker compose ps </dev/null
EOF
}

# --- Comprobación de salud ---

# Corre en la VM5, desde ~/dfsha: comprobaciones desde el contenedor cliente. Una sola pasada;
# termina en 0 si todo está bien.
health_script() {
  cat <<'EOF'
cd "$1" || exit 1
password_file=$2 metas=$3 dns=$4 zones=$5
fail=0 status=""
ok() { echo "ok     $*"; }
bad() { echo "FALLA  $*"; fail=1; }
client() { docker compose exec -T -e HOME=/tmp/dfs-check client "$@" </dev/null; }
for ip in ${metas//,/ }; do
  if client curl -fsS --cacert /run/secrets/ca.crt "https://$ip:8443/api/v1/health" >/dev/null 2>&1; then
    ok "MetaServer $ip:8443 responde por TLS"
  else
    bad "MetaServer $ip:8443 no responde"
  fi
done
for ip in ${dns//,/ }; do
  if client curl -fsS --cacert /run/secrets/ca.crt "https://$ip:9443/api/v1/health" >/dev/null 2>&1; then
    ok "DataNode $ip:9443 responde por TLS"
  else
    bad "DataNode $ip:9443 no responde"
  fi
done
listed=$(client curl -fsS --cacert /run/secrets/ca.crt "https://${metas%%,*}:8443/api/v1/cluster/metaservers" 2>/dev/null | grep -o '"meta_id"' | wc -l)
if [ "$listed" -eq 3 ]; then ok "/cluster/metaservers lista los 3 MetaServers"; else bad "/cluster/metaservers lista $listed MetaServers"; fi
# La contraseña entra por la entrada estándar, sin quedar en la línea de comandos.
if docker compose exec -T -e HOME=/tmp/dfs-check client dfs login --user admin <"$password_file" >/dev/null 2>&1 &&
  status=$(client dfs status 2>&1); then
  leader=$(echo "$status" | sed -n 's/^Líder: //p')
  case $leader in meta*) ok "líder: $leader" ;; *) bad "no hay líder (${leader:-sin respuesta})" ;; esac
  for pair in ${zones//,/ }; do
    node=${pair%%=*} zone=${pair#*=}
    seen=$(echo "$status" | awk -v node="$node" '$1 == node && $2 == "vivo" {print $3}')
    if [ "$seen" = "$zone" ]; then ok "$node vivo en $zone"; else bad "$node no está vivo en $zone (dfs status: ${seen:-caído o ausente})"; fi
  done
  printf '\n%s\n' "$status"
else
  bad "no pude entrar como admin y correr dfs status ${status:+: $status}"
fi
exit "$fail"
EOF
}

# Repite la comprobación cada 15 s hasta que todo esté bien, o hasta 5 minutos: al primer
# despliegue los MetaServers se reinician hasta que etcd tiene mayoría (compose.aws.yml).
check_health() {
  local deadline=$((SECONDS + 300)) etcd_out health_out etcd_ok health_ok
  info "comprobando la salud: etcd desde vm1, lo demás desde el cliente de vm5 (hasta 5 minutos)"
  while true; do
    etcd_ok=false health_ok=false
    if etcd_out=$(vm_ssh vm1 "cd $REMOTE_DIR && docker compose exec -T etcd etcdctl endpoint health --cluster" </dev/null 2>&1) &&
      [[ $(grep -c 'is healthy' <<<"$etcd_out") -eq 3 ]]; then
      etcd_ok=true
    fi
    if health_out=$(health_script | vm_ssh vm5 bash -s -- "$REMOTE_DIR" "$SECRETS_DIR/admin_password" "$seeds" "$dn_ips" "$dn_zones" 2>&1); then
      health_ok=true
    fi
    if { $etcd_ok && $health_ok; } || ((SECONDS >= deadline)); then break; fi
    sleep 15
  done
  echo "$etcd_out"
  if $etcd_ok; then echo "ok     etcd: 3 miembros sanos, por TLS"; else echo "FALLA  etcd: no están los 3 miembros sanos"; fi
  echo "$health_out"
  $etcd_ok && $health_ok
}

if $check_only; then
  check_health || die "el sistema no está sano (detalle arriba)"
  exit 0
fi

# --- Despliegue ---

if ((${#times[@]})); then
  summary=""
  for var in "${TIME_VARS[@]}"; do
    if [[ -n ${times[$var]:-} ]]; then summary+=" $var=${times[$var]}"; fi
  done
  info "tiempos de DA-16:$summary (los demás, normales)"
else
  info "tiempos de DA-16: los normales de la Tabla 19"
fi
prepare_secrets

logs=$(mktemp -d)
info "desplegando en las 5 VMs a la vez; el log de cada una queda en $logs/"
declare -A pids=()
for vm in "${VMS[@]}"; do
  deploy_vm "$vm" >"$logs/$vm.log" 2>&1 &
  pids[$vm]=$!
done
failed=()
for vm in "${VMS[@]}"; do
  if wait "${pids[$vm]}"; then
    info "$vm listo: $(grep '^commit ' "$logs/$vm.log" | tail -n 1)"
  else
    failed+=("$vm")
  fi
done
if ((${#failed[@]})); then
  for vm in "${failed[@]}"; do
    echo "--- $vm: últimas líneas de $logs/$vm.log"
    tail -n 25 "$logs/$vm.log"
  done
  die "falló el despliegue en: ${failed[*]}"
fi

check_health || die "el despliegue terminó, pero el sistema no está sano (detalle arriba; logs en $logs/)"
echo
info "desplegado en $((SECONDS / 60)) min $((SECONDS % 60)) s"
echo "Para entrar a una VM: ./infra/ssh.sh vm5 (o vm1..vm4)"
