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
WORKER_DB_PASSWORD=$(secret)
umask 077
cat > "$ENV_FILE" <<EOF
POSTGRES_PASSWORD=$POSTGRES_PASSWORD
PHAROS_OWNER_PASSWORD=$(secret)
PHAROS_APP_PASSWORD=$APP_PASSWORD
PHAROS_WORKER_DB_PASSWORD=$WORKER_DB_PASSWORD
PHAROS_REDIS_PASSWORD=$(secret)
PHAROS_REDIS_WORKER_PASSWORD=$(secret)
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
PHAROS_APP_SUBNET=172.28.31.0/24
PHAROS_APP_DYNAMIC_RANGE=172.28.31.0/25
PHAROS_CADDY_IP=172.28.31.254
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

echo "== which client address the API believes"
# The API logs the peer address it ended up with. Behind Caddy that is the real client; a header sent
# by anyone else must change nothing.
FORGED=203.0.113.77
https -o /dev/null -H "X-Forwarded-For: $FORGED" "$base/api/healthz?probe=via-caddy"
compose exec -T web node -e "fetch('http://api:8000/api/healthz?probe=direct', {headers: {'x-forwarded-for': '$FORGED'}}).then((r) => process.exit(r.ok ? 0 : 1))"
sleep 1
api_log=$(compose logs --no-log-prefix api 2>&1)
via_caddy=$(grep 'probe=via-caddy' <<<"$api_log" | tail -1)
direct=$(grep 'probe=direct' <<<"$api_log" | tail -1)
web_ip=$(docker inspect --format '{{(index .NetworkSettings.Networks "'"$PROJECT"'_app").IPAddress}}' "$(compose ps -q web)")
echo "      via Caddy: $(grep -o '"message": "[^"]*"' <<<"$via_caddy")"
echo "      direct:    $(grep -o '"message": "[^"]*"' <<<"$direct")"
check "the request through Caddy reached the API log (positive control)" test -n "$via_caddy"
check "the request from the web container reached the API log (positive control)" test -n "$direct"
check_blocked "a forged X-Forwarded-For sent through Caddy is not believed" grep -q "$FORGED" <<<"$via_caddy"
check_blocked "Caddy's own address is not what the API records for a client behind it" grep -q "172.28.31.254:" <<<"$via_caddy"
check_blocked "a forged X-Forwarded-For sent straight to the API is not believed" grep -q "$FORGED" <<<"$direct"
check "a direct caller is recorded under its own address" grep -q "$web_ip:" <<<"$direct"

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
# The API runs a round of checks at most every 2 seconds (F-10), so the new organization shows
# within that window, not on the very next call.
for _ in $(seq 1 10); do
  ready=$(readyz)
  grep -q '"status":"ok"' <<<"$ready" && break
  sleep 1
done
echo "      $ready"
check "readyz reports database, application role, redis, worker, migrations and organization all ok" grep -q '"status":"ok"' <<<"$ready"

echo "== signing in through the proxy"
ADMIN_PASSWORD=$(secret)
printf '%s\n' "$ADMIN_PASSWORD" |
  compose exec -T api python -m pharos.cli create-admin --username smoke-admin --password-stdin >/dev/null 2>&1
check "the operator command creates an administrator" test "$?" = 0
jar="$WORK/cookies"
login_json=$(printf '{"username":"smoke-admin","password":"%s"}' "$ADMIN_PASSWORD")
bad_json=$(printf '{"username":"smoke-admin","password":"%s-wrong"}' "$ADMIN_PASSWORD")
post_json() { # json, extra curl args...
  local body=$1
  shift
  https -H 'Content-Type: application/json' -d "$body" "$@" "$base/api/auth/local/login"
}
check "a sign-in without an Origin header is refused (403)" \
  test "$(post_json "$login_json" -o /dev/null -w '%{http_code}')" = 403
check "a sign-in from another origin is refused (403)" \
  test "$(post_json "$login_json" -H 'Origin: https://evil.example' -o /dev/null -w '%{http_code}')" = 403
check "a wrong password is refused (401)" \
  test "$(post_json "$bad_json" -H "Origin: $base" -o /dev/null -w '%{http_code}')" = 401
signed_in=$(post_json "$login_json" -H "Origin: $base" -c "$jar" -D "$WORK/login-headers" -w '\n%{http_code}')
check "the right password signs in (200)" test "$(tail -1 <<<"$signed_in")" = 200
cookie_line=$(grep -i '^set-cookie:' "$WORK/login-headers" | head -1)
for attribute in '__Host-pharos_session=' 'Secure' 'HttpOnly' 'SameSite=lax' 'Path=/'; do
  check "the session cookie has $attribute" grep -qi -- "$attribute" <<<"$cookie_line"
done
check_blocked "the session cookie has no Domain attribute" grep -qi 'domain=' <<<"$cookie_line"
check "the session works through the proxy" \
  test "$(https -b "$jar" -o /dev/null -w '%{http_code}' "$base/api/me")" = 200
# The worker holds its own Redis user (F-14): it can use its own keys, but a live session record,
# which exists right now, is out of reach. The API's user reads the same record (positive control).
redis_probe() { # service, database (sessions|default), command  (as that service's own Redis user)
  compose exec -T "$1" python -c "import redis
from pharos.core.config import get_settings
from pharos.core.redis import url_for_database
settings = get_settings()
database = settings.session_redis_db if '$2' == 'sessions' else 0
r = redis.Redis.from_url(url_for_database(settings.redis_url.get_secret_value(), database))
$3"
}
session_key=$(redis_probe api sessions "print(next(r.scan_iter('pharos:session:id:*'), b'').decode())" 2>/dev/null | tr -d '\r')
check "a live session record exists and the API's Redis user can read it (positive control)" \
  test -n "$session_key" -a "$(redis_probe api sessions "print(int(bool(r.get('$session_key'))))" 2>/dev/null | tr -d '\r')" = 1
check "the worker's Redis user can use its own keys (positive control)" \
  redis_probe worker default "r.set('pharos:worker:probe', '1', ex=30)"
check_blocked "the worker's Redis user cannot open the sessions' database" \
  redis_probe worker sessions "r.ping()"
# The same names in the default database, where the worker can connect (positive control above).
check "a session-like key is set by the API in the default database (positive control)" \
  redis_probe api default "r.set('pharos:session:id:probe', 'x', ex=30)"
check_blocked "the worker's Redis user cannot read a session-like key even where it can connect" \
  redis_probe worker default "r.get('pharos:session:id:probe')"
check_blocked "the worker's Redis user cannot write a session-like key" \
  redis_probe worker default "r.set('pharos:session:id:forged', '{}')"
csrf=$(head -n -1 <<<"$signed_in" | python3 -I -c "import json, sys; print(json.load(sys.stdin)['csrf_token'])")
professions=$(https -b "$jar" "$base/api/professions")
check "the seeded professions are listed (with all four languages)" \
  test "$(grep -o '"names"' <<<"$professions" | wc -l)" -ge 10
check "a profile change without the CSRF token is refused (403)" \
  test "$(https -b "$jar" -X PUT -H 'Content-Type: application/json' -H "Origin: $base" -d '{"locale":"az"}' -o /dev/null -w '%{http_code}' "$base/api/me/profile")" = 403
check "a profile change with the CSRF token works (200)" \
  test "$(https -b "$jar" -X PUT -H 'Content-Type: application/json' -H "Origin: $base" -H "X-CSRF-Token: $csrf" -d '{"locale":"az"}' -o /dev/null -w '%{http_code}' "$base/api/me/profile")" = 200
check "the saved language is reported back" \
  grep -q '"effective_locale":"az"' <<<"$(https -b "$jar" "$base/api/me/profile")"
check "a sign-out without the CSRF token is refused (403)" \
  test "$(https -b "$jar" -X POST -H "Origin: $base" -o /dev/null -w '%{http_code}' "$base/api/auth/logout")" = 403
check "a sign-out with the CSRF token works (204)" \
  test "$(https -b "$jar" -X POST -H "Origin: $base" -H "X-CSRF-Token: $csrf" -o /dev/null -w '%{http_code}' "$base/api/auth/logout")" = 204
check "the old cookie no longer works (401)" \
  test "$(https -b "$jar" -o /dev/null -w '%{http_code}' "$base/api/me")" = 401
all_logs=$(compose logs --no-log-prefix 2>&1)
check "the container logs record the sign-in requests (positive control)" \
  grep -q '/api/auth/local/login' <<<"$all_logs"
check_blocked "the password appears in no container log" grep -qF -- "$ADMIN_PASSWORD" <<<"$all_logs"

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
    api | worker | beat) compose exec -T "$1" python -c "import socket, sys; socket.create_connection((sys.argv[1], int(sys.argv[2])), timeout=5)" "$2" "$3" ;;
  esac
}
# The same tool on a plain Docker network, where the connection is allowed.
probe_on_plain_network() {
  case $1 in
    postgres) docker run --rm --entrypoint timeout "$(scripts/compose-image.sh postgres)" 5 bash -c "exec 3<>/dev/tcp/$2/$3" ;;
    redis | caddy) docker run --rm --entrypoint nc "$(scripts/compose-image.sh redis)" -w 5 -z "$2" "$3" ;;
    web) docker run --rm --entrypoint node "pharos-web:$TAG" -e "$NODE_PROBE" "$2" "$3" ;;
    worker | beat) docker run --rm --entrypoint python "pharos-backend:$TAG" -c "import socket, sys; socket.create_connection((sys.argv[1], int(sys.argv[2])), timeout=5)" "$2" "$3" ;;
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
blocked "beat cannot reach the internet (it only needs Redis)" beat 1.1.1.1 443
blocked "web cannot connect to postgres" web postgres 5432
blocked "web cannot connect to redis" web redis 6379
blocked "caddy cannot connect to postgres" caddy postgres 5432
blocked "caddy cannot connect to redis" caddy redis 6379
# Positive controls for the design: the services that must reach something do.
check "api reaches the internet through the egress network" probe_in_service api 1.1.1.1 443
check "worker reaches the internet through the egress network" probe_in_service worker 1.1.1.1 443
check "beat reaches redis" probe_in_service beat redis 6379
check "api reaches postgres" probe_in_service api postgres 5432
check "api reaches redis" probe_in_service api redis 6379
check "caddy reaches the API on the app network" probe_in_service caddy api 8000
check "caddy reaches the web app on the app network" probe_in_service caddy web 3000
check_blocked "postgres is not published on the host (no port mapping)" bash -c "docker port $(compose ps -q postgres) | grep -q ."
check_blocked "redis is not published on the host (no port mapping)" bash -c "docker port $(compose ps -q redis) | grep -q ."

