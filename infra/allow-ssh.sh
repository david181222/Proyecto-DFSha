#!/usr/bin/env bash
# Abre el SSH (puerto 22) de las VMs de DFSha solo a tu IP pública actual (tarea 23, DA-15).
#
# Uso, desde la raíz del repositorio (en Windows, desde WSL):
#   ./infra/allow-ssh.sh
#
# Córrelo cuando cambie tu IP (otra red, otro día): ssh deja de conectar y deploy.sh o ssh.sh
# dicen que la VM no responde. Quita las reglas del 22 que había, de IP anteriores, y deja solo
# la actual. create.sh lo corre al terminar.
set -euo pipefail
cd "$(dirname "$0")/.."
source infra/config.sh
source infra/lib.sh

require_aws
sg=$(security_group_id)
[[ $sg != None ]] || die "no existe el grupo de seguridad $SG_NAME. Crea todo con ./infra/create.sh"

ip=$(curl -fsS --max-time 10 https://checkip.amazonaws.com | tr -d '[:space:]')
[[ $ip =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "no pude averiguar tu IP pública (respuesta: '$ip')"

old=$(aws ec2 describe-security-groups --group-ids "$sg" \
  --query "SecurityGroups[0].IpPermissions[?FromPort==\`22\`].IpRanges[].CidrIp" --output text)
read -ra old_cidrs <<<"$old"
for cidr in "${old_cidrs[@]}"; do
  if [[ $cidr != "$ip/32" && $cidr != None ]]; then
    aws ec2 revoke-security-group-ingress --group-id "$sg" --protocol tcp --port 22 --cidr "$cidr" >/dev/null
    info "quitado el SSH para $cidr"
  fi
done

if [[ " ${old_cidrs[*]} " == *" $ip/32 "* ]]; then
  info "el SSH ya estaba abierto solo a tu IP ($ip/32)"
else
  aws ec2 authorize-security-group-ingress --group-id "$sg" --protocol tcp --port 22 --cidr "$ip/32" >/dev/null
  info "SSH abierto solo a tu IP ($ip/32)"
fi
