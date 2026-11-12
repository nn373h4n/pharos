#!/usr/bin/env bash
# The starter catalogue fetched once, for real, through the real worker (M5-08, F-30 light).
#
# A THROWAWAY copy of the stack is built from the checkout (own project name, volumes, image tag,
# random passwords, high ports, no test overlay: the worker reaches the real internet through its
# egress network and SafeHttpClient, exactly as in an installation). The script creates the
# organization and an administrator with a generated password (never printed, never on a command
# line), imports the starter sources, signs in through the API, switches every source on and waits
# until each one has been fetched or has failed. It prints one table and removes everything again.
#
# This is a manual check and never part of CI: it contacts the 25 public sites of
# backend/src/pharos/sources/starter_sources.json from this host and takes several minutes.
# The language model and personal feeds stay off.
#
# Usage: scripts/starter-catalogue-check.sh [--timeout SECONDS]   (or: make check-catalogue)
#   --timeout   how long to wait for the fetches after switching the sources on (default 600)
# Exit: 0 every source fetched well; 1 a source failed, was not fetched in time, or the stack did
# not come up; 2 wrong usage. Needs Docker, curl, openssl and python3.
set -euo pipefail
cd "$(dirname "$0")/.."

usage() { echo "usage: scripts/starter-catalogue-check.sh [--timeout SECONDS]"; }
die() {
  echo "FAIL  $*" >&2
  exit 1
}

TIMEOUT=${CATALOGUE_TIMEOUT:-600}
while [ "$#" -gt 0 ]; do
  case "$1" in
    --timeout)
      [ "$#" -ge 2 ] || { usage >&2; exit 2; }
      TIMEOUT=$2
      shift 2
      ;;
    -h | --help) usage; exit 0 ;;
    *) usage >&2; exit 2 ;;
  esac
done
if ! [[ "$TIMEOUT" =~ ^[0-9]+$ ]] || [ "$TIMEOUT" -lt 1 ]; then
  usage >&2
  exit 2
fi
POLL_SECONDS=${CATALOGUE_POLL_SECONDS:-15}

PROJECT=pharos-catalogue
TAG=catalogue
HTTP_PORT=${PHAROS_CATALOGUE_HTTP_PORT:-19880}
HTTPS_PORT=${PHAROS_CATALOGUE_HTTPS_PORT:-19843}
ORG_SLUG=catalogue
ADMIN=catalogue-admin
BASE="https://localhost:$HTTPS_PORT"
LOCK=${CATALOGUE_LOCK:-${XDG_RUNTIME_DIR:-$HOME}/.pharos-catalogue.lock}

# Compose reads a variable from the shell before the env file. With a real installation's settings
# exported, this run would re-tag that installation's images (PHAROS_IMAGE_TAG), hand its tokens to
# the throwaway worker or load its profiles: the generated file must be the only source.
while read -r name; do
  unset "$name"
done < <(compgen -e | grep -E '^(PHAROS_|POSTGRES_|COMPOSE_)' || true)

# One run at a time: a second one would start by taking down the first one's project.
if command -v flock >/dev/null; then
  exec 9>>"$LOCK"
  flock -n 9 || die "another starter catalogue check is running (lock $LOCK)"
fi

umask 077
WORK=$(mktemp -d)
ENV_FILE="$WORK/stack-env"
JAR="$WORK/cookies"
CSRF_HEADER="$WORK/csrf-header"

compose() { docker compose -p "$PROJECT" -f deploy/docker-compose.yml --env-file "$ENV_FILE" "$@"; }
cleanup() {
  compose down --volumes --remove-orphans >/dev/null 2>&1 || true
  docker rmi "pharos-web:$TAG" "pharos-backend:$TAG" >/dev/null 2>&1 || true
  rm -r -- "$WORK"
}
trap cleanup EXIT

secret() { openssl rand -hex 24; }
cat > "$ENV_FILE" <<EOF
POSTGRES_PASSWORD=$(secret)
PHAROS_OWNER_PASSWORD=$(secret)
PHAROS_APP_PASSWORD=$(secret)
PHAROS_WORKER_DB_PASSWORD=$(secret)
PHAROS_REDIS_PASSWORD=$(secret)
PHAROS_REDIS_WORKER_PASSWORD=$(secret)
PHAROS_SECRET_KEY=$(secret)
PHAROS_DOMAIN=localhost
PHAROS_TLS=internal
PHAROS_HTTP_PORT=$HTTP_PORT
PHAROS_HTTPS_PORT=$HTTPS_PORT
PHAROS_APP_SUBNET=172.28.36.0/24
PHAROS_APP_DYNAMIC_RANGE=172.28.36.0/25
PHAROS_CADDY_IP=172.28.36.254
PHAROS_OLLAMA_SUBNET=172.28.37.0/24
PHAROS_OLLAMA_IP=172.28.37.10
PHAROS_ENVIRONMENT=production
PHAROS_MIGRATE_ON_START=true
PHAROS_IMAGE_TAG=$TAG
PHAROS_PUBLIC_URL=$BASE
PHAROS_DEFAULT_LOCALE=en
PHAROS_ORG_SLUG=$ORG_SLUG
PHAROS_LLM_PROVIDER=off
PHAROS_PERSONAL_FEEDS=off
EOF

# The administrator's password lives only in this mode-600 file, as the body of the sign-in.
ADMIN_PASSWORD=$(secret)
printf '{"username":"%s","password":"%s"}' "$ADMIN" "$ADMIN_PASSWORD" > "$WORK/login.json"
printf '{"enabled":true}' > "$WORK/enable.json"

