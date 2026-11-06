#!/usr/bin/env bash
# Prueba de humo de DFSha: put y get de un archivo aleatorio, con sus comprobaciones (tarea 22).
#
# Uso, desde la raíz del repositorio (en Windows, desde WSL; en AWS, en la VM5 dentro de ~/dfsha):
#   scripts/pruebas/smoke.sh                     sistema local (compose.local.yml), 512 MiB
#   scripts/pruebas/smoke.sh --mib 64            otro tamaño
#   scripts/pruebas/smoke.sh --env aws           en la VM5 (compose.aws.yml, lo dice ~/dfsha/.env)
#   scripts/pruebas/smoke.sh --csv docs/pruebas/datos/x.csv --rep 3
#
# Opciones:
#   --env local|aws      local: compose.local.yml y secrets/local/; aws: el .env que escribió
#                        infra/deploy.sh y secrets/aws/. Por defecto, local.
#   --mib N              tamaño del archivo, en MiB. Por defecto 512 (la prueba oficial, Tabla 21).
#   --user NOMBRE        usuario de prueba; se crea si no existe. Por defecto smoke.
#   --wait-copies SEG    si un bloque quedó con 2 copias, cuánto esperar la tercera. Por defecto 30.
#   --csv ARCHIVO        agrega una fila por operación (put y get) a ese CSV.
#   --rep N              número de repetición para el CSV. Por defecto 1.
#
# Pasos:
#   1. Entra como admin y crea el usuario de prueba si no existe.
#   2. Crea un archivo aleatorio de N MiB y lo sube con dfs put.
#   3. dfs stat --blocks: cada bloque con copias vivas en al menos 2 zonas (G1), con 3 copias, y
#      los bloques repartidos en los 4 DataNodes (DA-05).
#   4. dfs get y comparación del SHA-256 con el original.
#   5. dfs status: hay líder y ningún bloque con menos de 3 copias.
#   6. Limpieza: dfs rm del archivo y borrado de los archivos temporales.
#
# Resultado: FALLA solo si se rompe una garantía o algo indispensable: un bloque con menos de 2
# copias vivas o en una sola zona (G1), el SHA-256 distinto, ningún líder, o un comando dfs que
# falla. Un bloque con 2 copias en vez de 3 es AVISO: con DFS_W=2 el diseño lo permite (DA-04) y el
# líder repone la tercera después de su espera (DA-12); el script la espera --wait-copies segundos
# y dice si llegó. Sale con 0 si no hubo ninguna FALLA (los AVISO no cambian el código de salida),
# con 1 si hubo alguna y con 2 si las opciones están mal.
#
# El script corre en la máquina y usa el contenedor client con docker compose exec. El archivo
# aleatorio y el descargado quedan dentro de ese contenedor, en /tmp/dfs-smoke-*, nunca en el
# repositorio. Cada dfs corre ahí con su propio HOME, así que no toca la sesión de quien esté usando
# el contenedor. Las contraseñas entran por la entrada estándar. La del usuario de prueba se crea la
# primera vez en secrets/<usuario>_password (git lo ignora); si el usuario ya existe y ese archivo no
# está o no sirve, el script lo dice: usa otro --user, porque las contraseñas no se reinician.
set -uo pipefail
export MSYS_NO_PATHCONV=1 # en Git Bash, que no convierta las rutas /tmp/... del contenedor
cd "$(dirname "$0")/../.." || exit 2

# --- Opciones ---

env_name=local mib=512 user=smoke wait_copies=30 csv="" rep=1

usage_error() {
  echo "error: $*. El uso está en el comentario del inicio de scripts/pruebas/smoke.sh" >&2
  exit 2
}

while (($#)); do
  [[ $1 =~ ^--(env|mib|user|wait-copies|csv|rep)$ ]] || usage_error "opción desconocida: $1"
  (($# >= 2)) || usage_error "falta el valor de $1"
  case $1 in
    --env) env_name=$2 ;;
    --mib) mib=$2 ;;
    --user) user=$2 ;;
    --wait-copies) wait_copies=$2 ;;
    --csv) csv=$2 ;;
    --rep) rep=$2 ;;
  esac
  shift 2
done

case $env_name in
  local)
    compose=(docker compose -f compose.local.yml)
    secrets=secrets/local
    ;;
  aws)
    compose=(docker compose) # ~/dfsha/.env dice el archivo y el perfil
    secrets=secrets/aws
    ;;
  *) usage_error "--env debe ser local o aws" ;;
