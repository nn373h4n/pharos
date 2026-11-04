#!/usr/bin/env bash
# Backup of the Pharos database (ADR-0022). A script for the host's scheduler, not a service.
#
#   scripts/backup.sh run   --dir DIR            dump, check the dump, keep 7 daily, 4 weekly, 3 monthly
#   scripts/backup.sh prune --dir DIR            only remove old dumps (what `run` does at its end)
#   scripts/backup.sh env   --dir ENVDIR --dumps DIR
#                                                copy deploy/.env to a directory that is NOT the dumps
#
# The dump is a custom-format pg_dump made as the database owner through the container's local
# socket, so no password is on any command line. It holds every person's data: the directory must be
# yours and private (mode 700, files 600); an existing directory that is not is refused. Each dump
# gets a SHA-256 file beside it (scripts/restore.sh checks it). The roles are not in a dump (restore
# needs them first) and neither is deploy/.env, which holds the root secret that the restored data
# needs (the audit chain, the tokens): `env` copies it apart, and it must not travel with the dumps.
#
# Relative paths are read from the directory you run the script in, and a path inside this
# repository is refused, so a dump or the secret can never be committed by `git add -A`.
#
# The off-host copy and its encryption belong to the operator (docs/DEPLOYMENT.md, "Backup and
# restore"). A dump that has never been restored is not a backup: rehearse scripts/restore.sh.
#
# PHAROS_COMPOSE names the Compose command (default: this repository's file and deploy/.env).
set -euo pipefail
CALLER=$PWD
cd "$(dirname "$0")/.."
# Links resolved: a path compared with the repository must be compared in the same form.
ROOT=$(pwd -P)

umask 077
read -r -a COMPOSE <<<"${PHAROS_COMPOSE:-docker compose -f deploy/docker-compose.yml --env-file deploy/.env}"
compose() { "${COMPOSE[@]}" "$@"; }

KEEP_DAILY=7
KEEP_WEEKLY=4
KEEP_MONTHLY=3
NAME_RE='^pharos-[0-9]{8}T[0-9]{6}Z\.dump$'

die() { echo "backup: $*" >&2; exit 1; }

usage() {
  sed -n '2,6p' "$0" | sed 's/^# \{0,1\}//' >&2
  exit 2
}

# A stamp is UTC time as YYYYMMDDTHHMMSSZ. PHAROS_BACKUP_NOW lets a test fix the clock.
stamp() {
  local now=${PHAROS_BACKUP_NOW:-$(date -u +%Y%m%dT%H%M%SZ)}
  [[ $now =~ ^[0-9]{8}T[0-9]{6}Z$ ]] || die "PHAROS_BACKUP_NOW is not a stamp: $now"
  printf '%s' "$now"
}

# A path made absolute from where the caller stood. It does not judge the path: the callers refuse a
# name that starts with "-" before they use this, in their own shell.
absolute() {
  case $1 in /*) printf '%s' "$1" ;; *) printf '%s/%s' "$CALLER" "$1" ;; esac
}

# Make the directory if it is new; refuse one that is not ours or that others can write to; refuse
# anything inside this repository.
private_dir() {
  local path=$1 real
  # Trailing slashes off: `test -L link/` is false for a link, and a link must be refused.
  path=${path%"${path##*[!/]}"}
  [ -n "$path" ] || die "an empty directory name"
  # Before anything is made: where would it be, links followed?
  real=$(realpath -m -- "$path") || die "cannot resolve $path"
  [ -n "$real" ] || die "cannot resolve $path"
  case "$real/" in "$ROOT"/*) die "$path is inside the repository; a dump or the secret could be committed" ;; esac
  [ ! -L "$path" ] || die "$path is a link, not a plain directory"
  mkdir -p -m 700 "$path"
  real=$(cd -- "$path" && pwd -P) || die "cannot enter $path"
  # After it exists, once more with the real path: the answer must be the same.
  case "$real/" in "$ROOT"/*) die "$path is inside the repository; a dump or the secret could be committed" ;; esac
  [ -d "$real" ] || die "$path is not a directory"
  [ "$(stat -c %u "$real")" = "$(id -u)" ] || die "$path is not owned by you"
  [ $(( 0$(stat -c %a "$real") & 077 )) -eq 0 ] || die "$path is open to others (mode $(stat -c %a "$real"); use chmod 700)"
  printf '%s' "$real"
}

# Remove a dump with its checksum file.
remove_dump() { rm -f -- "$1" "$1.sha256"; }

# The names of the dumps in a directory, newest first, one per line. It looks at files only, and
# only at names that are exactly a dump's name (a name with a line break in it never matches).
dump_names() {
  local path name
  for path in "$1"/pharos-*.dump; do
    [ -f "$path" ] || continue
    name=${path##*/}
    if [[ $name =~ $NAME_RE ]]; then printf '%s\n' "$name"; fi
  done | sort -r
}

# Keep the newest $2 dumps of directory $1 (names sort by time) and never the one named $3 (the
# dump just made, in whichever directory its copy is); touch nothing else.
prune_dir() {
  local dir=$1 keep=$2 spare=${3:-} file count=0
  [ -d "$dir" ] || return 0
  while IFS= read -r file; do
    count=$((count + 1))
    if [ "$count" -gt "$keep" ] && [ "$file" != "$spare" ]; then remove_dump "$dir/$file"; fi
  done < <(dump_names "$dir")
}

