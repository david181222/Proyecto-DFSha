#!/usr/bin/env bash
# Prueba de rendimiento de DFSha: latencia y throughput de put y get (tarea 25; Tabla 21, DA-03, DA-04).
#
# Uso, desde la raíz del repositorio (en Windows, desde WSL; en AWS, en la VM5 dentro de ~/dfsha):
#   scripts/pruebas/perf.sh --sizes "1 16" --reps 2                  ensayo local, pequeño
#   scripts/pruebas/perf.sh --env aws --csv docs/pruebas/datos/25-rendimiento-aws.csv
#   scripts/pruebas/perf.sh --env aws --cold --sizes 512 --csv ...   get con la caché vacía (control)
#
# Opciones:
#   --env local|aws      como en smoke.sh: local usa compose.local.yml y secrets/local/; aws, el .env
#                        que escribió infra/deploy.sh y secrets/aws/. Por defecto, local.
#   --sizes "A B ..."    tamaños de archivo en MiB. Por defecto "1 64 512".
#   --clients "A B ..."  clientes a la vez. Por defecto "1 4".
#   --reps N             repeticiones medidas por condición. Por defecto 5.
#   --warmup N           repeticiones de calentamiento por condición, que van al CSV con
#                        repeticion=0 y no entran en el resumen. Por defecto 1.
#   --user NOMBRE        usuario de prueba; se crea si no existe. Por defecto perf.
#   --min-free-gib N     si algún DataNode vivo tiene menos de N GiB libres, espera a que la limpieza
#                        los libere antes de seguir. Por defecto 4.
#   --csv ARCHIVO        agrega una fila por cliente y operación, y una fila "todos" por repetición
#                        cuando hay más de un cliente. Sin --csv, el CSV queda en el directorio temporal
#                        y solo se imprime el resumen.
#   --cold               control "en frío", con 1 cliente: sube todas las repeticiones de cada tamaño,
#                        se detiene para que vacíes la caché de página de los DataNodes (lo dice en
#                        pantalla) y después las baja. Necesita una terminal: corre el script dentro
#                        de una sesión ./infra/ssh.sh vm5, no como comando suelto.
#
# Cómo mide (decisiones de la tarea 25, explicadas en docs/pruebas/25-rendimiento.md):
#   - Cada repetición y cada cliente usan un archivo aleatorio nuevo: los bloques se nombran por su
#     hash (DA-02), y un archivo repetido no transferiría nada.
#   - Orden intercalado: la repetición 1 de todas las condiciones, después la 2, y así. Si se agotan
#     los créditos de red o de CPU de las t3, el efecto se reparte entre las condiciones en vez de caer
#     en la última; la hora de cada fila queda en el CSV.
#   - "segundos" de cada cliente es el tiempo que imprime el propio dfs (sin el arranque de Python ni
#     de docker exec, que suman cerca de 1 s). La fila "todos" usa el del cliente más lento, y su MiB/s
#     es el total de MiB entre ese tiempo: supone que los N arrancan a la vez (se lanzan juntos; el
#     desfase es de décimas de segundo, que solo pesa con archivos de 1 MiB). En pantalla se muestra
#     además el tiempo de pared, con los arranques incluidos.
#   - Sin --cold, el get corre justo después del put: los bloques acaban de escribirse y pueden estar
#     en la caché de página de los DataNodes ("caliente", columna cache).
#   - Al terminar cada repetición se borran los archivos remotos. La limpieza (DA-02) libera los
#     bloques después de DFS_GC_MIN_AGE_S; para no llenar los discos, despliega con
#     scripts/pruebas/pruebas.env (README, "Despliegue en AWS").
#
# Sale con 0 si todo pasó, con 1 si algún put, get o SHA-256 falló, y con 2 si las opciones están
# mal. Los archivos aleatorios quedan dentro del contenedor client, en /tmp/dfs-perf-*, y se borran.
set -uo pipefail
export MSYS_NO_PATHCONV=1 # en Git Bash, que no convierta las rutas /tmp/... del contenedor
cd "$(dirname "$0")/../.." || exit 2

