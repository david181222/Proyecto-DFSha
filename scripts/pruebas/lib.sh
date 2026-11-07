# shellcheck shell=bash disable=SC2034,SC2154  # run_dir lo pone quien carga; compose, secrets y w los usa él
# DFSha · funciones comunes de perf.sh y rf3.sh (tarea 25). Se carga con source, desde la raíz del
# repositorio. smoke.sh (tarea 22) tiene su propia copia de varias de estas funciones.
#
# Antes de usarlas: run_dir (carpeta dentro del contenedor client) y, con select_env, compose y
# secrets.

die() { echo "FALLA  $*" >&2 && exit 1; }
info() { echo "==> $*"; }

# La última línea de error de un comando dfs ("error E_CODIGO: mensaje"), o su última línea.
error_of() { grep '^error' <<<"$1" | tail -n 1 | grep . || tail -n 1 <<<"$1"; }

# "segundos MiB/s" de la línea que imprimen dfs put y dfs get ("..., 12.34 s, 41.5 MiB/s ...").
timing_of() { grep 'MiB/s' <<<"$1" | tail -n 1 | sed -E 's|.*, ([0-9.]+) s, ([0-9.]+) MiB/s.*|\1 \2|'; }

# select_env local|aws: deja compose (el comando) y secrets (la carpeta), como en smoke.sh.
select_env() {
  case $1 in
    local)
      compose=(docker compose -f compose.local.yml)
      secrets=secrets/local
      ;;
    aws)
      compose=(docker compose) # ~/dfsha/.env dice el archivo y el perfil
      secrets=secrets/aws
      ;;
    *) return 1 ;;
  esac
}

client() { "${compose[@]}" exec -T client "$@" </dev/null; }
# dfs con una sesión propia (su HOME, dentro de run_dir): dfs_as sesión comando...
dfs_as() {
  local who=$1
  shift
  "${compose[@]}" exec -T -e "HOME=$run_dir/$who" client dfs "$@" </dev/null
}
# Igual, pero con la entrada estándar del que llama (contraseñas, o comandos de dfs shell).
dfs_input() {
  local who=$1
  shift
  "${compose[@]}" exec -T -e "HOME=$run_dir/$who" client dfs "$@"
}

# Docker, el contenedor client corriendo y la contraseña de admin; deja w con DFS_W del cliente.
require_client() {
  command -v docker >/dev/null || die "no encuentro docker"
  docker version --format '{{.Server.Version}}' >/dev/null 2>&1 || die "Docker no responde (¿está abierto Docker Desktop?)"
  "${compose[@]}" ps --services --status running 2>/dev/null | grep -qx client ||
    die "el contenedor client no está corriendo (local: docker compose -f compose.local.yml up -d; aws: corre el script en la VM5, dentro de ~/dfsha)"
  [[ -r $secrets/admin_password ]] || die "no encuentro $secrets/admin_password"
  w=$(client python -c 'from dfs_common.config import Settings; print(Settings().w)' 2>/dev/null)
  [[ -n $w ]] || die "no pude leer DFS_W del contenedor client"
  client mkdir -p "$run_dir" || die "no pude crear $run_dir en el contenedor client"
}

# login_sessions usuario sesión...: entra como admin (sesión "admin"), crea el usuario si no existe
# y abre una sesión suya por cada nombre. La contraseña del usuario se crea la primera vez en
# secrets/<usuario>_password (git lo ignora), como en smoke.sh. Las contraseñas van por stdin.
login_sessions() {
  local user=$1 password password_file new_password out who
  shift
  out=$(dfs_input admin login --user admin <"$secrets/admin_password" 2>&1) || die "admin no pudo entrar: $(error_of "$out")"
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
    $new_password && die "el usuario $user ya existe, pero no está $password_file con su contraseña. Usa --user con otro nombre"
  else
    die "no se pudo crear el usuario $user: $(error_of "$out")"
  fi
  for who in "$@"; do
    out=$(printf '%s\n' "$password" | dfs_input "$who" login --user "$user" 2>&1) || die "la sesión $who no pudo entrar: $(error_of "$out")"
  done
}