echo "== database access"
# The password goes to the container through the environment (`-e NAME` takes the value from the
# caller's), never on a command line that `ps` would show (F-45).
sql() { PGPASSWORD="$2" compose exec -T -e PGPASSWORD postgres psql -h postgres -U "$1" -d pharos -tAc "$3"; }
check "the application role can log in over the network" test "$(sql pharos_app "$APP_PASSWORD" 'select 1' 2>&1)" = 1
check_blocked "the superuser cannot log in over the network, even with its password" sql postgres "$POSTGRES_PASSWORD" 'select 1'
check "the application role is neither superuser nor allowed to bypass row-level security" \
  test "$(sql pharos_app "$APP_PASSWORD" "select rolsuper or rolbypassrls from pg_roles where rolname = current_user" 2>&1)" = f
# The worker handles content from outside, so its role sees nothing about people or sign-in (ADR-0012).
check "the worker role can log in over the network" test "$(sql pharos_worker "$WORKER_DB_PASSWORD" 'select 1' 2>&1)" = 1
check "the worker role is neither superuser nor allowed to bypass row-level security" \
  test "$(sql pharos_worker "$WORKER_DB_PASSWORD" "select rolsuper or rolbypassrls or rolcreaterole or rolcreatedb from pg_roles where rolname = current_user" 2>&1)" = f