# --- Opciones ---

env_name=local sizes="1 64 512" clients_list="1 4" reps=5 warmup=1 user=perf min_free=4 csv="" cold=false

usage_error() {
  echo "error: $*. El uso está en el comentario del inicio de scripts/pruebas/perf.sh" >&2
  exit 2
}

while (($#)); do
  if [[ $1 == --cold ]]; then
    cold=true
    shift
    continue
  fi
  [[ $1 =~ ^--(env|sizes|clients|reps|warmup|user|min-free-gib|csv)$ ]] || usage_error "opción desconocida: $1"
  (($# >= 2)) || usage_error "falta el valor de $1"
  case $1 in
    --env) env_name=$2 ;;
    --sizes) sizes=$2 ;;
    --clients) clients_list=$2 ;;
    --reps) reps=$2 ;;
    --warmup) warmup=$2 ;;
    --user) user=$2 ;;
    --min-free-gib) min_free=$2 ;;
    --csv) csv=$2 ;;
  esac
  shift 2
done

source scripts/pruebas/lib.sh
select_env "$env_name" || usage_error "--env debe ser local o aws"
for value in $sizes; do [[ $value =~ ^[1-9][0-9]*$ ]] || usage_error "--sizes: $value no es un entero positivo"; done
for value in $clients_list; do [[ $value =~ ^[1-9][0-9]*$ ]] || usage_error "--clients: $value no es un entero positivo"; done
[[ -n $sizes && -n $clients_list ]] || usage_error "--sizes y --clients no pueden ir vacíos"
[[ $reps =~ ^[1-9][0-9]*$ ]] || usage_error "--reps debe ser un entero positivo"
[[ $warmup =~ ^[0-9]+$ ]] || usage_error "--warmup debe ser un entero"
[[ $min_free =~ ^[0-9]+$ ]] || usage_error "--min-free-gib debe ser un entero"
[[ $user =~ ^[a-z0-9_-]{1,32}$ && $user != admin ]] || usage_error "--user: de 1 a 32 caracteres a-z, 0-9, _ o -, y no admin"
if $cold; then
  clients_list=1
  [[ -r /dev/tty ]] || usage_error "--cold necesita una terminal (entra con ./infra/ssh.sh vm5 y córrelo ahí)"
fi
max_clients=$(tr ' ' '\n' <<<"$clients_list" | sort -n | tail -n 1)

# --- Ayudas ---

failures=0
fail() { echo "FALLA  $*" && failures=$((failures + 1)); }
now() { date +%s.%N; }

stamp=$(date +%Y%m%d-%H%M%S)-$$
run_dir=/tmp/dfs-perf-$stamp # dentro del contenedor client
host_dir=$(mktemp -d)        # salidas de los clientes en paralelo, en la máquina
commit=$(git rev-parse --short HEAD 2>/dev/null || echo desconocido)
[[ -n $csv ]] || csv=$host_dir/perf.csv

cleanup() {
  client rm -rf "$run_dir" >/dev/null 2>&1
  rm -rf "$host_dir"
}
trap 'echo; fail "interrumpido"; cleanup; exit 1' INT TERM

csv_row() { # fecha operacion mib clientes cliente rep segundos mib_s sha_ok cache
  echo "$1,$env_name,$commit,$2,$3,$4,$5,$w,$6,$7,$8,$9,${10}" >>"$csv"
}

# --- 0. Requisitos y sesiones ---

sessions=()
for ((k = 1; k <= max_clients; k++)); do sessions+=("c$k"); done
# Una sesión por cliente (c1, c2, ...): cada proceso dfs tiene su HOME y su session.json.
require_client
login_sessions "$user" "${sessions[@]}"
mkdir -p "$(dirname "$csv")"
if [[ ! -s $csv ]]; then
  echo "fecha,entorno,commit,operacion,tamano_mib,clientes,cliente,w,repeticion,segundos,mib_s,sha_ok,cache" >"$csv"