esac
[[ $mib =~ ^[1-9][0-9]*$ ]] || usage_error "--mib debe ser un entero positivo"
[[ $user =~ ^[a-z0-9_-]{1,32}$ && $user != admin ]] || usage_error "--user: de 1 a 32 caracteres a-z, 0-9, _ o -, y no admin"
[[ $wait_copies =~ ^[0-9]+$ ]] || usage_error "--wait-copies debe ser un entero"
[[ $rep =~ ^[1-9][0-9]*$ ]] || usage_error "--rep debe ser un entero positivo"

# --- Resultados ---

results=() failures=0 warnings=0
pass() { results+=("PASA   $*") && echo "PASA   $*"; }
warn() { results+=("AVISO  $*") && echo "AVISO  $*" && warnings=$((warnings + 1)); }
fail() { results+=("FALLA  $*") && echo "FALLA  $*" && failures=$((failures + 1)); }
info() { echo "==> $*"; }

# La última línea de error de un comando dfs ("error E_CODIGO: mensaje"), o su última línea.
error_of() {
  grep '^error' <<<"$1" | tail -n 1 | grep . || tail -n 1 <<<"$1"
}

# Segundos y MiB/s de la línea que imprimen dfs put y dfs get ("..., 12.34 s, 41.5 MiB/s ...").
timing_of() {
  grep 'MiB/s' <<<"$1" | tail -n 1 | sed -E 's|.*, ([0-9.]+) s, ([0-9.]+) MiB/s.*|\1 \2|'
}

# --- El contenedor cliente ---

run_dir=/tmp/dfs-smoke-$(date +%s)-$$ # dentro del contenedor client
remote=/smoke-$(date +%Y%m%d-%H%M%S)-$$.bin
started_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)
commit=$(git rev-parse --short HEAD 2>/dev/null || echo desconocido)

client() { "${compose[@]}" exec -T client "$@" </dev/null; }
# dfs con una sesión propia: dfs_as admin|user comando...
dfs_as() {
  local who=$1
  shift
  "${compose[@]}" exec -T -e "HOME=$run_dir/$who" client dfs "$@" </dev/null
}
# Igual, pero con la entrada estándar del que llama (para las contraseñas).
dfs_input() {
  local who=$1
  shift
  "${compose[@]}" exec -T -e "HOME=$run_dir/$who" client dfs "$@"
}

# --- Limpieza y resumen ---

uploaded=false cleaned=false put_s="" put_rate="" get_s="" get_rate="" sha_ok=false w=""

cleanup() {
  $cleaned && return 0
  cleaned=true
  if $uploaded; then
    if dfs_as user rm "$remote" >/dev/null 2>&1; then pass "limpieza: borrado $remote"; else warn "limpieza: no se pudo borrar $remote"; fi
  fi
  client rm -rf "$run_dir" >/dev/null 2>&1
}

write_csv() {
  [[ -n $csv ]] || return 0
  mkdir -p "$(dirname "$csv")"
  if [[ ! -s $csv ]]; then
    echo "fecha,entorno,commit,operacion,tamano_mib,clientes,w,repeticion,segundos,mib_s,sha_ok" >"$csv"
  fi
  if [[ -n $put_s ]]; then echo "$started_at,$env_name,$commit,put,$mib,1,$w,$rep,$put_s,$put_rate,$sha_ok" >>"$csv"; fi
  if [[ -n $get_s ]]; then echo "$started_at,$env_name,$commit,get,$mib,1,$w,$rep,$get_s,$get_rate,$sha_ok" >>"$csv"; fi
}

finish() {
  cleanup
  write_csv
  echo
  echo "== Resumen: $env_name, $mib MiB, usuario $user, commit $commit, DFS_W=${w:-?} =="
  printf '%s\n' "${results[@]}"
  if ((failures)); then
    echo "RESULTADO: FALLA ($failures fallas, $warnings avisos)"
    exit 1
  fi
  echo "RESULTADO: PASA ($warnings avisos)"
  exit 0
}

trap 'echo; fail "interrumpido"; finish' INT TERM

# --- 0. Requisitos ---

command -v docker >/dev/null || { fail "no encuentro docker" && finish; }
if ! docker version --format '{{.Server.Version}}' >/dev/null 2>&1; then
  fail "Docker no responde. ¿Está abierto Docker Desktop? En WSL, además, actívalo para la distro: Docker Desktop, Settings, Resources, WSL integration"
  finish
