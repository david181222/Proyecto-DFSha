#!/usr/bin/env bash
# Entra por SSH a una VM de DFSha, o corre un comando en ella (tarea 23).
#
# Uso, desde la raíz del repositorio (en Windows, desde WSL):
#   ./infra/ssh.sh vm5                            sesión interactiva, dentro de ~/dfsha
#   ./infra/ssh.sh vm4 docker compose stop dn     un comando, que corre dentro de ~/dfsha
#   ./infra/ssh.sh vm1 'docker compose logs --tail 50 meta'
#
# La IP pública se busca por la etiqueta DfsVm, porque cambia en cada sesión del Learner Lab.
# En cada VM, ~/dfsha/.env ya dice qué archivo y qué perfil de Compose usar, así que basta
# "docker compose ps", "logs", "stop" o "start" con el nombre del servicio: etcd, meta, dn o client.
# La llave es la de DFS_SSH_KEY (por defecto ~/.ssh/labsuser.pem).
set -euo pipefail
cd "$(dirname "$0")/.."
source infra/config.sh
source infra/lib.sh

(($# >= 1)) || die "uso: ./infra/ssh.sh vmN [comando...]"
vm=$1
shift
[[ -n ${VM_ZONE[$vm]:-} ]] || die "no conozco $vm; las VMs son ${VMS[*]}"

require_aws
require_ssh_key
load_vms
require_running "$vm"

if (($#)); then
  vm_ssh "$vm" "cd $REMOTE_DIR && $*"
else
  # -t: una terminal, para la sesión interactiva.
  ssh -t "${SSH_OPTS[@]}" "$SSH_USER@${VM_PUBLIC_IP[$vm]}" "cd $REMOTE_DIR && exec bash -l"
fi
