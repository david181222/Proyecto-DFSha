#!/usr/bin/env bash
# Pruebas de fallos y de consistencia (tarea 27; Tabla 21: G1, G2, G3, G5, DA-05, DA-12).
#
# Uso, desde la raíz del repositorio:
#   scripts/pruebas/fallos.sh CASO [--env local|aws] [opciones]
#
# Local: en WSL o Git Bash, con compose.local.yml corriendo. AWS: en WSL, en la máquina que
# despliega (usa la AWS CLI, la llave SSH e infra/vm.sh, como los scripts de infra/), con el
# clúster desplegado desde una rama que tenga este script: la línea de tiempo corre en la VM5.
#
# Casos (filas de la Tabla 21):
#   datanode        Detiene dn4 durante un get y durante un put; espera la tercera copia.
#   zona-c          Detiene la zona c (VM3; en local etcd3, meta3 y dn3). Si el líder está en la
#                   zona c, antes lo mueve reiniciando meta3 y espera DFS_LEADER_GRACE_S.
#   zona-b          Detiene la zona b (VM2 y VM4; en local etcd2, meta2, dn2 y dn4) durante
#                   --hold segundos: se lee y se escribe con 2 copias (dn1 y dn3).
#   consistencia    Corta con iptables el puerto 9444 de un secundario (--target) durante un put,
#                   mientras otro cliente lee el archivo una y otra vez (G1, G2 y G3).
#   sesion-antes    Antes del reinicio de sesión: sube un archivo y guarda su SHA-256 y las IP.
#   sesion-despues  Después: línea de tiempo hasta 2 min después de la espera del líder; ninguna
#                   orden copy durante DFS_LEADER_GRACE_S (DA-12), mismas IP y el archivo intacto.
#
# Reinicio de sesión. AWS: sesion-antes, End Lab, Start Lab, credenciales nuevas,
# ./infra/allow-ssh.sh y sesion-despues, lo antes posible. Local: sesion-antes,
# "docker compose -f compose.local.yml stop", "... start" y sesion-despues.
#
# Opciones:
#   --env local|aws   Por defecto, local.
#   --mib N           Tamaño de los archivos. Por defecto 128 en local y 512 en AWS (64 en
#                     consistencia y en sesion-*).
#   --user NOMBRE     Usuario de prueba; se crea si no existe. Por defecto fallos.
#   --fault-after SEG Cuántos segundos después de empezar el get o el put se provoca el fallo.
#                     Por defecto 2. El script avisa si la operación ya había terminado.
#   --hold SEG        Zona b: cuánto se mantiene caída después de leer y escribir. Por defecto 90.
#   --target dnN      Consistencia: el DataNode al que se le corta el tráfico. Por defecto dn4.
#   --wait SEG        Cuánto esperar como máximo la re-réplica y la vuelta a sano. Por defecto 600.
#   --csv ARCHIVO     Línea de tiempo y eventos en CSV. Por defecto
#                     docs/pruebas/datos/27-CASO-ENTORNO.csv (se reemplaza en cada corrida).
#
# Cada caso empieza con el clúster sano y termina igual: todo lo que detuvo vuelve a arrancar y
# las reglas de iptables se quitan, también con Ctrl+C o si algo falla (trap). Al final espera a
# que dfs status muestre 4 DataNodes vivos, líder, 0 bloques con menos de 3 copias y 0 órdenes, y
# revisa los contenedores (local) o corre ./infra/deploy.sh --check (AWS).
#
# Línea de tiempo: timeline.sh corre en segundo plano (en AWS, en la VM5) y los eventos de este
# script se intercalan por hora. En AWS la hora de los eventos se corrige con la diferencia de
# reloj con la VM5. Los archivos de prueba viven en el contenedor client (/tmp/dfs-fallos-*) y
# se borran al final; ninguno va al repositorio.
#
# Restaurar a mano, si el script se corta sin restaurar. Pasa si se cierra la terminal, y en
# Git Bash también con Ctrl+C o kill mientras corre un comando de Docker: en el ensayo, el script
# terminó sin correr su trap. En WSL y en Linux, Ctrl+C sí restaura.
#   local: docker compose -f compose.local.yml start; y, si quedó la regla de la prueba de
#          consistencia, docker compose -f compose.local.yml restart dn4 (su espacio de red se
#          crea de nuevo, sin la regla).
#   AWS:   ./infra/vm.sh start vm2 vm3 vm4; ./infra/ssh.sh vm4 docker compose start dn;
#          ./infra/ssh.sh vm4 sudo iptables -D INPUT -p tcp --dport 9444 -j DROP; y
#          ./infra/deploy.sh --check.
#
# Resultado: PASA, AVISO o FALLA por comprobación; sale con 0 si no hubo FALLA, con 1 si hubo
# alguna y con 2 si las opciones están mal.
set -uo pipefail
export MSYS_NO_PATHCONV=1
cd "$(dirname "$0")/../.." || exit 2
source scripts/pruebas/lib.sh

usage_error() {
  echo "error: $*. El uso está en el comentario del inicio de scripts/pruebas/fallos.sh" >&2
  exit 2
}

# --- Opciones ---

case_name=${1:-}
(($#)) && shift
case $case_name in
  datanode | zona-c | zona-b | consistencia | sesion-antes | sesion-despues) ;;
  *) usage_error "falta el caso o no lo conozco: ${case_name:-(vacío)}" ;;