fi
if ! "${compose[@]}" ps --services --status running 2>/dev/null | grep -qx client; then
  fail "el contenedor client no está corriendo (local: docker compose -f compose.local.yml up -d; aws: corre el script en la VM5, dentro de ~/dfsha)"
  finish
fi
[[ -r $secrets/admin_password ]] || { fail "no encuentro $secrets/admin_password"; finish; }
w=$(client python -c 'from dfs_common.config import Settings; print(Settings().w)' 2>/dev/null)

# --- 1. Admin y usuario de prueba ---

info "entrando como admin y preparando el usuario $user"
if ! out=$(dfs_input admin login --user admin <"$secrets/admin_password" 2>&1); then
  fail "admin no pudo entrar: $(error_of "$out")"
  finish
fi
password_file=secrets/${user}_password
if [[ -f $password_file ]]; then
  password=$(<"$password_file") new_password=false
else
  password=$(head -c 18 /dev/urandom | base64 | tr -d '/+=\n') new_password=true
fi
if out=$(printf '%s\n%s\n' "$password" "$password" | dfs_input admin useradd "$user" 2>&1); then
  if $new_password; then (umask 077 && printf '%s' "$password" >"$password_file"); fi
  info "creado el usuario $user"
elif grep -q E_EXISTS <<<"$out"; then
  if $new_password; then
    fail "el usuario $user ya existe, pero no está $password_file con su contraseña. Usa --user con otro nombre (las contraseñas no se reinician)"
    finish
  fi
else
  fail "no se pudo crear el usuario $user: $(error_of "$out")"
  finish
fi
if ! out=$(printf '%s\n' "$password" | dfs_input user login --user "$user" 2>&1); then
  if grep -q E_AUTH <<<"$out"; then
    fail "la contraseña de $password_file no sirve para el usuario $user (¿es de otro clúster?). Usa --user con otro nombre"
  else
    fail "el usuario $user no pudo entrar: $(error_of "$out")"
  fi
  finish
fi
pass "admin entró y el usuario $user está listo"

# --- 2. put ---

info "creando $mib MiB aleatorios en el contenedor client y subiéndolos a $remote"
if ! client sh -c "mkdir -p $run_dir && head -c ${mib}M /dev/urandom > $run_dir/original.bin"; then
  fail "no se pudo crear el archivo aleatorio en el contenedor client"
  finish
fi
original_sha=$(client sha256sum "$run_dir/original.bin" | cut -d ' ' -f 1)
if ! out=$(dfs_as user put "$run_dir/original.bin" "$remote" 2>&1); then
  fail "dfs put: $(error_of "$out")"
  finish
fi
uploaded=true
read -r put_s put_rate <<<"$(timing_of "$out")"
pass "put: $(grep 'MiB/s' <<<"$out" | tail -n 1)"

# --- 3. Dónde quedó cada bloque ---

# Zona de cada DataNode vivo, de dfs status: "dn1=us-east-1a,dn2=...".
zones_from_status() {
  awk '$2 == "vivo" { printf "%s=%s,", $1, $3 }' <<<"$1"
}

# Una línea por bloque de dfs stat --blocks: "índice copias_vivas zonas nodos_vivos". En stat, un
# nodo caído va seguido de "(caído)", y su copia no cuenta.
block_report() {
  awk -v zones="$1" '
    BEGIN {
      n = split(zones, pairs, ",")
      for (i = 1; i <= n; i++) { split(pairs[i], kv, "="); zone[kv[1]] = kv[2] }
    }
    NR > 2 && $1 ~ /^[0-9]+$/ {
      k = 0
      for (i = 4; i <= NF; i++) {
        t = $i
        gsub(",", "", t)
        if (t == "(caído)") { dead[k] = 1; continue }
        if (t == "-") continue
        k++; node[k] = t; dead[k] = 0
      }
      alive = 0; zl = " "; nl = ""
      for (j = 1; j <= k; j++) {
        if (dead[j]) continue
        alive++
        nl = nl (nl == "" ? "" : ",") node[j]
        z = zone[node[j]]
        if (z != "" && index(zl, " " z " ") == 0) zl = zl z " "
      }
      print $1, alive, split(zl, tmp, " "), (nl == "" ? "-" : nl)
    }'
}

