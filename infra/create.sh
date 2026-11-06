#!/usr/bin/env bash
# Crea el grupo de seguridad y las 5 VMs de DFSha (tarea 23; DA-13, DA-15).
#
# Uso, desde la raíz del repositorio (en Windows, desde WSL), con las credenciales del Learner
# Lab en ~/.aws/credentials:
#   ./infra/create.sh
#
# - VMs t3.small con Ubuntu 24.04 en la VPC por defecto de us-east-1, cada una en su zona
#   (infra/config.sh): VM1 a VM3 de control, VM4 con el cuarto DataNode y VM5 para el cliente.
# - Disco de 20 GiB gp3 cifrado en cada VM, y uno de 4 GiB para etcd en las de control. Se
#   borran con la VM. Las VMs y sus discos llevan las etiquetas Project=dfsha y DfsVm=vmN.
# - Grupo de seguridad dfsha-sg: los puertos del sistema (2379-2380, 8443-8444 y 9443-9444)
#   solo se abren a las VMs del mismo grupo; el 22, a tu IP pública (lo hace allow-ssh.sh).
# - Al arrancar, cada VM instala Docker y clona el repositorio (infra/user-data.sh).
#
# Se niega si ya hay VMs con la etiqueta Project=dfsha: para empezar de cero, ./infra/destroy.sh.
# Después: ./infra/deploy.sh.
set -euo pipefail
cd "$(dirname "$0")/.."
source infra/config.sh
source infra/lib.sh

# La imagen más reciente de Ubuntu 24.04: parámetro público de SSM o, si no se puede leer, la
# última que publicó Canonical.
resolve_ami() {
  local ami
  ami=$(aws ssm get-parameter --name "$AMI_SSM_PARAM" --query Parameter.Value --output text 2>/dev/null) || ami=""
  if [[ ! $ami =~ ^ami- ]]; then
    ami=$(aws ec2 describe-images --owners "$AMI_OWNER" \
      --filters "Name=name,Values=$AMI_NAME_PATTERN" Name=state,Values=available \
      --query 'sort_by(Images, &CreationDate)[-1].ImageId' --output text)
  fi
  [[ $ami =~ ^ami- ]] || die "no encontré la imagen de Ubuntu 24.04 en $REGION"
  echo "$ami"
}

# Imprime el ID del grupo dfsha-sg; si no existe, lo crea con las reglas entre VMs del mismo
# grupo. Sus mensajes van a stderr, para que en la salida quede solo el ID.
ensure_security_group() {
  local vpc=$1 sg rule rules=()
  sg=$(security_group_id)
  if [[ $sg != None ]]; then
    info "reutilizo el grupo de seguridad $SG_NAME ($sg)" >&2
    echo "$sg"
    return
  fi
  sg=$(aws ec2 create-security-group --vpc-id "$vpc" --group-name "$SG_NAME" \
    --description "DFSha: system ports only from this group; SSH only from the current IP" \
    --tag-specifications "ResourceType=security-group,Tags=[{Key=Project,Value=$PROJECT},{Key=Name,Value=$SG_NAME}]" \
    --query GroupId --output text)
  for rule in 2379:2380 8443:8444 9443:9444; do
    rules+=("IpProtocol=tcp,FromPort=${rule%:*},ToPort=${rule#*:},UserIdGroupPairs=[{GroupId=$sg}]")
  done
  aws ec2 authorize-security-group-ingress --group-id "$sg" --ip-permissions "${rules[@]}" >/dev/null
  info "creado el grupo de seguridad $SG_NAME ($sg)" >&2
  echo "$sg"
}

require_aws
load_vms
if ((${#VM_ID[@]})); then
  die "ya existen VMs de DFSha (${!VM_ID[*]}). Para desplegar en ellas: ./infra/deploy.sh. Para empezar de cero: ./infra/destroy.sh"
fi

running=$(aws ec2 describe-instances --filters Name=instance-state-name,Values=pending,running \
  --query 'length(Reservations[].Instances[])' --output text)
if ((running + ${#VMS[@]} > MAX_INSTANCES)); then
  die "ya hay $running instancias encendidas; con ${#VMS[@]} más se pasa del límite de $MAX_INSTANCES del Learner Lab, que terminaría las sobrantes"
fi

ami=$(resolve_ami)
root_device=$(aws ec2 describe-images --image-ids "$ami" --query 'Images[0].RootDeviceName' --output text)
vpc=$(aws ec2 describe-vpcs --filters Name=isDefault,Values=true --query 'Vpcs[0].VpcId' --output text)
[[ $vpc != None ]] || die "no hay VPC por defecto en $REGION"
sg=$(ensure_security_group "$vpc")
user_data=$(sed "s|__REPO_URL__|$REPO_URL|" infra/user-data.sh)
info "imagen $ami, VPC $vpc, repositorio $REPO_URL"

ids=()
for vm in "${VMS[@]}"; do
  zone=${VM_ZONE[$vm]}
  subnet=$(aws ec2 describe-subnets \
    --filters "Name=vpc-id,Values=$vpc" "Name=availability-zone,Values=$zone" Name=default-for-az,Values=true \
    --query 'Subnets[0].SubnetId' --output text)
  [[ $subnet != None ]] || die "la VPC por defecto no tiene subred en $zone"
  disks=("DeviceName=$root_device,Ebs={VolumeSize=$ROOT_GIB,VolumeType=gp3,Encrypted=true,DeleteOnTermination=true}")
  if [[ ${VM_ROLE[$vm]} == control ]]; then
    disks+=("DeviceName=$ETCD_DEVICE,Ebs={VolumeSize=$ETCD_GIB,VolumeType=gp3,Encrypted=true,DeleteOnTermination=true}")
  fi
  tags="{Key=Project,Value=$PROJECT},{Key=DfsVm,Value=$vm},{Key=Name,Value=$PROJECT-$vm}"
  # La AWS CLI codifica en base64 el texto de --user-data.
  id=$(aws ec2 run-instances \
    --image-id "$ami" --instance-type "$INSTANCE_TYPE" --key-name "$KEY_NAME" \
    --subnet-id "$subnet" --security-group-ids "$sg" \
    --block-device-mappings "${disks[@]}" \
    --metadata-options HttpTokens=required \
    --tag-specifications "ResourceType=instance,Tags=[$tags]" "ResourceType=volume,Tags=[$tags]" \
    --user-data "$user_data" \
    --query 'Instances[0].InstanceId' --output text)
  info "$vm: $id en $zone (${VM_ROLE[$vm]})"
  ids+=("$id")
done

info "esperando a que las 5 VMs estén encendidas"
aws ec2 wait instance-running --instance-ids "${ids[@]}"
load_vms
printf '\n%-4s  %-10s  %-8s  %-20s  %-15s  %s\n' VM zona perfil instancia "IP privada" "IP pública"
for vm in "${VMS[@]}"; do
  printf '%-4s  %-10s  %-8s  %-20s  %-15s  %s\n' "$vm" "${VM_ZONE[$vm]}" "${VM_ROLE[$vm]}" \
    "${VM_ID[$vm]}" "${VM_PRIVATE_IP[$vm]}" "${VM_PUBLIC_IP[$vm]}"
done
echo

infra/allow-ssh.sh
echo
echo "Listo. Cada VM tarda unos minutos en instalar Docker. Después: ./infra/deploy.sh"
