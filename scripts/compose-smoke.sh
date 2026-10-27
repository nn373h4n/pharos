#!/usr/bin/env bash
# End-to-end check of the Docker Compose stack, in a throwaway project: own name, own volumes,
# own image tag, random passwords, high ports. It never touches deploy/.env, the images of a real
# installation (they use the tag "local") or any other running project, and it removes everything it
# created when it ends. The test ports listen on all interfaces for the few minutes it runs.
# Usage: make test-compose (needs Docker)
set -uo pipefail
cd "$(dirname "$0")/.."

PROJECT=pharos-smoke
TAG=smoke
HTTP_PORT=${PHAROS_SMOKE_HTTP_PORT:-18080}
HTTPS_PORT=${PHAROS_SMOKE_HTTPS_PORT:-18443}
WORK=$(mktemp -d)
ENV_FILE="$WORK/env"

secret() { openssl rand -hex 24; }
POSTGRES_PASSWORD=$(secret)
APP_PASSWORD=$(secret)
umask 077
cat > "$ENV_FILE" <<EOF
POSTGRES_PASSWORD=$POSTGRES_PASSWORD
PHAROS_OWNER_PASSWORD=$(secret)
PHAROS_APP_PASSWORD=$APP_PASSWORD
PHAROS_REDIS_PASSWORD=$(secret)
PHAROS_DOMAIN=localhost
PHAROS_TLS=internal
PHAROS_HTTP_PORT=$HTTP_PORT
PHAROS_HTTPS_PORT=$HTTPS_PORT
PHAROS_ENVIRONMENT=production
PHAROS_DEFAULT_LOCALE=az
PHAROS_MIGRATE_ON_START=true
PHAROS_IMAGE_TAG=$TAG
PHAROS_PUBLIC_URL=https://localhost:$HTTPS_PORT
PHAROS_ORG_SLUG=smoke
PHAROS_SECRET_KEY=$(secret)
EOF

compose() { docker compose -p "$PROJECT" -f deploy/docker-compose.yml --env-file "$ENV_FILE" "$@"; }
cleanup() {
  compose down --volumes --remove-orphans >/dev/null 2>&1
  docker rmi "pharos-web:$TAG" "pharos-backend:$TAG" >/dev/null 2>&1
  rm -r "$WORK"
}
trap cleanup EXIT

failures=0
pass() { printf 'PASS  %s\n' "$1"; }
fail() { printf 'FAIL  %s\n' "$1"; failures=$((failures + 1)); }
check() { # description, command...  (the command must succeed)
  local description=$1
  shift
  if "$@" >/dev/null 2>&1; then pass "$description"; else fail "$description"; fi
}
check_blocked() { # description, command...  (the command must fail)
  local description=$1
  shift
  if "$@" >/dev/null 2>&1; then fail "$description"; else pass "$description"; fi
}

compose down --volumes --remove-orphans >/dev/null 2>&1
echo "== build and start"
if ! compose up -d --build 2>&1 | tail -5; then
  fail "docker compose up"
  exit 1
fi

health() { docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$(compose ps -q "$1")" 2>/dev/null; }
echo "== waiting for the services to become healthy (up to 5 minutes)"
for _ in $(seq 1 150); do
  pending=""
  for service in postgres redis web api worker caddy; do
    [ "$(health "$service")" = "healthy" ] || pending="$pending $service"
  done
  [ -z "$pending" ] && break
  sleep 2
done
if [ -n "$pending" ]; then
  fail "not healthy after 5 minutes:$pending"
  compose ps -a
  for service in $pending; do compose logs --tail 15 "$service" 2>&1; done
  exit 1
fi
pass "every service is healthy"
migrate_exit=$(docker inspect --format '{{.State.ExitCode}}' "$(compose ps -a -q migrate)")
check "migrate finished successfully" test "$migrate_exit" = 0

echo "== through the edge proxy"
https() { curl -sk --max-time 10 --resolve "localhost:$HTTPS_PORT:127.0.0.1" "$@"; }
base="https://localhost:$HTTPS_PORT"
home=$(https -L -H 'Accept-Language: de' "$base/")
check "the home page is served in the installation default language (az)" grep -q 'lang="az"' <<<"$home"
check "web health check is reachable" https -f "$base/healthz"
check "API liveness is reachable under /api" https -f "$base/api/healthz"
check "HTTP redirects to HTTPS" \
  test "$(curl -s --max-time 10 -o /dev/null -w '%{http_code}' -H "Host: localhost:$HTTP_PORT" "http://127.0.0.1:$HTTP_PORT/")" = 308
check "the redirect names the configured domain and HTTPS port" \
  test "$(curl -s --max-time 10 -o /dev/null -w '%{redirect_url}' -H "Host: localhost:$HTTP_PORT" "http://127.0.0.1:$HTTP_PORT/x?y=1")" = "https://localhost:$HTTPS_PORT/x?y=1"
foreign=$(curl -s --max-time 10 -D - -o /dev/null -H 'Host: evil.example' "http://127.0.0.1:$HTTP_PORT/x"; true)
echo "$foreign" | sed 's/^/      /'
check_blocked "a foreign Host header is never reflected into a redirect" grep -qi 'evil.example' <<<"$foreign"
headers=$(https -D - -o /dev/null -H 'X-Request-ID: client-chosen' "$base/api/healthz")
check_blocked "a client-chosen X-Request-ID is replaced by the proxy" grep -qi 'x-request-id: client-chosen' <<<"$headers"

# /readyz must not be served to the internet, whatever the spelling of the path.
leaks=0
for path in /api/readyz /api/readyz/ /API/readyz /api//readyz /api/%72eadyz /api/./readyz /api/readyz%2F; do
  body=$(https "$base$path")
  if grep -q '"checks"' <<<"$body"; then
    leaks=$((leaks + 1))
    printf '      readiness data leaked through %s\n' "$path"
  fi
done
check "/api/readyz is not served to the internet, under any spelling of the path" test "$leaks" = 0
check "/api/readyz answers 404" test "$(https -o /dev/null -w '%{http_code}' "$base/api/readyz")" = 404

echo "== readiness on the internal network"
readyz() { compose exec -T api python -c "import urllib.error, urllib.request
try:
    print(urllib.request.urlopen('http://127.0.0.1:8000/api/readyz', timeout=5).read().decode())
except urllib.error.HTTPError as error:
    print(error.read().decode())" 2>&1; }
# The organization is created by the operator after the stack is up, so a fresh stack is not ready.
before=$(readyz)
echo "      $before"
check "before the organization exists readyz is unavailable and names only that check" \
  grep -q '"status":"unavailable".*"organization":"fail"' <<<"$before"
check_blocked "no other check fails before the organization exists" \
  grep -qE '"(database|database_role|redis|worker|migrations)":"fail"' <<<"$before"
compose exec -T postgres psql -q -U postgres -d pharos \
  -c "INSERT INTO organizations (name, slug) VALUES ('Smoke', 'smoke')" >/dev/null
ready=$(readyz)
echo "      $ready"
check "readyz reports database, application role, redis, worker, migrations and organization all ok" grep -q '"status":"ok"' <<<"$ready"

echo "== network isolation"
# Every probe is a plain TCP connection made with a tool the image already has. A probe only counts
# when the same command succeeds where the connection is allowed (the control); otherwise
# "blocked" could just mean "the tool does not work" or "this host has no internet".
NODE_PROBE="require('net').connect({host:process.argv[1],port:Number(process.argv[2]),timeout:5000}).on('connect',()=>process.exit(0)).on('error',()=>process.exit(1)).on('timeout',()=>process.exit(1))"
# probe_in_service <service> <host> <port>
probe_in_service() {
  case $1 in
    postgres) compose exec -T postgres timeout 5 bash -c "exec 3<>/dev/tcp/$2/$3" ;;
    redis | caddy) compose exec -T "$1" nc -w 5 -z "$2" "$3" ;;
    web) compose exec -T web node -e "$NODE_PROBE" "$2" "$3" ;;
    api) compose exec -T api python -c "import socket, sys; socket.create_connection((sys.argv[1], int(sys.argv[2])), timeout=5)" "$2" "$3" ;;
  esac
}
# The same tool on a plain Docker network, where the connection is allowed.
probe_on_plain_network() {
  case $1 in
    postgres) docker run --rm --entrypoint timeout "$(scripts/compose-image.sh postgres)" 5 bash -c "exec 3<>/dev/tcp/$2/$3" ;;
    redis | caddy) docker run --rm --entrypoint nc "$(scripts/compose-image.sh redis)" -w 5 -z "$2" "$3" ;;
    web) docker run --rm --entrypoint node "pharos-web:$TAG" -e "$NODE_PROBE" "$2" "$3" ;;
  esac
}
blocked() { # description, service, host, port
  if ! probe_on_plain_network "$2" "1.1.1.1" 443 >/dev/null 2>&1; then
    printf 'NOTE  %-9s probe fails on a plain Docker network too (no outbound access here?); not proven: %s\n' "$2" "$1"
  elif probe_in_service "$2" "$3" "$4" >/dev/null 2>&1; then
    fail "$1"
  else
    pass "$1 (the same tool reaches 1.1.1.1 on a plain Docker network)"
  fi
}
blocked "postgres cannot reach the internet" postgres 1.1.1.1 443
blocked "redis cannot reach the internet" redis 1.1.1.1 443
blocked "web cannot reach the internet" web 1.1.1.1 443
blocked "web cannot connect to postgres" web postgres 5432
blocked "web cannot connect to redis" web redis 6379
blocked "caddy cannot connect to postgres" caddy postgres 5432
blocked "caddy cannot connect to redis" caddy redis 6379
# Positive controls for the design: the services that must reach something do.
check "api reaches the internet through the egress network" probe_in_service api 1.1.1.1 443
check "api reaches postgres" probe_in_service api postgres 5432
check "api reaches redis" probe_in_service api redis 6379
check "caddy reaches the API on the app network" probe_in_service caddy api 8000
check "caddy reaches the web app on the app network" probe_in_service caddy web 3000
check_blocked "postgres is not published on the host (no port mapping)" bash -c "docker port $(compose ps -q postgres) | grep -q ."
check_blocked "redis is not published on the host (no port mapping)" bash -c "docker port $(compose ps -q redis) | grep -q ."

