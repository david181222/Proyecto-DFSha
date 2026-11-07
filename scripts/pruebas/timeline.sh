#!/usr/bin/env bash
# Línea de tiempo de dfs status para las pruebas de fallos (tarea 27; DA-12, DA-16).
#
# Uso, desde la raíz del repositorio (en Windows, desde WSL o Git Bash; en AWS, en la VM5 dentro
# de ~/dfsha):
#   scripts/pruebas/timeline.sh                       sistema local, cada 5 s, hasta Ctrl+C
#   scripts/pruebas/timeline.sh --env aws --every 5   en la VM5
#
# Opciones:
#   --env local|aws   como en smoke.sh. Por defecto, local.
#   --every SEG       cada cuántos segundos toma dfs status. Por defecto 5.
#   --for SEG         cuánto dura; 0 es hasta Ctrl+C o hasta que lo detengan. Por defecto 0.
#   --parent PID      termina cuando ese proceso ya no existe (lo usa fallos.sh en local).
#
# Escribe una línea por muestra, con la hora UTC de la máquina donde corre:
#   15:02:05  vivos=dn1,dn2,dn3 caídos=dn4 líder=meta1 menos_de_3=12 copy=3 delete=0
# vivos y caídos son los DataNodes; menos_de_3, los bloques con menos de 3 copias vivas; copy y
# delete, las órdenes pendientes de cada tipo. Si dfs status falla (por ejemplo, mientras cambia
# el líder o con un MetaServer caído), la línea dice "error: ..." y la muestra siguiente sigue.
# fallos.sh lee estas líneas, así que su formato es parte del script.
#
# Entra como admin con su propia sesión (un HOME aparte dentro del contenedor client) y vuelve a
# entrar si el JWT vence. La contraseña va por la entrada estándar.
set -uo pipefail
export MSYS_NO_PATHCONV=1
cd "$(dirname "$0")/../.." || exit 2
source scripts/pruebas/lib.sh

env_name=local every=5 duration=0 parent=""

usage_error() {
  echo "error: $*. El uso está en el comentario del inicio de scripts/pruebas/timeline.sh" >&2
  exit 2
}

while (($#)); do
  [[ $1 =~ ^--(env|every|for|parent)$ ]] || usage_error "opción desconocida: $1"
  (($# >= 2)) || usage_error "falta el valor de $1"
  case $1 in
    --env) env_name=$2 ;;
    --every) every=$2 ;;
    --for) duration=$2 ;;
    --parent) parent=$2 ;;
  esac
  shift 2
done
select_env "$env_name" || usage_error "--env debe ser local o aws"
[[ $every =~ ^[1-9][0-9]*$ ]] || usage_error "--every debe ser un entero positivo"
[[ $duration =~ ^[0-9]+$ ]] || usage_error "--for debe ser un entero"

run_dir=/tmp/dfs-timeline-$$
trap 'client rm -rf "$run_dir" >/dev/null 2>&1; exit 0' INT TERM
require_client

login() { dfs_input admin login --user admin <"$secrets/admin_password" >/dev/null 2>&1; }
login || die "admin no pudo entrar"

# Una línea a partir de la salida de dfs status (format_status en src/dfs_client/cli.py).
summarize() {
  awk '
    /^Líder: / { leader = ($2 == "sin" ? "-" : $2) }
    /^DataNodes:/ { in_dn = 1; next }
    in_dn && /^  / { if ($2 == "vivo") alive = alive (alive ? "," : "") $1; else dead = dead (dead ? "," : "") $1; next }
    in_dn && !/^  / { in_dn = 0 }
    /^Bloques con menos de 3 copias vivas: / { under = $NF }
    /^Órdenes pendientes: / { in_orders = 1; next }
    in_orders && /^  / { if ($2 == "copy") copy++; else if ($2 == "delete") del++ }
    END {
      printf "vivos=%s caídos=%s líder=%s menos_de_3=%s copy=%d delete=%d\n",
        (alive ? alive : "-"), (dead ? dead : "-"), leader, under, copy, del
    }'
}

started=$SECONDS
while ((duration == 0 || SECONDS - started < duration)); do
  [[ -z $parent ]] || kill -0 "$parent" 2>/dev/null || break
  now=$(date -u +%H:%M:%S)
  if out=$(dfs_as admin status 2>&1); then
    echo "$now  $(summarize <<<"$out")"
  else
    grep -q E_AUTH <<<"$out" && login
    echo "$now  error: $(error_of "$out")"
  fi
  sleep "$every"
done
client rm -rf "$run_dir" >/dev/null 2>&1