check "the application role can read users (positive control)" \
  test "$(sql pharos_app "$APP_PASSWORD" "select count(*) >= 0 from users" 2>&1)" = t
for table in local_credentials audit_logs user_profiles departments organizations; do
  check_blocked "the worker role cannot read $table" sql pharos_worker "$WORKER_DB_PASSWORD" "select 1 from $table limit 1"
done
# The one thing it may know about a person is the state of the account (migration 0004, ADR-0014).
check "the worker role can read the state of an account (positive control)" \
  test "$(sql pharos_worker "$WORKER_DB_PASSWORD" "select count(status) >= 0 from users" 2>&1)" = t
for column in email display_name role subject; do
  check_blocked "the worker role cannot read users.$column" sql pharos_worker "$WORKER_DB_PASSWORD" "select $column from users limit 1"
done
check_blocked "the worker role cannot change an account" sql pharos_worker "$WORKER_DB_PASSWORD" "update users set status = 'active'"
check_blocked "the worker role cannot create a table" sql pharos_worker "$WORKER_DB_PASSWORD" "create table worker_probe (id int)"
check_blocked "the worker role cannot log in with the application password" sql pharos_worker "$APP_PASSWORD" 'select 1'

echo "== hardening"
for service in api worker beat web postgres redis caddy; do
  user=$(compose exec -T "$service" id -u 2>/dev/null | tr -d '\r')
  if [ -n "$user" ] && [ "$user" != "0" ]; then
    printf 'PASS  %-9s runs as uid %s (not root)\n' "$service" "$user"
  else
    fail "$service runs as uid ${user:-unknown}"
  fi
done
# Caddy binds 80 and 443 as an ordinary user (F-08): no capability at all, not even NET_BIND_SERVICE.
check "caddy has no effective capability (F-08)" \
  test "$(compose exec -T caddy sh -c "grep '^CapEff:' /proc/1/status" 2>/dev/null | tr -d '\r' | awk '{print $2}')" = 0000000000000000