echo "== database access"
sql() { compose exec -T -e "PGPASSWORD=$2" postgres psql -h postgres -U "$1" -d pharos -tAc "$3"; }
check "the application role can log in over the network" test "$(sql pharos_app "$APP_PASSWORD" 'select 1' 2>&1)" = 1
check_blocked "the superuser cannot log in over the network, even with its password" sql postgres "$POSTGRES_PASSWORD" 'select 1'
check "the application role is neither superuser nor allowed to bypass row-level security" \
  test "$(sql pharos_app "$APP_PASSWORD" "select rolsuper or rolbypassrls from pg_roles where rolname = current_user" 2>&1)" = f

echo "== hardening"
for service in api worker beat web postgres redis; do
  user=$(compose exec -T "$service" id -u 2>/dev/null | tr -d '\r')
  if [ -n "$user" ] && [ "$user" != "0" ]; then
    printf 'PASS  %-9s runs as uid %s (not root)\n' "$service" "$user"
  else
    fail "$service runs as uid ${user:-unknown}"
  fi
done
for service in caddy web api worker beat migrate postgres redis; do
  container=$(compose ps -a -q "$service")
  settings=$(docker inspect --format '{{.HostConfig.ReadonlyRootfs}} {{.HostConfig.CapDrop}} {{.HostConfig.SecurityOpt}} {{.HostConfig.PidsLimit}}' "$container")
  case "$settings" in
    "true [ALL] [no-new-privileges:true] 256") printf 'PASS  %-9s read-only root, all capabilities dropped, no-new-privileges, 256 processes max\n' "$service" ;;
    *) fail "$service hardening is: $settings" ;;
  esac
done

echo
if [ "$failures" -eq 0 ]; then
  echo "All checks passed."
else
  echo "$failures check(s) FAILED."
  exit 1
fi
