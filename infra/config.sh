# shellcheck shell=bash disable=SC2034
# DFSha · configuración común de los scripts de AWS (tarea 23; DA-13, DA-15).
#
# Solo valores. La cargan con source create.sh, allow-ssh.sh, destroy.sh, deploy.sh y ssh.sh.
# Nada de aquí cambia entre sesiones del Learner Lab ni depende de una cuenta: las IP y los IDs
# de las VMs se buscan cada vez por sus etiquetas.
#
# Se pueden cambiar sin editar este archivo:
#   DFS_SSH_KEY   llave privada del Learner Lab (por defecto ~/.ssh/labsuser.pem)
#   DFS_REPO_URL  repositorio público que clonan las VMs (por defecto, el del equipo)
#   DFS_PYTHON    Python con cryptography para scripts/gen_secrets.py (por defecto python3)

# --- AWS ---
REGION=us-east-1
export AWS_REGION=$REGION AWS_DEFAULT_REGION=$REGION
export AWS_PAGER=""        # que la AWS CLI no abra un paginador
INSTANCE_TYPE=t3.small
MAX_INSTANCES=9            # límite del Learner Lab: las instancias de más se terminan
PROJECT=dfsha              # etiqueta Project de las VMs, sus discos y el grupo de seguridad
SG_NAME=dfsha-sg
KEY_NAME=vockey            # par de llaves que trae el Learner Lab; su llave privada es labsuser.pem

# --- Sistema de las VMs: Ubuntu 24.04 LTS, la imagen más reciente (sin ID fijo) ---
AMI_SSM_PARAM=/aws/service/canonical/ubuntu/server/24.04/stable/current/amd64/hvm/ebs-gp3/ami-id
AMI_OWNER=099720109477     # Canonical, por si el laboratorio no deja leer el parámetro de SSM
AMI_NAME_PATTERN='ubuntu/images/hvm-ssd-gp3/ubuntu-noble-24.04-amd64-server-*'

# --- Discos (DA-13): todos gp3 y cifrados ---
ROOT_GIB=20
ETCD_GIB=4                 # solo en las VMs de control
ETCD_DEVICE=/dev/sdf       # nombre en la reserva de discos; en las t3 aparece como NVMe
ETCD_MOUNT=/srv/dfsha/etcd # donde deploy.sh monta ese disco; compose.aws.yml usa su carpeta data

# --- Acceso y repositorio ---
SSH_USER=ubuntu
SSH_KEY=${DFS_SSH_KEY:-$HOME/.ssh/labsuser.pem}
REPO_URL=${DFS_REPO_URL:-https://github.com/david181222/Proyecto-DFSha.git}
REMOTE_DIR=dfsha           # clon en /home/ubuntu/dfsha (lo crea infra/user-data.sh)
SECRETS_DIR=secrets/aws    # en la máquina que despliega y en cada VM; git lo ignora
PYTHON=${DFS_PYTHON:-python3}

# --- Topología (DA-13) ---
VMS=(vm1 vm2 vm3 vm4 vm5)
declare -A VM_ZONE=([vm1]=us-east-1a [vm2]=us-east-1b [vm3]=us-east-1c [vm4]=us-east-1b [vm5]=us-east-1a)
# Perfil de compose.aws.yml: control (etcd, MetaServer y DataNode), data (DataNode) o client.
declare -A VM_ROLE=([vm1]=control [vm2]=control [vm3]=control [vm4]=data [vm5]=client)
declare -A VM_NODES=([vm1]="etcd1 meta1 dn1" [vm2]="etcd2 meta2 dn2" [vm3]="etcd3 meta3 dn3" [vm4]="dn4" [vm5]="")

# --- Tiempos y umbrales de DA-16 que deploy.sh lleva a las VMs ---
# Los mismos del bloque x-dfs-times de compose.local.yml y compose.aws.yml (tests/test_infra.py).
TIME_VARS=(
  DFS_BLOCK_MIB DFS_W DFS_PRIMARY_WAIT_S DFS_LOCK_LEASE_S DFS_HEARTBEAT_S DFS_NODE_LEASE_S
  DFS_REREPLICATE_AFTER_S DFS_LEADER_GRACE_S DFS_RETENTION_S DFS_GC_INTERVAL_S DFS_GC_MIN_AGE_S
  DFS_LOCK_RENEW_S DFS_GC_CLOCK_MARGIN_S DFS_LEADER_SCAN_S DFS_JWT_TTL_S DFS_PASS_TTL_S
)