fi
if $cold; then extra="en frío, sin calentamiento"; else extra="+$warmup de calentamiento"; fi
info "entorno $env_name, commit $commit, DFS_W=$w, tamaños: $sizes MiB, clientes: $clients_list, $reps repeticiones ($extra)"

# --- Espacio libre en los DataNodes (de dfs status) ---

min_free_gib() {
  dfs_as admin status 2>/dev/null | awk '$2 == "vivo" && $(NF) == "libres" { print $(NF - 2) }' | sort -n | head -n 1
}

wait_for_space() {
  local free waited=0
  free=$(min_free_gib)
  [[ -n $free ]] || { echo "AVISO  no pude leer el espacio libre de dfs status" && return 0; }
  while awk -v f="$free" -v m="$min_free" 'BEGIN { exit !(f < m) }'; do
    ((waited >= 900)) && { echo "AVISO  después de ${waited} s, el DataNode más lleno sigue con $free GiB libres" && return 0; }
    info "el DataNode más lleno tiene $free GiB libres (< $min_free): espero a la limpieza (DA-02)"
    sleep 30
    waited=$((waited + 30))
    free=$(min_free_gib)
  done
}

# --- Una operación con N clientes a la vez ---

# run_op put|get mib n rep cache: corre la operación en los N clientes en paralelo, escribe sus filas
# en el CSV y deja ok=false si alguno falló.
run_op() {
  local op=$1 mib=$2 n=$3 rep=$4 cache=$5 k start end wall slowest=0 date_utc seconds rate sha_ok local_sha out
  local -a pids=()
  date_utc=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  start=$(now)
  for ((k = 1; k <= n; k++)); do
    if [[ $op == put ]]; then
      dfs_as "c$k" put "$run_dir/c$k.bin" "$(remote_of "$mib" "$n" "$rep" "$k")" >"$host_dir/c$k.out" 2>&1 &
    else
      dfs_as "c$k" get "$(remote_of "$mib" "$n" "$rep" "$k")" "$run_dir/c$k.copy" >"$host_dir/c$k.out" 2>&1 &
    fi
    pids+=($!)
  done
  for ((k = 1; k <= n; k++)); do
    wait "${pids[k - 1]}" || echo failed >>"$host_dir/c$k.out"
  done
  end=$(now)
  for ((k = 1; k <= n; k++)); do
    out=$(<"$host_dir/c$k.out")
    sha_ok=""
    if grep -qx failed <<<"$out" || ! grep -q 'MiB/s' <<<"$out"; then
      fail "$op $mib MiB, cliente $k de $n, repetición $rep: $(error_of "$out")"
      ok=false
      csv_row "$date_utc" "$op" "$mib" "$n" "$k" "$rep" "" "" false "$cache"
      continue
    fi
    read -r seconds rate <<<"$(timing_of "$out")"
    slowest=$(awk -v a="$slowest" -v b="$seconds" 'BEGIN { print (b > a ? b : a) }')
    if [[ $op == get ]]; then
      local_sha=$(client sha256sum "$run_dir/c$k.copy" | cut -d ' ' -f 1)
      if [[ -n $local_sha && $local_sha == "${original_sha[k]}" ]]; then
        sha_ok=true
      else
        sha_ok=false ok=false
        fail "SHA-256 distinto: $mib MiB, cliente $k de $n, repetición $rep"
      fi
    fi
    csv_row "$date_utc" "$op" "$mib" "$n" "$k" "$rep" "$seconds" "$rate" "$sha_ok" "$cache"
  done
  if ((n > 1)); then
    wall=$(awk -v s="$start" -v e="$end" 'BEGIN { printf "%.2f", e - s }')
    if $ok; then
      rate=$(awk -v t="$((mib * n))" -v s="$slowest" 'BEGIN { printf "%.1f", t / s }')
      csv_row "$date_utc" "$op" "$mib" "$n" todos "$rep" "$slowest" "$rate" "" "$cache"
      echo "       $op: $n clientes a la vez, $((mib * n)) MiB; el más lento tardó $slowest s: $rate MiB/s en total (pared: $wall s)"
    fi
  else
    echo "       $op: $(grep 'MiB/s' "$host_dir/c1.out" | tail -n 1 | sed -E 's|.*, ([0-9.]+ s, [0-9.]+ MiB/s).*|\1|')"
  fi
}

