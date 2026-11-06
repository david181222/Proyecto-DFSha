#!/usr/bin/env bash
# Borra todo lo de DFSha en AWS: las VMs, sus discos y el grupo de seguridad (tarea 23, DA-15).
#
# Uso, desde la raíz del repositorio (en Windows, desde WSL):
#   ./infra/destroy.sh          muestra lo que va a borrar y pide confirmación
#   ./infra/destroy.sh --yes    sin preguntar (también borra secrets/aws/)
#
# No se puede deshacer: los bloques guardados en los DataNodes y los datos de etcd se pierden.
# Los discos se borran con su VM; si quedó alguno suelto con la etiqueta Project=dfsha, también
# se borra. Al final ofrece borrar secrets/aws/: sin las VMs ya no sirve (los certificados llevan
# sus IP), y el siguiente deploy.sh genera unos nuevos.
#
# Es el camino para recrear una VM: borrar, crear (./infra/create.sh) y desplegar de cero.
set -euo pipefail
cd "$(dirname "$0")/.."
source infra/config.sh
source infra/lib.sh

assume_yes=false
case ${1:-} in
  --yes) assume_yes=true ;;
  "") ;;
  *) die "uso: ./infra/destroy.sh [--yes]" ;;
esac

confirm() {
  local answer
  $assume_yes && return 0
  read -r -p "$1 Escribe si para confirmar: " answer
  [[ $answer == si || $answer == sí ]]
}

require_aws
load_vms
sg=$(security_group_id)
volumes=$(aws ec2 describe-volumes --filters "Name=tag:Project,Values=$PROJECT" \
  --query 'Volumes[].[VolumeId, Size, State]' --output text)

if ((${#VM_ID[@]} == 0)) && [[ $sg == None && -z $volumes ]]; then
  info "no hay nada de DFSha en AWS"
else
  echo "Se va a borrar:"
  for vm in "${!VM_ID[@]}"; do
    echo "  VM    $vm  ${VM_ID[$vm]}  (${VM_STATE[$vm]})"
  done
  while read -r volume size state; do
    if [[ -n $volume ]]; then
      echo "  disco $volume  $size GiB  ($state)"
    fi
  done <<<"$volumes"
  [[ $sg == None ]] || echo "  grupo de seguridad $SG_NAME  $sg"
  confirm "Se pierden los datos guardados en DFSha." || die "cancelado; no se borró nada"

  if ((${#VM_ID[@]})); then
    info "terminando las VMs (tarda uno o dos minutos)"
    aws ec2 terminate-instances --instance-ids "${VM_ID[@]}" >/dev/null
    aws ec2 wait instance-terminated --instance-ids "${VM_ID[@]}"
  fi

  # Los discos de las VMs se borran solos (DeleteOnTermination); aquí, los que hayan quedado sueltos.
  loose=$(aws ec2 describe-volumes --filters "Name=tag:Project,Values=$PROJECT" Name=status,Values=available \
    --query 'Volumes[].VolumeId' --output text)
  read -ra loose_ids <<<"$loose"
  for volume in "${loose_ids[@]}"; do
    aws ec2 delete-volume --volume-id "$volume"
    info "borrado el disco suelto $volume"
  done

  if [[ $sg != None ]]; then
    # Justo después de terminar las VMs, AWS puede tardar en soltar sus interfaces de red.
    for attempt in {1..12}; do
      if aws ec2 delete-security-group --group-id "$sg" 2>/dev/null; then
        info "borrado el grupo de seguridad $SG_NAME"
        break
      fi
      ((attempt < 12)) || die "no pude borrar el grupo de seguridad $sg; bórralo en la consola de EC2"
      sleep 10
    done
  fi
fi

if [[ -d $SECRETS_DIR ]] && confirm "¿Borrar también $SECRETS_DIR/ (CA, certificados y claves del clúster de AWS)?"; then
  rm -rf -- "$SECRETS_DIR"
  info "borrado $SECRETS_DIR/"
fi