api() { # method, path, output file, [body file]; prints the HTTP status
  local method=$1 path=$2 out=$3 body=${4-}
  local args=(-sk --max-time 30 --resolve "localhost:$HTTPS_PORT:127.0.0.1" -X "$method"
    -o "$out" -w '%{http_code}' -b "$JAR" -c "$JAR" -H "Origin: $BASE")
  if [ -s "$CSRF_HEADER" ]; then args+=(-H "@$CSRF_HEADER"); fi
  if [ -n "$body" ]; then args+=(-H 'Content-Type: application/json' --data-binary "@$body"); fi
  curl "${args[@]}" "$BASE$path" || true
}
json() { # file, python expression over `d`
  python3 -I -c "import json, sys; d = json.load(open(sys.argv[1])); print($2)" "$1"
}

started=$SECONDS
compose down --volumes --remove-orphans >/dev/null 2>&1 || true
echo "== build and start ($PROJECT, ports $HTTP_PORT and $HTTPS_PORT)"
if ! compose up -d --build 2>&1 | tail -5; then die "the stack did not start (compose up)"; fi

health() {
  docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' \
    "$(compose ps -q "$1")" 2>/dev/null || true
}
pending=""
for _ in $(seq 1 150); do
  pending=""
  for service in postgres redis api worker caddy; do
    [ "$(health "$service")" = healthy ] || pending="$pending $service"
  done
  [ -z "$pending" ] && break
  sleep 2
done
[ -z "$pending" ] || die "not healthy after 5 minutes:$pending"
echo "every service is healthy ($((SECONDS - started)) s)"

echo "== organization, administrator, starter sources"
compose run --rm migrate python -m pharos.cli create-org --slug "$ORG_SLUG" --name "Catalogue" \
  >/dev/null || die "create-org"
printf '%s\n' "$ADMIN_PASSWORD" |
  compose exec -T api python -m pharos.cli create-admin --username "$ADMIN" --password-stdin \
    >/dev/null || die "create-admin"
compose exec -T api python -m pharos.cli import-sources || die "import-sources"

echo "== signing in and switching every source on"
status=$(api POST /api/auth/local/login "$WORK/login-answer.json" "$WORK/login.json")
[ "$status" = 200 ] || die "sign-in answered $status"
printf 'X-CSRF-Token: %s' "$(json "$WORK/login-answer.json" "d['csrf_token']")" > "$CSRF_HEADER"
rm -- "$WORK/login.json" "$WORK/login-answer.json"

status=$(api GET "/api/sources?limit=100" "$WORK/sources.json")
[ "$status" = 200 ] || die "the source list answered $status"
total=$(json "$WORK/sources.json" "d['total']")
[ "$total" -ge 1 ] || die "no source was imported"
for id in $(json "$WORK/sources.json" "' '.join(s['id'] for s in d['items'])"); do
  status=$(api PATCH "/api/sources/$id" "$WORK/patch.json" "$WORK/enable.json")
  [ "$status" = 200 ] || die "switching source $id on answered $status"
done
echo "$total source(s) switched on"

echo "== waiting for every source to be fetched (up to $TIMEOUT s)"
deadline=$((SECONDS + TIMEOUT))
waiting=$total
while :; do
  status=$(api GET "/api/sources?limit=100" "$WORK/sources.json")
  [ "$status" = 200 ] || die "the source list answered $status"
  waiting=$(json "$WORK/sources.json" \
    "sum(1 for s in d['items'] if not s['last_success_at'] and not s['consecutive_failures'])")
  echo "  $((total - waiting)) of $total done"
  [ "$waiting" -eq 0 ] && break
  [ "$SECONDS" -lt "$deadline" ] || break
  sleep "$POLL_SECONDS"
done

mkdir "$WORK/runs"
for id in $(json "$WORK/sources.json" "' '.join(s['id'] for s in d['items'])"); do
  status=$(api GET "/api/sources/$id/runs?limit=50" "$WORK/runs/$id.json")
  [ "$status" = 200 ] || printf '{"items": []}' > "$WORK/runs/$id.json"
done

echo
echo "== result ($((SECONDS - started)) s in all)"
set +e
python3 -I - "$WORK/sources.json" "$WORK/runs" <<'PYEOF'
import json
import sys
from pathlib import Path

sources = json.loads(Path(sys.argv[1]).read_text())["items"]
runs_dir = Path(sys.argv[2])


def cut(text: str, width: int) -> str:
    return text if len(text) <= width else text[: width - 1] + "~"


rows, counts = [], {"ok": 0, "failed": 0, "not fetched": 0}
for source in sources:
    path = runs_dir / f"{source['id']}.json"
    runs = json.loads(path.read_text())["items"] if path.exists() else []
    finished = [run for run in runs if run["outcome"] != "running"]
    articles = sum(run["items_new"] or 0 for run in finished)
    if source["consecutive_failures"]:
        health = "failed"
    elif source["last_success_at"]:
        health = "ok"
    else:
        health = "not fetched"
    counts[health] += 1
    problem = ""
    bad = [run for run in finished if run["outcome"] != "ok"]
    if health != "ok" and bad:
        newest = bad[0]
        problem = newest["error_code"] or newest["outcome"]
        if newest["http_status"]:
            problem += f" (HTTP {newest['http_status']})"
    elif health != "ok":
        problem = source["last_error_code"] or "no finished run"
    rows.append((source["name"], source["type"], health, str(articles), problem))

header = ("name", "type", "health", "articles", "last problem")
widths = (36, 10, 11, 8, 40)
for row in (header, *rows):
    print("  ".join(cut(cell, width).ljust(width) for cell, width in zip(row, widths)).rstrip())
print()
print(
    f"{len(sources)} source(s): {counts['ok']} ok, {counts['failed']} failed, "
    f"{counts['not fetched']} not fetched in time"
)
sys.exit(0 if counts["ok"] == len(sources) and sources else 1)
PYEOF
result=$?
set -e
exit "$result"
