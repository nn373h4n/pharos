#!/usr/bin/env bash
# One look at a running Pharos installation (M5-11). Read-only: it changes nothing, reads no secret,
# and prints no person's data or any setting value.
#
#   scripts/status.sh [--backups DIR]
#
# Checks, each printed as PASS, WARN, FAIL or NOT CHECKED:
#   services   every long-running Compose service is running (and healthy, where it has a check)
#   readyz     the API's own readiness report: database, database role, redis, worker, migrations,
#              organization
#   backup     the age of the newest dump under DIR (daily/, weekly/, monthly/); only with --backups.
#              It looks at the file's date only, not its size or content: rehearse scripts/restore.sh
#   disk       free space on the disk that holds Docker's data
# It does NOT check: that the sources fetch, that the model answers, the budget, or that a dump can
# be restored (rehearse scripts/restore.sh). Those are on the admin pages and in the runbook.
#
# Exit status: 0 when nothing FAILs (WARN and NOT CHECKED do not fail), 1 when something FAILs,
# 2 for wrong usage. PHAROS_COMPOSE names the Compose command (default: this repository's file and
# deploy/.env), as in scripts/backup.sh.
set -euo pipefail
caller=$PWD # a relative --backups path is read from where the script was run, not from the repository
cd "$(dirname "$0")/.."

read -r -a COMPOSE <<<"${PHAROS_COMPOSE:-docker compose -f deploy/docker-compose.yml --env-file deploy/.env}"
compose() { "${COMPOSE[@]}" "$@"; }

# Services that run once and exit; every other service in the default profile must stay up.
ONE_SHOT=" migrate operator ollama-init ollama-pull "
BACKUP_WARN_HOURS=30
BACKUP_FAIL_HOURS=54
DISK_WARN_PERCENT=85
DISK_FAIL_PERCENT=95

usage() {
  echo "usage: $0 [--backups DIR]" >&2
  exit 2
}

