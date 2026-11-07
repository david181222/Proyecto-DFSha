#!/usr/bin/env bash
# Detiene o enciende VMs de DFSha por nombre y espera a que lleguen a ese estado (tarea 27).
#
# Uso, desde la raíz del repositorio (en Windows, desde WSL):
#   ./infra/vm.sh status            estado de cada VM
#   ./infra/vm.sh stop vm3          detiene la VM3 (prueba de fallos: zona c)
#   ./infra/vm.sh stop vm2 vm4      detiene la zona b
#   ./infra/vm.sh start vm2 vm4     las enciende y espera a que respondan por SSH
#
# Detener una VM no borra nada: sus discos se conservan, y al encenderla tiene la misma IP privada
# y Docker levanta sus contenedores solo (restart: unless-stopped). La IP pública sí cambia, pero
# ssh.sh y deploy.sh la leen cada vez. Lo usa scripts/pruebas/fallos.sh.
set -euo pipefail
cd "$(dirname "$0")/.."
source infra/config.sh
source infra/lib.sh

usage() { die "uso: ./infra/vm.sh status | stop vmN... | start vmN..."; }
(($# >= 1)) || usage
action=$1
shift

require_aws
load_vms
case $action in
  status)
    for vm in "${VMS[@]}"; do echo "$vm  ${VM_ZONE[$vm]}  ${VM_STATE[$vm]:-no existe}"; done
    exit 0
    ;;
  stop | start) (($#)) || usage ;;
  *) usage ;;
esac

ids=()
for vm in "$@"; do
  [[ -n ${VM_ZONE[$vm]:-} ]] || die "no conozco $vm; las VMs son ${VMS[*]}"
  [[ -n ${VM_ID[$vm]:-} ]] || die "no existe $vm. Crea las VMs con ./infra/create.sh"
  ids+=("${VM_ID[$vm]}")
done

if [[ $action == stop ]]; then
  aws ec2 stop-instances --instance-ids "${ids[@]}" >/dev/null
  aws ec2 wait instance-stopped --instance-ids "${ids[@]}"
  info "detenidas: $*"
  exit 0
fi

require_ssh_key
aws ec2 start-instances --instance-ids "${ids[@]}" >/dev/null
aws ec2 wait instance-running --instance-ids "${ids[@]}"
load_vms # la IP pública es nueva
for vm in "$@"; do
  tries=0
  until vm_ssh "$vm" true 2>/dev/null; do
    ((++tries < 36)) || die "$vm está encendida pero no responde por SSH después de 3 minutos; prueba ./infra/allow-ssh.sh"
    sleep 5
  done
done
info "encendidas y con SSH: $*"
