# shellcheck shell=bash
# DFSha · funciones comunes de los scripts de infra/ (tarea 23, DA-15).
#
# Se carga con source después de infra/config.sh; no se corre sola.

die() {
  echo "error: $*" >&2
  exit 1
}

info() {
  echo "==> $*"
}

# Opciones de ssh y scp.
#
# Concesión aceptada: no se verifica la llave del servidor (StrictHostKeyChecking=no y
# UserKnownHostsFile=/dev/null). La IP pública de cada VM cambia en cada sesión del Learner Lab:
# una lista de llaves conocidas se llenaría de IP viejas y ssh se negaría a conectar cuando AWS
# le diera a una VM nuestra una IP que antes tuvo otra máquina. El costo es que ssh no detectaría
# a alguien que suplante a la VM en la red entre este PC y AWS. La llave del usuario (labsuser.pem)
# sí se exige, y BatchMode evita que ssh se quede esperando una contraseña.
SSH_OPTS=(
  -i "$SSH_KEY"
  -o StrictHostKeyChecking=no
  -o UserKnownHostsFile=/dev/null
  -o LogLevel=ERROR
  -o BatchMode=yes
  -o ConnectTimeout=10
  -o ServerAliveInterval=30
)

# Comprueba que la AWS CLI tenga credenciales vigentes; si no, explica cómo renovarlas.
# No muestra la identidad: el ARN trae el ID de la cuenta.
require_aws() {
  command -v aws >/dev/null || die "no encuentro la AWS CLI (aws). En WSL: https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html"
  if ! aws sts get-caller-identity >/dev/null 2>&1; then
    cat >&2 <<'EOF'
error: las credenciales de AWS no sirven o vencieron (cambian en cada sesión del Learner Lab).
  1. En el Learner Lab, pulsa Start Lab y espera a que el círculo de AWS quede en verde.
  2. Pulsa AWS Details y, junto a "AWS CLI", Show: aparece un bloque que empieza con [default].
  3. Copia ese bloque completo en ~/.aws/credentials, reemplazando lo que había
     (en WSL: nano ~/.aws/credentials, pegar, Ctrl+O, Enter y Ctrl+X).
  4. Vuelve a correr este script.
EOF
    exit 1
  fi
}

# Comprueba la llave SSH del Learner Lab (DFS_SSH_KEY): que exista y que ssh la acepte.
require_ssh_key() {
  [[ -f $SSH_KEY ]] || die "no encuentro la llave SSH en $SSH_KEY. Descárgala del Learner Lab (AWS Details, Download PEM) y cópiala: cp labsuser.pem ~/.ssh/labsuser.pem && chmod 400 ~/.ssh/labsuser.pem. Si está en otra ruta, usa DFS_SSH_KEY=ruta"
  case $(realpath "$SSH_KEY") in
    /mnt/*) die "la llave $SSH_KEY está en el disco de Windows, donde los permisos no funcionan y ssh la rechaza. Cópiala a WSL: cp '$SSH_KEY' ~/.ssh/labsuser.pem && chmod 400 ~/.ssh/labsuser.pem" ;;
  esac
  local mode
  mode=$(stat -c %a "$SSH_KEY")
  [[ $mode == 400 || $mode == 600 ]] || die "la llave $SSH_KEY tiene permisos $mode y ssh la rechaza. Corre: chmod 400 $SSH_KEY"
}

# Llena VM_ID, VM_STATE, VM_PUBLIC_IP y VM_PRIVATE_IP con las VMs de DFSha que no están
# terminadas, indexadas por su etiqueta DfsVm (vm1..vm5). Una IP que no existe queda en None.
load_vms() {
  declare -gA VM_ID=() VM_STATE=() VM_PUBLIC_IP=() VM_PRIVATE_IP=()
  local listing vm id state public private
  listing=$(aws ec2 describe-instances \
    --filters "Name=tag:Project,Values=$PROJECT" "Name=instance-state-name,Values=pending,running,stopping,stopped" \
    --query "Reservations[].Instances[].[Tags[?Key=='DfsVm']|[0].Value, InstanceId, State.Name, PublicIpAddress, PrivateIpAddress]" \
    --output text)
  while read -r vm id state public private; do
    [[ -n $vm ]] || continue
    [[ -z ${VM_ID[$vm]:-} ]] || die "hay dos VMs con la etiqueta DfsVm=$vm; revísalas en la consola de EC2"
    VM_ID[$vm]=$id
    VM_STATE[$vm]=$state
    VM_PUBLIC_IP[$vm]=$public
    # shellcheck disable=SC2034 # la usan create.sh y deploy.sh
    VM_PRIVATE_IP[$vm]=$private
  done <<<"$listing"
}

# Exige que estas VMs existan y estén encendidas (load_vms primero).
require_running() {
  local vm
  for vm in "$@"; do
    [[ -n ${VM_ID[$vm]:-} ]] || die "no existe $vm. Crea las VMs con ./infra/create.sh"
    [[ ${VM_STATE[$vm]} == running ]] || die "$vm está ${VM_STATE[$vm]}. Al iniciar la sesión del Learner Lab (Start Lab) las VMs arrancan solas; espera un par de minutos y vuelve a intentar"
  done
}

# ID del grupo de seguridad de DFSha, o None si no existe.
security_group_id() {
  aws ec2 describe-security-groups \
    --filters "Name=group-name,Values=$SG_NAME" "Name=tag:Project,Values=$PROJECT" \
    --query 'SecurityGroups[0].GroupId' --output text
}

# vm_ssh vm [comando...]: corre un comando en la VM (o abre una sesión si no hay comando).
vm_ssh() {
  local vm=$1
  shift
  # shellcheck disable=SC2029 # el comando se arma aquí a propósito; la VM lo recibe ya armado
  ssh "${SSH_OPTS[@]}" "$SSH_USER@${VM_PUBLIC_IP[$vm]}" "$@"
}

# vm_scp vm destino origen...: copia archivos y carpetas locales a la VM.
vm_scp() {
  local vm=$1 destination=$2
  shift 2
  scp "${SSH_OPTS[@]}" -q -r "$@" "$SSH_USER@${VM_PUBLIC_IP[$vm]}:$destination"
}
