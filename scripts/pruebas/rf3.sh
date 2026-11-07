#!/usr/bin/env bash
# Prueba de acceso RF3 con dos clientes (tarea 25; Tabla 21, G2, G3, DA-14).
#
# Uso, desde la raíz del repositorio (en Windows, desde WSL; en AWS, en la VM5 dentro de ~/dfsha):
#   scripts/pruebas/rf3.sh                 sistema local (compose.local.yml)
#   scripts/pruebas/rf3.sh --env aws       en la VM5
#
# Opciones:
#   --env local|aws   como en smoke.sh. Por defecto, local.
#   --mib N           tamaño del archivo, en MiB; al menos 2 bloques. Por defecto 20 (3 bloques de 8).
#   --user NOMBRE     usuario de prueba; se crea si no existe. Por defecto rf3.
#
# Pasos (dos procesos dfs shell, A y B, cada uno con su sesión, en el contenedor client):
#   1. Archivo aleatorio de N MiB con el texto VERSION-1 a mitad del bloque 1, y la versión 2
#      esperada: la misma, con VERSION-2 en ese offset. dfs put de la versión 1.
#   2. A: open rw, write en ese offset (dentro del bloque 1) y read de lo escrito. A no cierra.
#   3. B, con A abierto: open r, read del offset y del archivo entero. Debe ver la versión 1
#      completa: el texto VERSION-1 y el SHA-256 de la versión 1 (G2).
#   4. A: close, que publica la versión 2.
#   5. B: open r nuevo. Debe ver la versión 2 completa: VERSION-2 y el SHA-256 esperado (G3).
#   6. Modos a y w: A anexa 5 bytes (el tamaño crece en 5) y después reemplaza todo por un texto
#      corto; B los lee después de cada close.
#   7. Limpieza: dfs rm y borrado de los archivos temporales.
#
# A recibe sus comandos por una FIFO, así que su close ocurre cuando B ya terminó de leer, sin
# depender de un sleep. Sale con 0 si todo pasó, con 1 si algo falló y con 2 si las opciones están
# mal. Los archivos quedan dentro del contenedor client, en /tmp/dfs-rf3-*, y se borran al final.
set -uo pipefail
export MSYS_NO_PATHCONV=1
cd "$(dirname "$0")/../.." || exit 2
source scripts/pruebas/lib.sh

env_name=local mib=20 user=rf3

usage_error() {
  echo "error: $*. El uso está en el comentario del inicio de scripts/pruebas/rf3.sh" >&2
  exit 2
}