check "caddy's volumes were handed to its user before it started (caddy-volumes exited 0)" \
  test "$(docker inspect --format '{{.State.ExitCode}}' "$(compose ps -a -q caddy-volumes)")" = 0
check "caddy's certificate store belongs to its user" \
  test "$(compose exec -T caddy stat -c %u /data/caddy 2>/dev/null | tr -d '\r')" = 65532
for service in caddy web api worker beat migrate postgres redis; do
  container=$(compose ps -a -q "$service")
  settings=$(docker inspect --format '{{.HostConfig.ReadonlyRootfs}} {{.HostConfig.CapDrop}} {{.HostConfig.SecurityOpt}} {{.HostConfig.PidsLimit}}' "$container")
  case "$settings" in
    "true [ALL] [no-new-privileges:true] 256") printf 'PASS  %-9s read-only root, all capabilities dropped, no-new-privileges, 256 processes max\n' "$service" ;;
    *) fail "$service hardening is: $settings" ;;
  esac
done

echo "== backup and restore (ADR-0022)"
# The rehearsal of M5-02: the stack above is backed up, its volumes are destroyed, a new database is
# started and the dump is restored into it before anything else runs, as docs/DEPLOYMENT.md says.
SU() { compose exec -T postgres psql -q -U postgres -d pharos -tA "$@"; }
# What the restore must give back, as text: the schema version, the rows of every table, row-level
# security, the policies, and the exact grants of the two login roles (column by column).
snapshot() (
  # A subshell with -e: a query that fails stops the snapshot with a failure, so that a broken query
  # cannot hide behind "both sides are equal" (it did once: two queries failed on both sides).
  set -e
  SU -c "SELECT 'version ' || version_num FROM alembic_version"
  SU -c "SELECT 'rows ' || table_name || ' ' || (xpath('/row/c/text()', query_to_xml(format('select count(*) as c from %I.%I', table_schema, table_name), false, true, '')))[1]::text FROM information_schema.tables WHERE table_schema = 'public' AND table_type = 'BASE TABLE' ORDER BY 1"
  SU -c "SELECT 'rls ' || relname || ' ' || relrowsecurity || ' ' || relforcerowsecurity FROM pg_class WHERE relnamespace = 'public'::regnamespace AND relkind = 'r' ORDER BY 1"
  SU -c "SELECT 'policy ' || tablename || ' ' || policyname || ' ' || permissive || ' ' || cmd || ' ' || roles::text || ' ' || coalesce(qual, '') || ' ' || coalesce(with_check, '') FROM pg_policies ORDER BY 1"
  SU -c "SELECT 'grant ' || grantee || ' ' || table_name || ' ' || privilege_type FROM information_schema.role_table_grants WHERE grantee IN ('pharos_app', 'pharos_worker') AND table_schema = 'public' ORDER BY 1"
  SU -c "SELECT 'column ' || grantee || ' ' || table_name || ' ' || column_name || ' ' || privilege_type FROM information_schema.column_privileges WHERE grantee IN ('pharos_app', 'pharos_worker') AND table_schema = 'public' ORDER BY 1"
  # The parts of the tenant boundary that are not tables: functions (who owns them, whether they run
  # with the owner's rights, their settings and grants), triggers, owners and grants of every
  # relation, schemas, default privileges, settings on the database, and the roles themselves.
  SU -c "SELECT 'function ' || p.proname || '(' || pg_get_function_arguments(p.oid) || ') ' || md5(p.prosrc) || ' ' || pg_get_userbyid(p.proowner) || ' ' || p.prosecdef || ' ' || coalesce(p.proconfig::text, '') || ' ' || coalesce(p.proacl::text, '') FROM pg_proc p WHERE p.pronamespace = 'public'::regnamespace ORDER BY 1"
  SU -c "SELECT 'trigger ' || c.relname || ' ' || t.tgname || ' ' || pg_get_triggerdef(t.oid) FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid WHERE NOT t.tgisinternal AND c.relnamespace = 'public'::regnamespace ORDER BY 1"
  SU -c "SELECT 'relation ' || relname || ' ' || relkind::text || ' ' || pg_get_userbyid(relowner) || ' ' || coalesce(relacl::text, '') FROM pg_class WHERE relnamespace = 'public'::regnamespace AND relkind IN ('r', 'S', 'v', 'm') ORDER BY 1"
  SU -c "SELECT 'schema ' || nspname || ' ' || pg_get_userbyid(nspowner) || ' ' || coalesce(nspacl::text, '') FROM pg_namespace WHERE nspname NOT LIKE 'pg\_%' AND nspname <> 'information_schema' ORDER BY 1"
  SU -c "SELECT 'defacl ' || pg_get_userbyid(defaclrole) || ' ' || defaclobjtype::text || ' ' || defaclacl::text FROM pg_default_acl ORDER BY 1"
  SU -c "SELECT 'dbsettings ' || count(*) FROM pg_db_role_setting"
  SU -c "SELECT 'member ' || pg_get_userbyid(m.roleid) || ' ' || pg_get_userbyid(m.member) FROM pg_auth_members m JOIN pg_roles r ON r.oid = m.member WHERE r.rolname LIKE 'pharos\_%' ORDER BY 1"
  SU -c "SELECT 'role ' || rolname || ' ' || rolsuper || ' ' || rolbypassrls || ' ' || rolcreaterole || ' ' || rolcreatedb || ' ' || rolcanlogin FROM pg_roles WHERE rolname LIKE 'pharos\_%' ORDER BY 1"
)
wait_healthy() { # service...  (up to 5 minutes)
  local service pending
  for _ in $(seq 1 150); do
    pending=""
    for service in "$@"; do
      [ "$(health "$service")" = "healthy" ] || pending="$pending $service"
    done
    [ -z "$pending" ] && return 0
    sleep 2
  done
  echo "      not healthy:$pending"
  return 1
}