prune() {
  local dir=$1 spare=${2:-}
  prune_dir "$dir/daily" "$KEEP_DAILY" "$spare"
  prune_dir "$dir/weekly" "$KEEP_WEEKLY" "$spare"
  prune_dir "$dir/monthly" "$KEEP_MONTHLY" "$spare"
}

# One run at a time in a directory (backup or prune): they would share files.
lock() {
  exec 9>"$1/.lock"
  flock -n 9 || die "another backup or prune is running in $1"
}

# Reads --dir from the arguments of the sub-command. A name that starts with "-" is refused here, in
# the shell itself: inside a command substitution a refusal would not stop the script.
dir_argument() {
  dir=""
  while [ $# -gt 0 ]; do
    case $1 in
      --dir) [ $# -ge 2 ] || usage; dir=$2; shift 2 ;;
      *) shift ;;
    esac
  done
  [ -n "$dir" ] || usage
  case $dir in -*) die "a path may not start with '-': $dir" ;; esac
  case $dir in /*) ;; *) dir="$CALLER/$dir" ;; esac
}

run() {
  dir_argument "$@"
  local now date_part day weekday final list newer
  now=$(stamp)
  date_part=${now:0:8}
  dir=$(private_dir "$dir")
  for sub in daily weekly monthly; do private_dir "$dir/$sub" >/dev/null; done
  # One backup at a time; two runs in the same second would share a file.
  lock "$dir"
  # Under the lock no other run is writing: a partial file from a run that was killed (SIGKILL, power
  # loss: the exit trap did not run) is stale and full-sized, so it goes.
  rm -f -- "$dir"/daily/.pharos-*.partial
  final="$dir/daily/pharos-$now.dump"
  # A new, private file that nobody else can have made or linked first (O_EXCL).
  partial=$(mktemp "$dir/daily/.pharos-XXXXXXXX.partial")
  trap 'rm -f -- "${partial:-}"' EXIT

  # As the owner role, through the local socket of the container: no password is involved.
  compose exec -T postgres pg_dump --format=custom --username=pharos_owner --dbname=pharos \
    >"$partial" || die "pg_dump failed"
  [ -s "$partial" ] || die "the dump is empty"

  # The dump is read back whole (not only its table of contents): pg_restore must get to the end,
  # and the schema must be in it.
  compose exec -T postgres pg_restore --file=/dev/null <"$partial" || die "the dump cannot be read to its end"
  list=$(compose exec -T postgres pg_restore --list <"$partial") || die "the dump cannot be read back"
  grep -q ' alembic_version ' <<<"$list" || die "the dump has no alembic_version table"

  # A clock that went wrong once would sort a dump ahead of all later ones; say so.
  newer=$(dump_names "$dir/daily" | awk -v n="pharos-$now.dump" '$0 > n' | head -1 || true)
  [ -z "$newer" ] || echo "backup: WARNING $newer is newer than this dump; check the host clock" >&2

  mv -- "$partial" "$final"
  chmod 600 "$final"
  (cd "$dir/daily" && sha256sum -- "pharos-$now.dump" >"pharos-$now.dump.sha256")
  chmod 600 "$final.sha256"
  weekday=$(date -u -d "$date_part" +%u)
  day=${date_part:6:2}
  if [ "$weekday" = 7 ]; then cp -- "$final" "$final.sha256" "$dir/weekly/"; fi
  if [ "$day" = 01 ]; then cp -- "$final" "$final.sha256" "$dir/monthly/"; fi
  prune "$dir" "$(basename -- "$final")"
  [ -f "$final" ] || die "the new dump is gone after pruning"
  echo "backup: wrote $final ($(wc -c <"$final") bytes)"
}

env_copy() {
  local envdir="" dumps="" source=${PHAROS_ENV_FILE:-deploy/.env}
  while [ $# -gt 0 ]; do
    case $1 in
      --dir) [ $# -ge 2 ] || usage; envdir=$2; shift 2 ;;
      --dumps) [ $# -ge 2 ] || usage; dumps=$2; shift 2 ;;
      *) shift ;;
    esac
  done
  [ -n "$envdir" ] && [ -n "$dumps" ] || usage
  # Refused here, in this shell: a refusal inside "$(...)" would hand back an empty path, and an
  # empty path is the repository directory.
  case $envdir in -*) die "a path may not start with '-': $envdir" ;; esac
  case $dumps in -*) die "a path may not start with '-': $dumps" ;; esac
  [ -f "$source" ] || die "$source does not exist"
  local e d
  envdir=$(absolute "$envdir")
  dumps=$(absolute "$dumps")
  e=$(private_dir "$envdir")
  d=$(private_dir "$dumps")
  # The root secret must not sit beside the data it unlocks.
  case "$e/" in "$d"/*) die "the env directory is the dump directory or inside it" ;; esac
  case "$d/" in "$e"/*) die "the env directory contains the dump directory" ;; esac
  # The stamp first, on its own line: `local x=$(f)` would hide a failing f behind local's own status.
  local when target
  when=$(stamp)
  target="$e/pharos-env-$when"
  cp -- "$source" "$target"
  chmod 600 "$target"
  echo "backup: copied the environment file to $target"
  echo "backup: keep it apart from the dumps, encrypted, and not under a parent directory that is synced with them"
}

[ $# -ge 1 ] || usage
command=$1
shift
case $command in
  run) run "$@" ;;
  prune)
    dir_argument "$@"
    dir=$(private_dir "$dir")
    lock "$dir"
    prune "$dir"
    echo "backup: pruned $dir"
    ;;
  env) env_copy "$@" ;;
  *) usage ;;
esac