remote_of() { echo "/perf-$stamp-${1}m-n$2-r$3-c$4.bin"; }

# Crea los N archivos aleatorios y guarda sus SHA-256 en original_sha[k].
make_files() {
  local mib=$1 n=$2 k
  original_sha=()
  for ((k = 1; k <= n; k++)); do
    client sh -c "head -c ${mib}M /dev/urandom > $run_dir/c$k.bin" || die "no pude crear el archivo aleatorio en el contenedor client"
    original_sha[k]=$(client sha256sum "$run_dir/c$k.bin" | cut -d ' ' -f 1)
  done
}

remove_files() { # mib n rep: borra los archivos remotos y los locales de esa repetición
  local k
  for ((k = 1; k <= $2; k++)); do
    dfs_as "c$k" rm "$(remote_of "$1" "$2" "$3" "$k")" >/dev/null 2>&1 || echo "AVISO  no pude borrar $(remote_of "$1" "$2" "$3" "$k")"
  done
  client sh -c "rm -f $run_dir/c*.bin $run_dir/c*.copy"
}

# --- Una repetición de una condición, en caliente: put, get, SHA-256 y borrado ---

run_rep() {
  local mib=$1 n=$2 rep=$3 label
  label=$([[ $rep == 0 ]] && echo "calentamiento" || echo "repetición $rep")
  info "$mib MiB, $n cliente(s), $label"
  wait_for_space
  make_files "$mib" "$n"
  ok=true
  run_op put "$mib" "$n" "$rep" caliente
  $ok && run_op get "$mib" "$n" "$rep" caliente
  remove_files "$mib" "$n" "$rep"
}

# --- Control en frío: todas las subidas, pausa para vaciar la caché, todas las bajadas ---

run_cold() {
  local mib=$1 rep
  declare -A cold_sha=()
  for ((rep = 1; rep <= reps; rep++)); do
    info "en frío: $mib MiB, subida $rep de $reps"
    wait_for_space
    make_files "$mib" 1
    cold_sha[$rep]=${original_sha[1]}
    ok=true
    run_op put "$mib" 1 "$rep" frio
    client rm -f "$run_dir/c1.bin"
  done
  echo
  echo "Ahora vacía la caché de página de los 4 DataNodes. Desde tu máquina, en la raíz del repositorio:"
  echo "  for vm in vm1 vm2 vm3 vm4; do ./infra/ssh.sh \$vm \"sudo sh -c 'sync; echo 3 > /proc/sys/vm/drop_caches'\"; done"
  echo "Cuando termine, pulsa Enter aquí."
  read -r </dev/tty
  for ((rep = 1; rep <= reps; rep++)); do
    info "en frío: $mib MiB, bajada $rep de $reps"
    original_sha=([1]="${cold_sha[$rep]}")
    ok=true
    run_op get "$mib" 1 "$rep" frio
    remove_files "$mib" 1 "$rep"
  done
}

# --- Corrida ---

if $cold; then
  for mib in $sizes; do run_cold "$mib"; done
else
  for ((rep = 1 - warmup; rep <= reps; rep++)); do
    ((rep < 1)) && r=0 || r=$rep
    for mib in $sizes; do
      for n in $clients_list; do
        run_rep "$mib" "$n" "$r"
      done
    done
  done
fi

echo
info "resumen ($csv)"
if command -v python3 >/dev/null; then
  python3 scripts/pruebas/perf_summary.py "$csv"
else
  echo "AVISO  no está python3: corre scripts/pruebas/perf_summary.py $csv en otra máquina"
fi
cleanup
if ((failures)); then
  echo "RESULTADO: FALLA ($failures fallas)"
  exit 1
fi
echo "RESULTADO: PASA"