SU -c "INSERT INTO departments (org_id, name) SELECT id, 'Backup marker' FROM organizations WHERE slug = 'smoke'" >/dev/null
# An article the model has read, so that the Home feed has a card to show before and after.
# Published an hour ago: the feed is ranked at the start of the current five minutes (ADR-0027), so an
# article published just now would not show for up to five minutes.
SU -c "INSERT INTO items (org_id, canonical_url, url_hash, title, content_sha256, summary, category, quality, enriched_at, enrichment_version, published_at) SELECT id, 'https://news.example.org/backup-marker', encode(sha256('backup-marker-url'::bytea), 'hex'), 'Backup marker article', encode(sha256('backup-marker-content'::bytea), 'hex'), 'A summary of the marker article', 'news', 4, now(), 'v1', now() - interval '1 hour' FROM organizations WHERE slug = 'smoke'" >/dev/null
jar_before="$WORK/cookies-before-backup"
signed_in_before=$(post_json "$login_json" -H "Origin: $base" -c "$jar_before" -w '\n%{http_code}')
check "the administrator signs in again before the backup (200)" test "$(tail -1 <<<"$signed_in_before")" = 200
csrf_before=$(head -n -1 <<<"$signed_in_before" | python3 -I -c "import json, sys; print(json.load(sys.stdin)['csrf_token'])")
# The Home feed answers 409 until a person has chosen a profession (onboarding), so one is chosen.
profession_id=$(python3 -I -c "
import json, sys
data = json.load(sys.stdin)
data = data.get('items', data) if isinstance(data, dict) else data
print(next(p['id'] for p in data if p['code'] == 'accountant'))" <<<"$professions")
check "a profession is chosen for the administrator (200)" \
  test "$(https -b "$jar_before" -X PUT -H 'Content-Type: application/json' -H "Origin: $base" -H "X-CSRF-Token: $csrf_before" -d "{\"profession_id\":\"$profession_id\"}" -o /dev/null -w '%{http_code}' "$base/api/me/profile")" = 200
cards_before=$(https -b "$jar_before" "$base/api/cards")
check "the Home feed shows the marker article before the backup (positive control)" \
  grep -q 'Backup marker article' <<<"$cards_before"
# The writers are stopped, so that the snapshot before and the snapshot after can be equal.
compose stop api worker beat web caddy >/dev/null 2>&1
before=$(snapshot)
before_status=$?
check "the snapshot before the backup ran without a SQL error" test "$before_status" = 0
snapshot_is_meaningful() {
  grep -q '^rows departments [1-9]' <<<"$before" && grep -q '^rls .* true' <<<"$before" &&
    grep -q '^column pharos_worker ' <<<"$before" && grep -q '^function .* true ' <<<"$before" &&
    grep -q '^trigger ' <<<"$before" && grep -q '^role pharos_app false false false false true' <<<"$before" &&
    grep -q '^relation ' <<<"$before" && grep -q '^schema public ' <<<"$before"
}
check "the snapshot has rows, row-level security, the worker's column grants, a SECURITY DEFINER function, triggers and the roles (positive control)" \
  snapshot_is_meaningful

export PHAROS_COMPOSE="docker compose -p $PROJECT -f deploy/docker-compose.yml --env-file $ENV_FILE"
backups="$WORK/backups"
started=$SECONDS
check "backup.sh writes a dump" scripts/backup.sh run --dir "$backups"
backup_seconds=$((SECONDS - started))
dump=$(ls -1 "$backups"/daily/pharos-*.dump 2>/dev/null | head -1)
check "the dump is private (mode 600) and not empty" test -n "$dump" -a "$(stat -c %a "$dump" 2>/dev/null)" = 600 -a -s "$dump"
check "backup.sh env copies the environment file apart from the dumps" \
  env PHAROS_ENV_FILE="$ENV_FILE" scripts/backup.sh env --dir "$WORK/secrets" --dumps "$backups"
check_blocked "backup.sh env refuses the dump directory" \
  env PHAROS_ENV_FILE="$ENV_FILE" scripts/backup.sh env --dir "$backups" --dumps "$backups"

compose down --volumes --remove-orphans >/dev/null 2>&1
compose up -d postgres >/dev/null 2>&1
check "a new postgres volume is healthy" wait_healthy postgres
check "on the new volume the database is empty (control: the restore is not a no-op)" \
  test "$(SU -c "select count(*) from information_schema.tables where table_schema = 'public'" 2>&1)" = 0
check "on the new volume the init script made both roles" \
  test "$(SU -c "select count(*) from pg_roles where rolname in ('pharos_app', 'pharos_worker')" 2>&1)" = 2
check_blocked "restore.sh refuses a dump that has no SHA-256 file" scripts/restore.sh "$ENV_FILE"
# A file with a matching checksum that is still not a Pharos dump.
cp "$ENV_FILE" "$WORK/not-a-dump.dump"
(cd "$WORK" && sha256sum -- not-a-dump.dump >not-a-dump.dump.sha256)
check_blocked "restore.sh refuses a file that is not a dump, even with a matching checksum" \
  scripts/restore.sh "$WORK/not-a-dump.dump"
# A real dump changed after its checksum was made.
mkdir -p "$WORK/changed"
cp "$dump" "$dump.sha256" "$WORK/changed/"
printf 'x' >>"$WORK/changed/$(basename "$dump")"
check_blocked "restore.sh refuses a dump that was changed after its checksum was made" \
  scripts/restore.sh "$WORK/changed/$(basename "$dump")"
check "the checksum file of the real dump matches it (control for the two lines above)" \
  bash -c 'cd "$(dirname "$1")" && sha256sum --check --status -- "$(basename "$1").sha256"' _ "$dump"
check "nothing was restored by the refused attempts: the database is still empty" \
  test "$(SU -c "select count(*) from information_schema.tables where table_schema = 'public'" 2>&1)" = 0
started=$SECONDS
check "restore.sh restores the dump" scripts/restore.sh "$dump"
restore_seconds=$((SECONDS - started))
check_blocked "a second restore into the restored database is refused" scripts/restore.sh "$dump"
after=$(snapshot)
after_status=$?
check "the snapshot after the restore ran without a SQL error" test "$after_status" = 0
check "version, rows of every table, row-level security, policies and grants are identical after the restore" \
  test "$before" = "$after"
if [ "$before" != "$after" ]; then diff <(echo "$before") <(echo "$after") | head -20 | sed 's/^/      /'; fi
check "the marker row is back" test "$(SU -c "select count(*) from departments where name = 'Backup marker'" 2>&1)" = 1
check "restore.sh compared the restored database with the migrations and removed its scratch databases" \
  test "$(SU -c "select count(*) from pg_database where datname like 'pharos\_restore\_%'" 2>&1)" = 0

# F-44: a dump with one extra policy and one extra grant (what a hostile dump could add) is made
# from the restored database, which is then put back as it was.
SU -c "CREATE POLICY smoke_hostile ON users USING (true)" -c "GRANT SELECT ON users TO pharos_worker" >/dev/null
check "backup.sh writes the changed dump" scripts/backup.sh run --dir "$WORK/hostile-backups"
hostile=$(ls -1 "$WORK"/hostile-backups/daily/pharos-*.dump 2>/dev/null | head -1)
compose down --volumes --remove-orphans >/dev/null 2>&1
compose up -d postgres >/dev/null 2>&1
check "a new postgres volume for the changed dump is healthy" wait_healthy postgres
refusal=$(scripts/restore.sh "$hostile" 2>&1)
refused=$?
check "restore.sh refuses the dump with an extra policy and grant (exit 1)" test "$refused" = 1
check "and names the extra policy" grep -q 'only in the dump: *policy|public.users.smoke_hostile' <<<"$refusal"
check "and names the changed grants of the table" grep -q 'only in the dump: *acl|relation|public.users' <<<"$refusal"
check "and removed its scratch databases" \
  test "$(SU -c "select count(*) from pg_database where datname like 'pharos\_restore\_%'" 2>&1)" = 0

# The attacks of the F-44 review. (A) A schema named after the owner (first in its default
# search_path) with an operator that hides the schema from any check that resolves names there, and
# a view over every person in it.
OWNER() { compose exec -T postgres psql -q -v ON_ERROR_STOP=1 -U pharos_owner -d pharos "$@"; }
fresh_volume() {
  compose down --volumes --remove-orphans >/dev/null 2>&1
  compose up -d postgres >/dev/null 2>&1
  wait_healthy postgres
}
fresh_volume >/dev/null
check "the good dump is restored to make the next changed dump" scripts/restore.sh "$dump"
OWNER >/dev/null <<'SQL'
CREATE SCHEMA pharos_owner;
CREATE FUNCTION pharos_owner.hide(name, name) RETURNS boolean LANGUAGE sql IMMUTABLE AS
  $$ SELECT CASE WHEN $1 IN ('pharos_owner') THEN false ELSE NOT pg_catalog.textlike($1::text, $2::text) END $$;
CREATE OPERATOR pharos_owner.!~~ (LEFTARG = name, RIGHTARG = name, FUNCTION = pharos_owner.hide);
CREATE VIEW pharos_owner.all_users AS SELECT * FROM public.users;
SQL
check "backup.sh writes the dump with the hiding schema" scripts/backup.sh run --dir "$WORK/hidden-backups"
hidden=$(ls -1 "$WORK"/hidden-backups/daily/pharos-*.dump 2>/dev/null | head -1)
check "a new postgres volume for the hiding dump is healthy" fresh_volume
refusal=$(scripts/restore.sh "$hidden" 2>&1)
refused=$?
check "restore.sh refuses the dump whose schema hides itself from a search_path check (exit 1)" test "$refused" = 1
check "and says why: a schema a Pharos dump never makes" grep -q 'made schemas that a Pharos dump never makes' <<<"$refusal"

# (B) The owner makes a trusted extension of its own (a hand-made dump could do it in any entry);
# objects in an extension are not compared, so the extension itself must be refused.
check "a new postgres volume for the extension case is healthy" fresh_volume
OWNER -c "CREATE EXTENSION pgcrypto" >/dev/null
refusal=$(scripts/restore.sh "$dump" 2>&1)
refused=$?
check "restore.sh refuses a database with an extension the owner made (exit 1)" test "$refused" = 1
check "and says why: an extension Pharos does not make" grep -q 'an extension that Pharos does not make' <<<"$refusal"

# (C) Grants on the database itself (pg_dump writes none, a hand-made dump could): CREATE would let
# the application role make a schema that shadows the worker's tables.
check "a new postgres volume for the database grant case is healthy" fresh_volume
SU -c "GRANT CREATE, TEMP ON DATABASE pharos TO pharos_app" >/dev/null
refusal=$(scripts/restore.sh "$dump" 2>&1)
refused=$?
check "restore.sh refuses a database with extra database grants (exit 1)" test "$refused" = 1
check "and names the database's grants" grep -q 'only in the dump: *acl|database|current' <<<"$refusal"

# (D) From the second review: an UNLOGGED audit log is emptied by PostgreSQL after any crash, so a
# dump that makes it so must be refused although no grant or policy changed.
check "a new postgres volume for the unlogged case is healthy" fresh_volume
check "the good dump is restored to make the unlogged dump" scripts/restore.sh "$dump"
OWNER -c "ALTER TABLE public.audit_logs SET UNLOGGED" >/dev/null
check "backup.sh writes the dump with an unlogged audit log" scripts/backup.sh run --dir "$WORK/unlogged-backups"
unlogged=$(ls -1 "$WORK"/unlogged-backups/daily/pharos-*.dump 2>/dev/null | head -1)
check "a new postgres volume for the unlogged dump is healthy" fresh_volume
refusal=$(scripts/restore.sh "$unlogged" 2>&1)
refused=$?
check "restore.sh refuses the dump whose audit log is unlogged (exit 1)" test "$refused" = 1
check "and names the audit log" grep -q 'only in the dump: *relation|public.audit_logs' <<<"$refusal"

# (E) A real dump cut short (F-45; until now shown only with a stand-in for Docker). Its checksum
# file is made anew, as by someone who copied the broken file with its checksum, so the hash check
# passes and pg_restore itself must stop it. Cut in half, the table of contents itself may be broken
# (it is large next to the smoke test's few rows), so any of the two refusals will do; with only the
# last bytes missing (a full disk, an interrupted copy) the contents are whole and the restore stops
# partway. A failed restore may have written part of the dump, so the volume is thrown away
# afterwards, as the message says.
mkdir -p "$WORK/truncated"
size=$(stat -c %s "$dump")
cut_dump() { # name bytes
  head -c "$2" "$dump" >"$WORK/truncated/$1"
  (cd "$WORK/truncated" && sha256sum "$1" >"$1.sha256")
}
cut_dump pharos-half.dump $((size / 2))
cut_dump pharos-tail.dump $((size - 64))
check "a new postgres volume for the dump cut in half is healthy" fresh_volume
refusal=$(scripts/restore.sh "$WORK/truncated/pharos-half.dump" 2>&1)
refused=$?
echo "      $(tail -n 1 <<<"$refusal" | cut -c1-160)"
check "restore.sh refuses a real dump cut in half (exit 1)" test "$refused" = 1
check "and says it is not a whole dump, or that the restore failed" \
  grep -qE 'not a dump pg_restore understands|pg_restore failed' <<<"$refusal"
check "a new postgres volume for the dump missing its last bytes is healthy" fresh_volume
refusal=$(scripts/restore.sh "$WORK/truncated/pharos-tail.dump" 2>&1)
refused=$?
echo "      $(tail -n 1 <<<"$refusal" | cut -c1-160)"
check "restore.sh refuses a real dump missing its last 64 bytes (exit 1)" test "$refused" = 1
check "and says the database may be partly changed and the volume must go" \
  grep -q 'may be partly changed.*remove the postgres volume' <<<"$refusal"

check "a last postgres volume is healthy" fresh_volume
check "the good dump is restored again for the rest of these checks" scripts/restore.sh "$dump"

compose up -d >/dev/null 2>&1
check "every service is healthy again on the restored database" wait_healthy postgres redis web api worker caddy
check "migrate finds the restored schema at head and finishes successfully" \
  test "$(docker inspect --format '{{.State.ExitCode}}' "$(compose ps -a -q migrate)")" = 0
ready=""
for _ in $(seq 1 30); do
  ready=$(readyz)
  grep -q '"status":"ok"' <<<"$ready" && break
  sleep 2
done
echo "      $ready"
check "readyz is ok on the restored installation" grep -q '"status":"ok"' <<<"$ready"
# The operator's one look (scripts/status.sh, M5-11 and F-59) on the real stack: its probe runs in
# the API container, so only here is its heartbeat line read from the real Redis.
status_out=$(scripts/status.sh --backups "$backups" 2>&1)
status_rc=$?
echo "$status_out" | sed 's/^/      /'
check "status.sh passes on the restored installation (exit 0)" test "$status_rc" = 0
check "status.sh shows a recent worker heartbeat in seconds" \
  grep -qE '^PASS +worker +last heartbeat [0-9]+ s ago$' <<<"$status_out"
check "status.sh shows the size of the newest dump" \
  grep -qE '^PASS +backup +newest dump is [0-9]+ hours old, [0-9]+ KiB$' <<<"$status_out"
redis_api_password=$(sed -n 's/^PHAROS_REDIS_PASSWORD=//p' "$ENV_FILE")
check "the Redis password the probe uses was found (control for the next check)" test -n "$redis_api_password"
check_blocked "status.sh prints no password of this installation" \
  grep -qF -e "$APP_PASSWORD" -e "$POSTGRES_PASSWORD" -e "$redis_api_password" <<<"$status_out"
check "the administrator signs in with the same password (200)" \
  test "$(post_json "$login_json" -H "Origin: $base" -c "$jar" -o /dev/null -w '%{http_code}')" = 200
check "the new session works (200)" test "$(https -b "$jar" -o /dev/null -w '%{http_code}' "$base/api/me")" = 200
# Redis is not part of a backup: the sessions that existed before are gone, and signing in again works.
check "the session from before the backup is gone: sessions live in Redis, which is not backed up (401)" \
  test "$(https -b "$jar_before" -o /dev/null -w '%{http_code}' "$base/api/me")" = 401
cards_after=$(https -b "$jar" "$base/api/cards")
check "the Home feed shows the marker article on the restored database" \
  grep -q 'Backup marker article' <<<"$cards_after"
check "the application role works on the restored database (positive control)" \
  test "$(sql pharos_app "$APP_PASSWORD" "select count(*) >= 0 from users" 2>&1)" = t
check "row-level security still filters: the application role sees no user without an organization" \
  test "$(sql pharos_app "$APP_PASSWORD" "select count(*) from users" 2>&1)" = 0
check_blocked "the worker role still cannot read local_credentials" \
  sql pharos_worker "$WORKER_DB_PASSWORD" "select 1 from local_credentials limit 1"
check "the worker role can still read the state of an account (positive control)" \
  test "$(sql pharos_worker "$WORKER_DB_PASSWORD" "select count(status) >= 0 from users" 2>&1)" = t
check_blocked "the worker role still cannot read users.email" \
  sql pharos_worker "$WORKER_DB_PASSWORD" "select email from users limit 1"
printf 'NOTE  backup took %ss, restore took %ss (a small database: not a promise for a large one)\n' \
  "$backup_seconds" "$restore_seconds"

echo
if [ "$failures" -eq 0 ]; then
  echo "All checks passed."
else
  echo "$failures check(s) FAILED."
  exit 1
fi