backups=
while [ "$#" -gt 0 ]; do
  case $1 in
    --backups)
      [ "$#" -ge 2 ] || usage
      backups=$2
      case $backups in /*) ;; *) backups="$caller/$backups" ;; esac
      shift 2
      ;;
    *) usage ;;
  esac
done

failed=0
report() { # level name detail
  printf '%-12s %-9s %s\n' "$1" "$2" "$3"
  if [ "$1" = FAIL ]; then failed=1; fi
}

# --- services ---------------------------------------------------------------------------------
if services=$(compose config --services 2>/dev/null) && [ -n "$services" ]; then
  state=$(compose ps -a --format '{{.Service}}|{{.State}}|{{.Health}}' 2>/dev/null || true)
  bad=
  count=0
  while IFS= read -r name; do
    # A name that cannot be matched safely is a failure, never skipped: a skipped service is a
    # service nobody looked at.
    if ! [[ $name =~ ^[A-Za-z0-9._-]+$ ]]; then
      count=$((count + 1))
      bad="$bad (a service name this script cannot read)"
      continue
    fi
    case "$ONE_SHOT" in *" $name "*) continue ;; esac
    count=$((count + 1))
    line=
    while IFS= read -r candidate; do # the exact service name, not a prefix or a pattern
      if [ "${candidate%%|*}" = "$name" ]; then
        line=$candidate
        break
      fi
    done <<<"$state"
    run=${line#*|}
    run_state=${run%%|*}
    health=${run#*|}
    if [ "$run_state" != running ] || { [ -n "$health" ] && [ "$health" != healthy ]; }; then
      bad="$bad $name(${run_state:-missing}${health:+/$health})"
    fi
  done <<<"$services"
  if [ -n "$bad" ]; then
    report FAIL services "not running or not healthy:$bad"
  elif [ "$count" -eq 0 ]; then
    report FAIL services "no long-running service was found to check"
  else
    report PASS services "$count long-running services are up"
  fi
else
  report FAIL services "Compose did not answer (is this the right directory, and is Docker running?)"
fi

# --- readyz -----------------------------------------------------------------------------------
# /api/readyz is not served through the proxy, so ask the API container. The container prints
# `name=value` lines; they are checked against a strict pattern here and anything else is ignored.
probe='import json, urllib.error, urllib.request
u = "http://127.0.0.1:8000/api/readyz"
try:
    r = urllib.request.urlopen(u, timeout=5)
except urllib.error.HTTPError as e:
    r = e
d = json.loads(r.read().decode())
print("status=" + str(d.get("status")))
for k, v in sorted(d.get("checks", {}).items()):
    print(k + "=" + str(v))'
if ready=$(compose exec -T api python -c "$probe" 2>/dev/null); then
  failing=
  overall=
  while IFS= read -r line; do
    [[ $line =~ ^([a-z_]+)=([a-z]+)$ ]] || continue
    if [ "${BASH_REMATCH[1]}" = status ]; then
      overall=${BASH_REMATCH[2]}
    elif [ "${BASH_REMATCH[2]}" != ok ]; then
      failing="$failing ${BASH_REMATCH[1]}"
    fi
  done <<<"$ready"
  if [ "$overall" = ok ] && [ -z "$failing" ]; then
    report PASS readyz "ready"
  else
    report FAIL readyz "not ready; failing checks:${failing:- (no report)}; see docs/DEPLOYMENT.md, Troubleshooting"
  fi
else
  report FAIL readyz "the API container did not answer"
fi

# --- backup -----------------------------------------------------------------------------------
if [ -z "$backups" ]; then
  report "NOT CHECKED" backup "no --backups DIR given"
elif [ ! -d "$backups" ]; then
  report FAIL backup "no such directory"
else
  newest=$(find "$backups/daily" "$backups/weekly" "$backups/monthly" -maxdepth 1 -type f \
    -name 'pharos-*.dump' -printf '%T@\n' 2>/dev/null | sort -rn | head -n 1 || true)
  if [ -z "$newest" ]; then
    report FAIL backup "no dump found under daily/, weekly/ or monthly/"
  elif ! [[ $newest =~ ^[0-9]+(\.[0-9]+)?$ ]]; then
    report FAIL backup "the date of the newest dump could not be read"
  else
    # Only the file's date is checked: not its size, its content or whether it restores.
    hours=$(((${EPOCHSECONDS:-$(date +%s)} - ${newest%.*}) / 3600))
    if [ "$hours" -lt 0 ]; then
      report FAIL backup "the newest dump is dated in the future (check the clock)"
    elif [ "$hours" -ge "$BACKUP_FAIL_HOURS" ]; then
      report FAIL backup "newest dump is ${hours} hours old (limit ${BACKUP_FAIL_HOURS})"
    elif [ "$hours" -ge "$BACKUP_WARN_HOURS" ]; then
      report WARN backup "newest dump is ${hours} hours old (warning at ${BACKUP_WARN_HOURS})"
    else
      report PASS backup "newest dump is ${hours} hours old"
    fi
  fi
fi

# --- disk -------------------------------------------------------------------------------------
root=$(docker info --format '{{.DockerRootDir}}' 2>/dev/null || true)
used=
if [ -n "$root" ] && [ -d "$root" ]; then
  used=$(df -P "$root" 2>/dev/null | awk 'NR==2 { gsub("%", "", $5); print $5 }' || true)
fi
if [[ $used =~ ^[0-9]+$ ]]; then
  if [ "$used" -ge "$DISK_FAIL_PERCENT" ]; then
    report FAIL disk "${used}% used (limit ${DISK_FAIL_PERCENT}%)"
  elif [ "$used" -ge "$DISK_WARN_PERCENT" ]; then
    report WARN disk "${used}% used (warning at ${DISK_WARN_PERCENT}%)"
  else
    report PASS disk "${used}% used"
  fi
else
  report "NOT CHECKED" disk "Docker's data directory is not readable by this user"
fi

exit "$failed"
