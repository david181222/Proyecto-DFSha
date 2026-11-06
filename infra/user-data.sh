#!/bin/bash
# DFSha · primer arranque de cada VM: el user-data que pone infra/create.sh (tarea 23, DA-15).
#
# No se corre a mano. cloud-init lo ejecuta una sola vez, como root, al crear la VM:
#   1. instala Docker con los plugins de Compose y buildx (paquetes de Ubuntu 24.04) y git;
#   2. deja Docker activo al arrancar, para que los contenedores (restart: unless-stopped)
#      vuelvan solos cuando la VM se reinicia o empieza otra sesión del Learner Lab;
#   3. agrega al usuario ubuntu al grupo docker, para que deploy.sh y ssh.sh no usen sudo;
#   4. clona el repositorio público en /home/ubuntu/dfsha, como usuario ubuntu.
#
# create.sh reemplaza __REPO_URL__ por la URL de infra/config.sh. Nada secreto va aquí: los
# secretos los copia deploy.sh por scp. Para ver cómo terminó, en la VM:
#   cloud-init status --long        o        less /var/log/cloud-init-output.log
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

# Al primer arranque, las actualizaciones automáticas de Ubuntu pueden tener tomado apt.
apt-get -o DPkg::Lock::Timeout=300 update -q
apt-get -o DPkg::Lock::Timeout=300 install -y -q docker.io docker-compose-v2 docker-buildx git

systemctl enable --now docker
usermod -aG docker ubuntu

sudo -u ubuntu -H git clone --quiet '__REPO_URL__' /home/ubuntu/dfsha