esac
env_name=local mib="" user=fallos fault_after=2 hold=90 target=dn4 max_wait=600 csv=""
while (($#)); do
  [[ $1 =~ ^--(env|mib|user|fault-after|hold|target|wait|csv)$ ]] || usage_error "opción desconocida: $1"
  (($# >= 2)) || usage_error "falta el valor de $1"
  case $1 in
    --env) env_name=$2 ;;
    --mib) mib=$2 ;;
    --user) user=$2 ;;
    --fault-after) fault_after=$2 ;;
    --hold) hold=$2 ;;
    --target) target=$2 ;;
    --wait) max_wait=$2 ;;
    --csv) csv=$2 ;;
  esac
  shift 2
done
[[ $env_name == local || $env_name == aws ]] || usage_error "--env debe ser local o aws"
if [[ -z $mib ]]; then
  case $case_name in
    consistencia | sesion-*) mib=64 ;;
    *) mib=$([[ $env_name == aws ]] && echo 512 || echo 128) ;;
  esac
fi
[[ $mib =~ ^[1-9][0-9]*$ ]] || usage_error "--mib debe ser un entero positivo"
[[ $user =~ ^[a-z0-9_-]{1,32}$ && $user != admin ]] || usage_error "--user: de 1 a 32 caracteres a-z, 0-9, _ o -, y no admin"
[[ $fault_after =~ ^[0-9]+$ && $hold =~ ^[0-9]+$ && $max_wait =~ ^[1-9][0-9]*$ ]] || usage_error "--fault-after, --hold y --wait son enteros"
[[ $target =~ ^dn[1-4]$ ]] || usage_error "--target debe ser dn1, dn2, dn3 o dn4"
csv=${csv:-docs/pruebas/datos/27-$case_name-$env_name.csv}

# --- Resultados ---

results=() failures=0 warnings=0
pass() { results+=("PASA   $*") && echo "PASA   $*"; }
warn() { results+=("AVISO  $*") && echo "AVISO  $*" && warnings=$((warnings + 1)); }
fail() { results+=("FALLA  $*") && echo "FALLA  $*" && failures=$((failures + 1)); }

# --- Entorno: dónde corre el cliente y cómo se provoca cada fallo ---

aws() { [[ $env_name == aws ]]; }
offset=0 # segundos que el reloj de la VM5 va adelante de este (solo AWS)