# Llena total, broken (índices sin 2 copias vivas en 2 zonas), short (bloques con 2 copias) y used
# (DataNodes con alguna copia viva), a partir de la salida de stat --blocks.
evaluate_blocks() {
  local index alive zone_count nodes report
  total=0 broken="" short=0 used=""
  report=$(block_report "$zones" <<<"$1")
  while read -r index alive zone_count nodes; do
    [[ -n $index ]] || continue
    total=$((total + 1))
    if ((alive < 2 || zone_count < 2)); then
      broken+=" $index"
    elif ((alive < 3)); then
      short=$((short + 1))
    fi
    used+=",$nodes"
  done <<<"$report"
  used=$(tr ',' '\n' <<<"$used" | grep -v -e '^$' -e '^-$' | sort -u | paste -sd ' ' -)
}

# Lee dfs status (admin): status_out, zones y alive_dns (cuántos DataNodes vivos).
read_status() {
  status_out=$(dfs_as admin status 2>&1) || return 1
  zones=$(zones_from_status "$status_out")
  alive_dns=$(awk '$2 == "vivo"' <<<"$status_out" | wc -l)
}

# Lee dfs stat --blocks del archivo y lo evalúa con las zonas de read_status.
read_blocks() {
  blocks_out=$(dfs_as user stat --blocks "$remote" 2>&1) || return 1
  evaluate_blocks "$blocks_out"
}

info "revisando dónde quedó cada bloque (dfs stat --blocks y dfs status)"
read_status || { fail "dfs status: $(error_of "$status_out")" && finish; }
read_blocks || { fail "dfs stat --blocks: $(error_of "$blocks_out")" && finish; }
if ((total == 0)); then
  fail "dfs stat --blocks no mostró bloques"
elif [[ -n $broken ]]; then
  fail "G1: bloques con menos de 2 copias vivas o en una sola zona (índices$broken)"
else
  pass "G1: los $total bloques tienen copias vivas en al menos 2 zonas"
fi
used_count=$(wc -w <<<"$used")
if ((total == 1)); then
  warn "el archivo tiene un solo bloque: con eso no se ve el reparto en los 4 DataNodes"
elif ((used_count == 4 && alive_dns == 4)); then
  pass "DA-05: bloques repartidos en los 4 DataNodes ($used)"
elif ((total > 1)); then
  warn "DA-05: bloques repartidos en $used_count DataNodes ($used), con $alive_dns vivos; se esperaban los 4"
fi
if ((short == 0 && total > 0)); then
  pass "los $total bloques tienen 3 copias vivas"
elif ((short > 0)); then
  # Se vuelve a leer también dfs status: un DataNode que vuelve trae su zona.
  first_short=$short waited=0
  while ((short > 0 && waited < wait_copies)); do
    sleep 5
    waited=$((waited + 5))
    read_status && read_blocks
  done
  if ((short == 0)); then
    warn "$first_short bloques quedaron con 2 copias; la tercera llegó a los ${waited} s"
  else
    warn "$short bloques siguen con 2 copias después de ${waited} s; el líder repone la tercera pasados DFS_LEADER_GRACE_S y DFS_REREPLICATE_AFTER_S"
  fi
fi

# --- 4. get y SHA-256 ---

info "bajando $remote y comparando el SHA-256"
if out=$(dfs_as user get "$remote" "$run_dir/copy.bin" 2>&1); then
  read -r get_s get_rate <<<"$(timing_of "$out")"
  copy_sha=$(client sha256sum "$run_dir/copy.bin" | cut -d ' ' -f 1)
  if [[ -n $copy_sha && $copy_sha == "$original_sha" ]]; then
    sha_ok=true
    pass "get: ${get_s} s, ${get_rate} MiB/s; SHA-256 igual al original (${original_sha:0:16}...)"
  else
    fail "SHA-256 distinto: original ${original_sha:0:16}..., descargado ${copy_sha:0:16}..."
  fi
else
  fail "dfs get: $(error_of "$out")"
fi

# --- 5. Estado del sistema ---

info "revisando dfs status"
if status_out=$(dfs_as admin status 2>&1); then
  leader=$(sed -n 's/^Líder: //p' <<<"$status_out")
  case $leader in
    meta*) pass "hay líder: $leader" ;;
    *) fail "no hay líder (dfs status: ${leader:-sin respuesta})" ;;
  esac
  under=$(sed -n 's/^Bloques con menos de 3 copias vivas: //p' <<<"$status_out")
  if [[ $under == 0 ]]; then
    pass "dfs status: ningún bloque con menos de 3 copias vivas"
  else
    warn "dfs status: ${under:-?} bloques del sistema con menos de 3 copias vivas"
  fi
else
  fail "dfs status: $(error_of "$status_out")"
fi

# --- 6. Limpieza y resumen ---

finish