while (($#)); do
  [[ $1 =~ ^--(env|mib|user)$ ]] || usage_error "opción desconocida: $1"
  (($# >= 2)) || usage_error "falta el valor de $1"
  case $1 in
    --env) env_name=$2 ;;
    --mib) mib=$2 ;;
    --user) user=$2 ;;
  esac
  shift 2
done
select_env "$env_name" || usage_error "--env debe ser local o aws"
[[ $mib =~ ^[1-9][0-9]*$ ]] || usage_error "--mib debe ser un entero positivo"
[[ $user =~ ^[a-z0-9_-]{1,32}$ && $user != admin ]] || usage_error "--user: de 1 a 32 caracteres a-z, 0-9, _ o -, y no admin"

failures=0
pass() { echo "PASA   $*"; }
fail() { echo "FALLA  $*" && failures=$((failures + 1)); }

stamp=$(date +%Y%m%d-%H%M%S)-$$
run_dir=/tmp/dfs-rf3-$stamp
host_dir=$(mktemp -d)
remote=/rf3-$stamp.bin
a_pid="" uploaded=false

cleanup() {
  [[ -n $a_pid ]] && kill "$a_pid" 2>/dev/null
  $uploaded && dfs_as a rm "$remote" >/dev/null 2>&1
  client rm -rf "$run_dir" >/dev/null 2>&1
  rm -rf "$host_dir"
}
finish() {
  cleanup
  echo
  if ((failures)); then echo "RESULTADO: FALLA ($failures fallas)" && exit 1; fi
  echo "RESULTADO: PASA"
  exit 0
}
trap 'echo; fail "interrumpido"; finish' INT TERM

sha_in() { client sha256sum "$1" | cut -d ' ' -f 1; }
prefix() { sed "s/^/$1: /"; } # antepone A: o B: a cada línea de la salida de un shell
# Corre en B un dfs shell con los comandos dados (uno por argumento) y muestra su salida con "B: ".
b_shell() {
  b_out=$(printf '%s\n' "$@" | dfs_input b shell 2>&1)
  prefix B <<<"$b_out"
}
# Le manda una línea a A por su FIFO y espera a que su salida tenga el texto dado.
a_send() {
  local line=$1 expect=$2 waited=0
  echo "$line" >&"$a_fd"
  until grep -qF -- "$expect" "$host_dir/a.out" 2>/dev/null; do
    sleep 0.5
    waited=$((waited + 1))
    ((waited < 120)) || return 1
  done
}

# --- 0. Requisitos, sesiones y archivos ---

require_client
login_sessions "$user" a b
block_mib=$(client python -c 'from dfs_common.config import Settings; print(Settings().block_mib)')
((mib >= 2 * block_mib)) || usage_error "--mib debe dar al menos 2 bloques de $block_mib MiB"
offset=$((block_mib * 1024 * 1024 + block_mib * 1024 * 512)) # a mitad del bloque 1
size=$((mib * 1024 * 1024))
info "commit $(git rev-parse --short HEAD 2>/dev/null || echo desconocido), DFS_W=$w, bloques de $block_mib MiB"
info "archivo de $mib MiB; A escribe en el offset $offset, dentro del bloque 1"
client sh -c "head -c ${mib}M /dev/urandom > $run_dir/v1.bin \
  && printf VERSION-1 | dd of=$run_dir/v1.bin bs=1 seek=$offset conv=notrunc status=none \
  && cp $run_dir/v1.bin $run_dir/v2.bin \
  && printf VERSION-2 | dd of=$run_dir/v2.bin bs=1 seek=$offset conv=notrunc status=none" ||
  die "no pude crear los archivos en el contenedor client"
sha1=$(sha_in "$run_dir/v1.bin") sha2=$(sha_in "$run_dir/v2.bin")
out=$(dfs_as a put "$run_dir/v1.bin" "$remote" 2>&1) || die "dfs put: $(error_of "$out")"
uploaded=true
pass "put de la versión 1: $(grep 'MiB/s' <<<"$out")"

# --- 1. A abre en rw y escribe, sin cerrar ---

mkfifo "$host_dir/a.in"
dfs_input a shell <"$host_dir/a.in" >"$host_dir/a.out" 2>&1 &
a_pid=$!
exec {a_fd}>"$host_dir/a.in"
echo
echo "### A abre en rw y escribe; no cierra"
a_send "open $remote rw" "modo rw" || { fail "A no pudo abrir en rw: $(tail -n 1 "$host_dir/a.out")" && finish; }
a_send "write 3 $offset VERSION-2" "bytes escritos" || { fail "A no pudo escribir" && finish; }
a_send "read 3 $offset 9" "VERSION-2" || { fail "A no ve su propio cambio" && finish; }
prefix A <"$host_dir/a.out"
echo

# --- 2. B lee con A abierto: la versión 1 completa ---

echo
echo "### B lee mientras A no ha cerrado"
b_shell "open $remote r" "read 3 $offset 9" "read 3 0 $size --to $run_dir/b-antes.bin" "close 3"
b_sha=$(sha_in "$run_dir/b-antes.bin")
if grep -q "versión 1" <<<"$b_out" && grep -q VERSION-1 <<<"$b_out" && [[ $b_sha == "$sha1" ]]; then
  pass "G2: con A abierto, B ve la versión 1 completa (VERSION-1, SHA-256 ${sha1:0:16}... de la versión 1)"
else
  fail "G2: con A abierto, B no vio la versión 1 completa (SHA-256 ${b_sha:0:16}..., esperado ${sha1:0:16}...)"
fi

# --- 3. A cierra y publica la versión 2 ---

echo
echo "### A cierra"
before=$(wc -l <"$host_dir/a.out")
a_send "close 3" "cerrado" || fail "A no pudo cerrar: $(tail -n 1 "$host_dir/a.out")"
echo quit >&"$a_fd"
exec {a_fd}>&-
wait "$a_pid"
a_pid=""
a_closed=$(tail -n +"$((before + 1))" "$host_dir/a.out")
prefix A <<<"$a_closed"
echo
if grep -q "cerrado: .* versión 2" <<<"$a_closed"; then
  pass "A publicó la versión 2"
else
  fail "el close de A no publicó la versión 2"
fi

# --- 4. B abre de nuevo: la versión 2 completa ---

echo
echo "### B lee después del close de A"
b_shell "open $remote r" "read 3 $offset 9" "read 3 0 $size --to $run_dir/b-despues.bin" "close 3"
b_sha=$(sha_in "$run_dir/b-despues.bin")
if grep -q "versión 2" <<<"$b_out" && grep -q VERSION-2 <<<"$b_out" && [[ $b_sha == "$sha2" ]]; then
  pass "G3: después del close, B ve la versión 2 completa (VERSION-2, SHA-256 ${sha2:0:16}... esperado)"
else
  fail "G3: después del close, B no vio la versión 2 (SHA-256 ${b_sha:0:16}..., esperado ${sha2:0:16}...)"
fi

# --- 5. Modos a y w ---

echo
echo "### A anexa (modo a) y B lee el final"
out=$(printf '%s\n' "open $remote a" "write 3 0 +COLA" "close 3" | dfs_input a shell 2>&1)
prefix A <<<"$out"
b_shell "open $remote r" "read 3 $size 100" "close 3"
if grep -q "versión 3, $((size + 5)) bytes" <<<"$b_out" && grep -q "+COLA" <<<"$b_out"; then
  pass "modo a: versión 3, $((size + 5)) bytes, con +COLA al final"
else
  fail "modo a: B no vio la versión 3 con +COLA al final"
fi

echo
echo "### A reemplaza (modo w) y B lee"
out=$(printf '%s\n' "open $remote w" "write 3 0 contenido nuevo" "close 3" | dfs_input a shell 2>&1)
prefix A <<<"$out"
b_shell "open $remote r" "read 3 0 100" "close 3"
if grep -q "versión 4, 15 bytes" <<<"$b_out" && grep -q "contenido nuevo" <<<"$b_out"; then
  pass "modo w: versión 4, 15 bytes, solo el contenido nuevo"
else
  fail "modo w: B no vio la versión 4 con el contenido nuevo"
fi

finish