if aws; then
  source infra/config.sh
  source infra/lib.sh # trae su propio die e info, iguales en lo que importa
  require_aws
  require_ssh_key
  load_vms
  if [[ $case_name == sesion-despues ]]; then # justo después de Start Lab las VMs pueden seguir arrancando
    for _ in $(seq 60); do
      [[ $(printf '%s
' "${VM_STATE[@]}" | grep -c running) == 5 ]] && break
      sleep 5
      load_vms
    done
  fi
  require_running "${VMS[@]}"
  # on_vm vm comando...: corre el comando en ~/dfsha de esa VM, con cada argumento citado.
  on_vm() {
    local vm=$1
    shift
    vm_ssh "$vm" "cd $REMOTE_DIR && $(printf '%q ' "$@")"
  }
  compose_cmd() { on_vm vm5 docker compose "$@"; }
  secrets=secrets/aws
else
  compose_cmd() { docker compose -f compose.local.yml "$@"; }
  secrets=secrets/local
fi
compose=(compose_cmd) # lo usan client, dfs_as y dfs_input de lib.sh

vm_of() { echo "vm${1//[!0-9]/}"; } # dn3 -> vm3, meta2 -> vm2
declare -A ZONE_LOCAL=([b]="etcd2 meta2 dn2 dn4" [c]="etcd3 meta3 dn3")
declare -A ZONE_VMS=([b]="vm2 vm4" [c]="vm3")

hms() { date -u -d "@$(($(date +%s) + offset))" +%H:%M:%S; }
secs() { # HH:MM:SS -> segundos del día
  local h m s
  IFS=: read -r h m s <<<"$1"
  echo $((10#$h * 3600 + 10#$m * 60 + 10#$s))
}
elapsed() { echo $((($(secs "$2") - $(secs "$1") + 86400) % 86400)); } # de $1 a $2, en segundos

log_dir=$(mktemp -d "${TMPDIR:-/tmp}/dfs-fallos-XXXXXX")
timeline=$log_dir/timeline.txt events=$log_dir/events.txt
: >"$timeline"
: >"$events"
run_dir=/tmp/dfs-fallos-$$ # dentro del contenedor client
stamp=$(date +%Y%m%d-%H%M%S)
started_date=$(date -u +%F)
commit=$(git rev-parse --short HEAD 2>/dev/null || echo desconocido)
state_file=${XDG_CACHE_HOME:-$HOME/.cache}/dfsha/27-sesion-$env_name.env
event_at=""

event() {
  event_at=$(hms)
  echo "$event_at  >> $*" | tee -a "$events"
}

# --- Línea de tiempo ---

timeline_pid=""
start_timeline() {
  local cmd=(scripts/pruebas/timeline.sh --env "$env_name" --every 5 --for 7200)
  if aws; then # si este script muere, ssh se corta y timeline.sh termina al escribir
    on_vm vm5 "${cmd[@]}" >"$timeline" 2>&1 </dev/null &
  else
    "${cmd[@]}" --parent $$ >"$timeline" 2>&1 </dev/null &
  fi
  timeline_pid=$!
}
stop_timeline() {
  [[ -n $timeline_pid ]] || return 0
  kill "$timeline_pid" 2>/dev/null
  if aws; then on_vm vm5 pkill -f 'pruebas/[t]imeline.sh' </dev/null >/dev/null 2>&1; fi
  wait "$timeline_pid" 2>/dev/null
  timeline_pid=""
}

# Deja en t_* los campos de una línea de timeline.sh; falla si es una línea de error.
parse_sample() {
  local a b c d e f
  [[ $1 == *vivos=* ]] || return 1
  read -r t_time a b c d e f <<<"$1"
  t_alive=${a#vivos=} t_dead=${b#caídos=} t_leader=${c#líder=} t_under=${d#menos_de_3=}
  t_copy=${e#copy=} t_delete=${f#delete=}
}

# wait_for SEG condición...: espera hasta SEG segundos una muestra nueva de la línea de tiempo
# que cumpla la condición (un comando que mira t_*). Deja esa muestra en t_*.
wait_for() {
  local deadline=$((SECONDS + $1)) seen line
  shift
  seen=$(wc -l <"$timeline")
  while ((SECONDS < deadline)); do
    tail -n +"$((seen + 1))" "$timeline" >"$log_dir/new.txt"
    while IFS= read -r line; do
      seen=$((seen + 1))
      parse_sample "$line" && "$@" && return 0
    done <"$log_dir/new.txt"
    kill -0 "$timeline_pid" 2>/dev/null || {
      fail "la línea de tiempo se detuvo: $(tail -n 2 "$timeline" | tr '\n' ' ')"
      return 1
    }
    sleep 2
  done
  return 1
}

# first_sample_since HH:MM:SS condición...: la primera muestra desde esa hora que cumple la
# condición, leída de toda la línea de tiempo; la deja en t_*.
first_sample_since() {
  local since line
  since=$(secs "$1")
  shift
  while IFS= read -r line; do
    parse_sample "$line" || continue
    (($(secs "$t_time") >= since)) || continue
    "$@" && return 0
  done <"$timeline"
  return 1
}

# repair_report nodo: después de wait_for repaired_without, cuenta los tiempos desde fault_at.
repair_report() {
  local repaired_at=$t_time dead_at
  first_sample_since "$fault_at" is_dead "$1" && dead_at=$t_time
  pass "tercera copia repuesta sin $1 a las $repaired_at: $(elapsed "$fault_at" "$repaired_at") s después de empezar el fallo ($fault_at) y $(elapsed "$dead_at" "$repaired_at") s después de que dfs status mostró $1 caído ($dead_at); DFS_NODE_LEASE_S=$node_lease, DFS_REREPLICATE_AFTER_S=$rereplicate"
}

# Condiciones para wait_for.
listed() { [[ ,$1, == *",$2,"* ]]; }
is_alive() { listed "$t_alive" "$1"; }
is_dead() { listed "$t_dead" "$1"; }
healthy() {
  [[ $t_dead == - && $t_leader == meta* && $t_under == 0 && $t_copy == 0 && $t_delete == 0 ]] &&
    (($(tr ',' '\n' <<<"$t_alive" | wc -l) == 4))
}
repaired_without() { is_dead "$1" && [[ $t_under == 0 ]]; }
no_under() { [[ $t_under == 0 ]]; }
leader_is_not() { [[ $t_leader == meta* && $t_leader != "$1" ]]; }
has_leader() { [[ $t_leader == meta* ]]; }

# --- Fallos y su restauración ---

stopped="" zones_down="" blocked="" fault_at="" # fault_at: hora en que empezó el último fallo

stop_dn() {
  event "deteniendo $1"
  fault_at=$event_at
  if aws; then on_vm "$(vm_of "$1")" docker compose stop dn; else compose_cmd stop "$1"; fi >/dev/null 2>&1
  stopped+=" $1"
  event "$1 detenido (docker compose stop)"
}
start_dn() {
  if aws; then on_vm "$(vm_of "$1")" docker compose start dn; else compose_cmd start "$1"; fi >/dev/null 2>&1
  stopped=${stopped// $1/}
  event "$1 arrancado otra vez"
}
zone_down() {
  if aws; then
    event "deteniendo la zona $1: ${ZONE_VMS[$1]} (aws ec2 stop-instances)"
    fault_at=$event_at
    # shellcheck disable=SC2086 # la lista de VMs se separa a propósito
    ./infra/vm.sh stop ${ZONE_VMS[$1]} >/dev/null
  else
    event "deteniendo la zona $1: ${ZONE_LOCAL[$1]}"
    fault_at=$event_at
    # shellcheck disable=SC2086
    compose_cmd stop ${ZONE_LOCAL[$1]} >/dev/null 2>&1
  fi
  zones_down+=" $1"
  event "zona $1 detenida"
}
zone_up() {
  if aws; then
    event "encendiendo la zona $1: ${ZONE_VMS[$1]}"
    restore_at=$event_at
    # shellcheck disable=SC2086
    ./infra/vm.sh start ${ZONE_VMS[$1]} >/dev/null && load_vms
  else
    event "arrancando la zona $1: ${ZONE_LOCAL[$1]}"
    restore_at=$event_at
    # shellcheck disable=SC2086
    compose_cmd start ${ZONE_LOCAL[$1]} >/dev/null 2>&1
  fi
  zones_down=${zones_down// $1/}
  event "zona $1 encendida otra vez"
}

# iptables en la red del DataNode. En AWS, en su VM (network_mode: host). En local, en el espacio
# de red de su contenedor, desde un contenedor auxiliar con NET_ADMIN (alpine con iptables, que se
# construye la primera vez); compose.local.yml no cambia.
IPTABLES_IMAGE=dfsha-iptables
NET_RULE=(INPUT -p tcp --dport 9444 -j DROP)
iptables_on() {
  local dn=$1
  shift
  if aws; then
    on_vm "$(vm_of "$dn")" sudo iptables "$@"
  else
    docker run --rm --net "container:$(compose_cmd ps -q "$dn")" --cap-add NET_ADMIN "$IPTABLES_IMAGE" iptables "$@"
  fi
}
block_net() {
  if ! aws && ! docker image inspect "$IPTABLES_IMAGE" >/dev/null 2>&1; then
    printf 'FROM alpine:3.20\nRUN apk add --no-cache iptables\n' | docker build -q -t "$IPTABLES_IMAGE" - >/dev/null ||
      die "no pude construir la imagen auxiliar con iptables"
  fi
  iptables_on "$1" -I "${NET_RULE[@]}" </dev/null || die "no pude poner la regla de iptables en $1"
  blocked=$1
  event "iptables: cortado el puerto 9444 de $1 (lo que le reenvían el primario y las órdenes copy)"
}
unblock_net() {
  iptables_on "$blocked" -D "${NET_RULE[@]}" </dev/null >/dev/null 2>&1
  if iptables_on "$blocked" -C "${NET_RULE[@]}" </dev/null >/dev/null 2>&1; then
    fail "la regla de iptables sigue puesta en $blocked; quítala a mano: iptables -D ${NET_RULE[*]}"
  else
    event "iptables: regla quitada en $blocked"
  fi
  blocked=""
}

restore_all() {
  local item
  [[ -z $blocked ]] || unblock_net
  for item in $zones_down; do zone_up "$item"; done
  for item in $stopped; do start_dn "$item"; done
}

# --- Archivos y comprobaciones ---

uploaded=()
make_file() { # make_file nombre MiB: archivo aleatorio en el contenedor; deja su SHA-256 en sha
  client sh -c "mkdir -p $run_dir && head -c ${2}M /dev/urandom > $run_dir/$1" || die "no pude crear $1 en el contenedor client"
  sha=$(client sha256sum "$run_dir/$1" | cut -d ' ' -f 1)
}
put_file() { # put_file local remoto
  local out
  if out=$(dfs_as writer put "$run_dir/$1" "$2" 2>&1); then
    uploaded+=("$2")
    pass "put de $2 con todo vivo: $(grep 'MiB/s' <<<"$out" | tail -n 1)"
  else
    fail "put de $2: $(error_of "$out")"
    finish
  fi
}
put_now() { # put_now local remoto etiqueta: put con eventos y su salida en el registro; deja put_ok
  local out
  event "empieza $3: put de $2"
  if out=$(dfs_as writer put "$run_dir/$1" "$2" 2>&1); then
    put_ok=true
    uploaded+=("$2")
    event "termina $3: bien"
    pass "$3: $(grep 'MiB/s' <<<"$out" | tail -n 1)"
  else
    put_ok=false
    event "termina $3: error"
    fail "$3: $(error_of "$out") (salida completa en $log_dir/put-$1.txt)"
  fi
  printf '%s
' "$out" >"$log_dir/put-$1.txt"
}
get_sha() { # get_sha remoto: lo baja con la sesión reader y deja su SHA-256 en got
  got=""
  op_out=$(client sh -c "HOME=$run_dir/reader dfs get $1 $run_dir/got.bin && sha256sum $run_dir/got.bin" 2>&1) || return 1
  got=$(tail -n 1 <<<"$op_out" | cut -d ' ' -f 1)
}
check_get() { # check_get remoto sha_esperado etiqueta
  if get_sha "$1" && [[ $got == "$2" ]]; then
    pass "$3: get de $1 terminó bien y el SHA-256 es igual al original (${2:0:16}...)"
  elif [[ -n $got ]]; then
    fail "$3: SHA-256 distinto en $1 (${got:0:16}... en vez de ${2:0:16}...)"
  else
    fail "$3: get de $1: $(error_of "$op_out")"
  fi
}

# check_blocks remoto: lee stat --blocks y deja b_total, b_broken (índices sin 2 copias vivas en 2
# zonas), b_three (bloques con 3 copias vivas) y b_nodes (los nodos vivos de cada bloque).
check_blocks() {
  local status_out blocks_out report index alive zone_count nodes
  b_total=0 b_broken="" b_three=0 b_nodes=""
  status_out=$(dfs_as admin status 2>&1) || return 1
  blocks_out=$(dfs_as writer stat --blocks "$1" 2>&1) || return 1
  report=$(block_report "$(zones_from_status "$status_out")" <<<"$blocks_out")
  while read -r index alive zone_count nodes; do
    [[ -n $index ]] || continue
    b_total=$((b_total + 1))
    ((alive >= 2 && zone_count >= 2)) || b_broken+=" $index"
    ((alive >= 3)) && b_three=$((b_three + 1))
    b_nodes+="$nodes "
  done <<<"$report"
}
check_g1() { # check_g1 remoto etiqueta
  if ! check_blocks "$1"; then
    fail "$2: no pude leer dfs stat --blocks de $1"
  elif ((b_total == 0)); then
    fail "$2: $1 no tiene bloques"
  elif [[ -n $b_broken ]]; then
    fail "$2: G1 roto, bloques sin 2 copias vivas en 2 zonas (índices$b_broken)"
  else
    pass "$2: G1, los $b_total bloques tienen copias vivas en al menos 2 zonas ($b_three con 3 copias)"
  fi
}
check_three() { # check_three remoto etiqueta
  if check_blocks "$1" && ((b_total > 0 && b_three == b_total)); then
    pass "$2: los $b_total bloques de $1 tienen 3 copias vivas"
  else
    fail "$2: $1 tiene $b_three de $b_total bloques con 3 copias vivas"
  fi
}

# during etiqueta fallo...: corre op (un arreglo con el comando) en segundo plano, provoca el
# fallo a los --fault-after segundos y espera la operación. Deja op_status y op_out.
during() {
  local label=$1 pid running=true
  shift
  "${op[@]}" >"$log_dir/op.txt" 2>&1 </dev/null &
  pid=$!
  event "empieza $label"
  sleep "$fault_after"
  kill -0 "$pid" 2>/dev/null || running=false
  "$@"
  wait "$pid"
  op_status=$?
  op_out=$(<"$log_dir/op.txt")
  event "termina $label (código $op_status)"
  $running || warn "$label terminó antes de provocar el fallo; usa un --mib mayor o un --fault-after menor"
}

# --- Final ---

finished=false
final_check() {
  if wait_for "$max_wait" healthy && wait_for 30 healthy; then
    pass "sano otra vez a las $t_time: 4 DataNodes vivos, líder $t_leader, 0 bloques con menos de 3 copias, 0 órdenes"
  else
    fail "el clúster no volvió a estar sano en $max_wait s (última muestra: $(tail -n 1 "$timeline"))"
  fi
  if aws; then
    if ./infra/deploy.sh --check >"$log_dir/check.txt" 2>&1; then
      pass "./infra/deploy.sh --check: todo ok (etcd con 3 miembros sanos)"
    else
      fail "./infra/deploy.sh --check no pasa: $(grep -m 3 FALLA "$log_dir/check.txt" | tr '\n' ' ')"
    fi
  else
    local states running
    states=$(compose_cmd ps --format '{{.Service}} {{.State}} {{.Health}}' 2>/dev/null)
    running=$(grep -c ' running' <<<"$states")
    if ((running == 11)) && (($(grep -c 'running healthy' <<<"$states") == 3)); then
      pass "los 11 contenedores corren y los 3 etcd están healthy"
    else
      fail "contenedores: $running de 11 corriendo; $(tr '\n' ';' <<<"$states")"
    fi
  fi
}

write_csv() {
  [[ -s $timeline ]] || return 0
  mkdir -p "$(dirname "$csv")"
  {
    echo "fecha,hora,tipo,vivos,caidos,lider,menos_de_3,copy,delete,detalle"
    sort -s -k1,1 "$timeline" "$events" | while IFS= read -r line; do
      if parse_sample "$line"; then
        echo "$started_date,$t_time,muestra,${t_alive//,/ },${t_dead//,/ },$t_leader,$t_under,$t_copy,$t_delete,"
      elif [[ $line == *'  >> '* ]]; then
        detail=${line#*  >> }
        echo "$started_date,${line%% *},evento,,,,,,,\"${detail//\"/\'}\""
      elif [[ $line =~ ^[0-9]{2}:[0-9]{2}:[0-9]{2} ]]; then
        detail=${line#*  }
        echo "$started_date,${line%% *},error,,,,,,,\"${detail//\"/\'}\""
      fi
    done
  } >"$csv"
}

finish() {
  $finished && return
  finished=true
  trap - INT TERM
  restore_all
  if [[ $case_name != sesion-antes && -n $timeline_pid ]]; then final_check; fi
  local remote
  for remote in "${uploaded[@]}"; do dfs_as writer rm "$remote" >/dev/null 2>&1 || warn "no se pudo borrar $remote"; done
  client rm -rf "$run_dir" >/dev/null 2>&1
  stop_timeline
  write_csv
  echo
  echo "== Línea de tiempo (UTC; >> son los eventos de este script) =="
  sort -s -k1,1 "$timeline" "$events"
  echo
  echo "== Resumen: $case_name, $env_name, commit $commit, ${mib} MiB, DFS_W=${w:-?}, DFS_REREPLICATE_AFTER_S=${rereplicate:-?}, DFS_LEADER_GRACE_S=${grace:-?} =="
  printf '%s\n' "${results[@]}"
  echo "CSV: $csv; registros: $log_dir"
  if ((failures)); then
    echo "RESULTADO: FALLA ($failures fallas, $warnings avisos)"
    exit 1
  fi
  echo "RESULTADO: PASA ($warnings avisos)"
  exit 0
}
trap 'echo; fail "interrumpido"; finish; exit 1' INT TERM HUP
trap finish EXIT
# Un die (de lib.sh, de infra/lib.sh o de aquí) es una FALLA, y también restaura lo detenido.
die() {
  fail "$*"
  finish
  exit 1
}

# --- Preparación ---

prepare() {
  local tries=0 limit=1
  [[ $case_name == sesion-despues ]] && limit=60 # hasta 5 minutos, mientras todo arranca
  until "${compose[@]}" ps --services --status running 2>/dev/null | grep -qx client; do
    ((++tries < limit)) || die "el contenedor client no está corriendo (local: docker compose -f compose.local.yml up -d; aws: ./infra/deploy.sh --check)"
    sleep 5
  done
  [[ -r $secrets/admin_password ]] || die "no encuentro $secrets/admin_password"
  tries=0
  until dfs_input admin login --user admin <"$secrets/admin_password" >/dev/null 2>&1; do
    ((++tries < limit)) || die "admin no pudo entrar (¿responden los MetaServers?)"
    sleep 5
  done
  if aws; then offset=$(($(on_vm vm5 date +%s </dev/null) - $(date +%s))); fi
  read -r w rereplicate grace node_lease < <(client python -c 'from dfs_common.config import Settings as S; s = S(); print(s.w, s.rereplicate_after_s, s.leader_grace_s, s.node_lease_s)')
  [[ -n $grace ]] || die "no pude leer los tiempos de DA-16 del contenedor client"
  login_sessions "$user" writer reader
  start_timeline
  event "inicio: $case_name en $env_name (DFS_W=$w, DFS_REREPLICATE_AFTER_S=$rereplicate, DFS_LEADER_GRACE_S=$grace)"
}
require_healthy() {
  wait_for 120 healthy || {
    fail "el clúster no está sano al empezar (última muestra: $(tail -n 1 "$timeline")); revísalo antes de probar"
    finish
  }
}

# --- Casos ---

case_datanode() {
  local a=/fallos-dn-a-$stamp.bin b=/fallos-dn-b-$stamp.bin sha_a sha_b
  make_file a.bin "$mib"
  sha_a=$sha
  put_file a.bin "$a"

  info "1. get de $a; dn4 se detiene a los $fault_after s"
  op=(client sh -c "HOME=$run_dir/reader dfs get $a $run_dir/got.bin && sha256sum $run_dir/got.bin")
  during "get de $a" stop_dn dn4
  if ((op_status == 0)) && [[ $(tail -n 1 <<<"$op_out" | cut -d ' ' -f 1) == "$sha_a" ]]; then
    pass "get con dn4 detenido en medio: terminó bien y el SHA-256 es igual al original"
  else
    fail "get con dn4 detenido en medio: $(error_of "$op_out")"
  fi
  start_dn dn4
  wait_for 60 is_alive dn4 || fail "dn4 no volvió a estar vivo en 60 s"

  info "2. put de $b; dn4 se detiene a los $fault_after s"
  make_file b.bin "$mib"
  sha_b=$sha
  op=(dfs_as writer put "$run_dir/b.bin" "$b")
  during "put de $b" stop_dn dn4
  if ((op_status == 0)); then
    uploaded+=("$b")
    pass "put con dn4 detenido en medio: terminó bien: $(grep 'MiB/s' <<<"$op_out" | tail -n 1)"
    check_g1 "$b" "put con dn4 detenido"
    check_get "$b" "$sha_b" "lectura de lo escrito sin dn4"
  else
    fail "put con dn4 detenido en medio: $(error_of "$op_out")"
  fi

  info "3. re-réplica: dn4 sigue detenido"
  if wait_for "$max_wait" repaired_without dn4; then
    repair_report dn4
  else
    fail "en $max_wait s no se repusieron las copias que faltaban sin dn4"
  fi
  check_three "$a" "archivo de antes, sin dn4"
  [[ ${uploaded[*]} == *"$b"* ]] && check_three "$b" "archivo escrito durante el fallo, sin dn4"
  info "4. dn4 vuelve; sus copias cuentan otra vez y el líder borra las que sobran"
  start_dn dn4
}

case_zona_c() {
  local a=/fallos-zc-a-$stamp.bin b=/fallos-zc-b-$stamp.bin sha_a sha_b
  wait_for 30 has_leader
  if [[ $t_leader == meta3 ]]; then
    event "el líder es meta3, en la zona c: se reinicia meta3 para que gane otro"
    if aws; then on_vm vm3 docker compose restart meta; else compose_cmd restart meta3; fi >/dev/null 2>&1
    wait_for 120 leader_is_not meta3 || {
      fail "el líder no salió de la zona c"
      return
    }
    event "nuevo líder: $t_leader; se espera DFS_LEADER_GRACE_S ($grace s) para que pueda re-replicar"
    sleep "$grace"
  fi
  pass "el líder ($t_leader) no está en la zona c"
  make_file a.bin "$mib"
  sha_a=$sha
  put_file a.bin "$a"

  info "1. se detiene la zona c"
  zone_down c
  check_get "$a" "$sha_a" "lectura con la zona c caída"
  make_file b.bin "$mib"
  sha_b=$sha
  put_now b.bin "$b" "escritura con la zona c caída"
  if $put_ok; then
    check_g1 "$b" "escritura con la zona c caída"
    check_get "$b" "$sha_b" "lectura de lo escrito sin la zona c"
  fi

  info "2. re-réplica sin la zona c"
  if wait_for "$max_wait" repaired_without dn3; then
    repair_report dn3
    check_three "$a" "archivo de antes, sin la zona c"
  else
    fail "en $max_wait s no se repusieron las copias sin la zona c"
  fi
  info "3. vuelve la zona c"
  zone_up c
}

case_zona_b() {
  local a=/fallos-zb-a-$stamp.bin b=/fallos-zb-b-$stamp.bin sha_a sha_b up_at nodes
  wait_for 30 has_leader
  event "líder antes del fallo: $t_leader"
  make_file a.bin "$mib"
  sha_a=$sha
  put_file a.bin "$a"

  info "1. se detiene la zona b"
  zone_down b
  wait_for 120 is_dead dn4 && wait_for 60 is_dead dn2
  check_get "$a" "$sha_a" "lectura con la zona b caída"
  make_file b.bin "$mib"
  sha_b=$sha
  put_now b.bin "$b" "escritura con la zona b caída"
  if $put_ok; then
    check_g1 "$b" "escritura con la zona b caída"
    nodes=$(tr ' ' '\n' <<<"$b_nodes" | grep . | sort -u | paste -sd ' ' -)
    if [[ $nodes == "dn1,dn3" || $nodes == "dn3,dn1" || $nodes == "dn1,dn3 dn3,dn1" ]]; then
      pass "los $b_total bloques nuevos quedaron con 2 copias, en dn1 y dn3 (DA-05)"
    else
      warn "nodos de los bloques nuevos: $nodes (se esperaban dn1 y dn3)"
    fi
    check_get "$b" "$sha_b" "lectura de lo escrito sin la zona b"
  fi
  info "2. la zona b sigue caída $hold s"
  sleep "$hold"
  event "fin de la espera con la zona b caída"
  check_get "$a" "$sha_a" "lectura después de $hold s sin la zona b"

  info "3. vuelve la zona b"
  zone_up b
  up_at=$event_at
  if wait_for "$max_wait" no_under; then
    pass "las copias que faltaban volvieron a las $t_time: $(elapsed "$restore_at" "$t_time") s después de empezar a encender la zona b ($restore_at) y $(elapsed "$up_at" "$t_time") s después de que respondió ($up_at)"
    [[ ${uploaded[*]} == *"$b"* ]] && check_three "$b" "archivo escrito sin la zona b"
  else
    fail "en $max_wait s no volvieron las terceras copias después de volver la zona b"
  fi
}

case_consistencia() {
  local f=/fallos-cons-$stamp.bin sha1 sha2 reads=0 old=0 new=0 other=0 late_old=0 put_pid
  make_file v1.bin "$mib"
  sha1=$sha
  make_file v2.bin "$mib"
  sha2=$sha
  put_file v1.bin "$f"

  info "1. iptables corta el puerto 9444 de $target; put de la versión 2 mientras reader lee"
  block_net "$target"
  dfs_as writer put "$run_dir/v2.bin" "$f" >"$log_dir/op.txt" 2>&1 </dev/null &
  put_pid=$!
  event "empieza el put de la versión 2"
  : >"$log_dir/reads.txt"
  while kill -0 "$put_pid" 2>/dev/null; do
    local begin
    begin=$(hms)
    if get_sha "$f"; then
      echo "$begin $(hms) $got" >>"$log_dir/reads.txt"
    else
      echo "$begin $(hms) error" >>"$log_dir/reads.txt"
    fi
  done
  wait "$put_pid"
  op_status=$?
  op_out=$(<"$log_dir/op.txt")
  event "termina el put de la versión 2 (código $op_status)"
  if ((op_status == 0)); then
    pass "put con el tráfico a $target cortado: terminó bien: $(grep 'MiB/s' <<<"$op_out" | tail -n 1)"
  else
    fail "put con el tráfico a $target cortado: $(error_of "$op_out")"
  fi
  # Dos lecturas después del put (close-to-open, G3).
  for _ in 1 2; do
    local begin
    begin=$(hms)
    if get_sha "$f"; then echo "$begin $(hms) $got after" >>"$log_dir/reads.txt"; else echo "$begin $(hms) error after" >>"$log_dir/reads.txt"; fi
  done
  while read -r _ end sum when; do
    reads=$((reads + 1))
    case $sum in
      "$sha1") old=$((old + 1)) && [[ $when == after ]] && late_old=$((late_old + 1)) ;;
      "$sha2") new=$((new + 1)) ;;
      *) other=$((other + 1)) ;;
    esac
    event "lectura que terminó a las $end: $([[ $sum == "$sha1" ]] && echo 'versión 1' || { [[ $sum == "$sha2" ]] && echo 'versión 2'; } || echo "otra cosa: $sum")${when:+ (después del put)}"
  done <"$log_dir/reads.txt"
  if ((other == 0 && reads > 0)); then
    pass "G2: $reads lecturas, todas una versión completa ($old de la 1 y $new de la 2), ninguna mezcla"
  else
    fail "G2: $other de $reads lecturas no son ni la versión 1 ni la 2"
  fi
  if ((late_old == 0)) && [[ $(tail -n 1 "$log_dir/reads.txt" | cut -d ' ' -f 3) == "$sha2" ]]; then
    pass "G3: las lecturas que empezaron después del put ven la versión 2"
  else
    fail "G3: una lectura que empezó después del put no vio la versión 2"
  fi
  if ((old > 0)); then
    pass "mientras duraba el put, los lectores veían la versión 1 ($old lecturas)"
  else
    warn "ninguna lectura alcanzó a terminar durante el put; usa un --mib mayor"
  fi
  check_g1 "$f" "versión 2 con $target cortado"
  local missing
  missing=$(tr ' ' '\n' <<<"$b_nodes" | grep . | grep -vc "$target")
  event "$missing de $b_total bloques de la versión 2 no tienen copia viva en $target"

  info "2. se quita la regla; el líder repone lo que falte"
  unblock_net
  local unblocked_at=$event_at
  if wait_for "$max_wait" no_under; then
    pass "sin bloques con menos de 3 copias a las $t_time, $(elapsed "$unblocked_at" "$t_time") s después de quitar la regla"
    check_three "$f" "versión 2 después de desbloquear"
  else
    fail "en $max_wait s quedaron bloques con menos de 3 copias"
  fi
}

# Lo que dfs status dice de cada DataNode: "dn1=10.88.0.31:9443 ..." (para comparar las IP).
dn_addresses() { dfs_as admin status 2>/dev/null | awk '$2 == "vivo" { printf "%s=%s ", $1, $4 }'; }

case_sesion_antes() {
  local p=/fallos-sesion.bin addresses ips=""
  make_file p.bin "$mib"
  put_file p.bin "$p"
  uploaded=() # este archivo se queda hasta sesion-despues
  addresses=$(dn_addresses)
  if aws; then
    local vm
    for vm in "${VMS[@]}"; do ips+="$vm=${VM_PRIVATE_IP[$vm]} "; done
  fi
  mkdir -p "$(dirname "$state_file")"
  printf 'remote=%q\nsha=%q\naddresses=%q\nips=%q\n' "$p" "$sha" "$addresses" "$ips" >"$state_file"
  pass "guardado $p (${sha:0:16}...) y las direcciones: $addresses"
  if aws; then
    info "Ahora: End Lab; Start Lab; credenciales nuevas; ./infra/allow-ssh.sh; y enseguida scripts/pruebas/fallos.sh sesion-despues --env aws"
  else
    info "Ahora: docker compose -f compose.local.yml stop; ... start; y scripts/pruebas/fallos.sh sesion-despues"
  fi
}

# meta_started metaN: hora (epoch) en que arrancó el contenedor de ese MetaServer.
meta_started() {
  local started
  if aws; then
    # shellcheck disable=SC2016 # el $(...) se expande en la VM, no aquí
    started=$(on_vm "$(vm_of "$1")" sh -c 'docker inspect -f "{{.State.StartedAt}}" $(docker compose ps -q meta)' </dev/null)
  else
    started=$(docker inspect -f '{{.State.StartedAt}}' "$(compose_cmd ps -q "$1")")
  fi
  date -u -d "$started" +%s
}

# La espera del líder se mide desde que arrancó su contenedor: el líder gana la elección después,
# así que esos DFS_LEADER_GRACE_S quedan dentro de su espera. No se usa el log "ganó la elección"
# porque los mensajes INFO del logger dfsha no llegan a docker logs (Pendientes de la tarea 27).
case_sesion_despues() {
  [[ -r $state_file ]] || die "no encuentro $state_file: corre antes sesion-antes"
  local remote sha addresses ips now_ips="" leader start_epoch from to wait_s line
  local samples=0 with_copy=0 with_under=0 first_seen=""
  # shellcheck disable=SC1090
  source "$state_file"
  rm -f "$state_file"
  uploaded=("$remote")
  wait_for 180 has_leader || {
    fail "no hubo líder en 3 minutos"
    return
  }
  leader=$t_leader
  start_epoch=$(meta_started "$leader") || {
    fail "no pude leer cuándo arrancó $leader"
    return
  }
  from=$(date -u -d "@$start_epoch" +%H:%M:%S)
  to=$(date -u -d "@$((start_epoch + grace))" +%H:%M:%S)
  event "el líder es $leader; su contenedor arrancó a las $from, así que su espera termina después de las $to"
  wait_s=$((start_epoch + grace + 120 - $(date +%s) - offset))
  if ((wait_s > 0)); then
    info "observando $wait_s s más: hasta 2 minutos después de la espera del líder ($grace s)"
    sleep "$wait_s"
  fi
  event "fin de la observación"
  while IFS= read -r line; do
    parse_sample "$line" || continue
    (($(secs "$t_time") >= $(secs "$from") && $(secs "$t_time") < $(secs "$to"))) || continue
    samples=$((samples + 1))
    [[ -n $first_seen ]] || first_seen=$t_time
    ((t_copy > 0)) && with_copy=$((with_copy + 1))
    ((t_under > 0)) && with_under=$((with_under + 1))
  done <"$timeline"
  if ((samples == 0)); then
    warn "DA-12: la línea de tiempo empezó después de la espera del líder ($from a $to); corre sesion-despues antes"
  elif ((with_copy == 0)); then
    pass "DA-12: $samples muestras entre $first_seen y $to, dentro de la espera del líder, y ninguna con órdenes copy ($with_under con bloques con menos de 3 copias)"
  else
    fail "DA-12: $with_copy de $samples muestras con órdenes copy durante la espera del líder ($from a $to)"
  fi
  if [[ $(dn_addresses) == "$addresses" ]]; then
    pass "los DataNodes volvieron con las mismas direcciones: $addresses"
  else
    fail "direcciones distintas: antes $addresses, ahora $(dn_addresses)"
  fi
  if aws; then
    local vm
    load_vms
    for vm in "${VMS[@]}"; do now_ips+="$vm=${VM_PRIVATE_IP[$vm]} "; done
    if [[ $now_ips == "$ips" ]]; then pass "mismas IP privadas: $ips"; else fail "IP privadas distintas: antes $ips, ahora $now_ips"; fi
  fi
  check_get "$remote" "$sha" "archivo de la sesión anterior"
}

# --- Principal ---

prepare
if [[ $case_name != sesion-despues ]]; then require_healthy; fi
case $case_name in
  datanode) case_datanode ;;
  zona-c) case_zona_c ;;
  zona-b) case_zona_b ;;
  consistencia) case_consistencia ;;
  sesion-antes) case_sesion_antes ;;
  sesion-despues) case_sesion_despues ;;
esac
finish
