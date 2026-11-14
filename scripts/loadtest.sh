#!/usr/bin/env bash
# The Home feed under load on a THROWAWAY copy of the real stack (M5-08, decision D5).
#
# Builds the stack (own project name, volumes, image tag, random passwords, high ports), fills it
# with synthetic data (30,000 articles and 300 people by default), opens one session per person
# and runs k6 (a digest-pinned container) against /api/cards through Caddy over HTTPS. The result
# is a report with the numbers AND the hardware they were measured on; a number without its
# hardware means nothing, and a run on a shared development machine is not a promise for a server.
#
# Usage: make loadtest            (needs Docker; the run takes about 15 minutes)
#   LOAD_STEPS=50 LOAD_DURATION=10m LOAD_THINK=30 LOAD_ARTICLES=30000 LOAD_PEOPLE=300 make loadtest
#   LOAD_STEPS=50,100,200,300 LOAD_DURATION=3m make loadtest   (the stepped run: where does p95 cross?)
#   LOAD_SCENARIO=insights make loadtest   (the insight burst: every person opens "Try this" at about
#     the same time; the worker, with the stack's fake provider, makes the insights)
#   LOAD_SCENARIO=sso make loadtest   (the single sign-on burst: LOAD_BURST people, 100 by default, sign in
#     through the development Keycloak within 100 s from this one address; takes about 5 minutes)
#   LOADTEST_OUT=dir  where the report and the k6 summary are written (default: loadtest-results)
set -uo pipefail
cd "$(dirname "$0")/.."

# k6 v2.3.0, pinned by digest (checked 2026-11-07). Dependabot does not see this line; move it with
# a deliberate change.
K6_IMAGE=grafana/k6@sha256:3e4186aea62e11a45560bfc51e19eeafe82fc07ce069c78aff44b7ff893d9a5e

PROJECT=pharos-load
TAG=loadtest
HTTP_PORT=${PHAROS_LOAD_HTTP_PORT:-19280}
HTTPS_PORT=${PHAROS_LOAD_HTTPS_PORT:-19643}
# The generated env file is the only source of settings (scripts/lib/throwaway-env.sh).
source scripts/lib/throwaway-env.sh
drop_installation_env PHAROS_LOAD_HTTP_PORT PHAROS_LOAD_HTTPS_PORT
PUBLIC="https://localhost:$HTTPS_PORT"
ORG_SLUG=load
STEPS=${LOAD_STEPS:-50}   # people at work at the same time, one run of k6 for each, the first is the D5 target
DURATION=${LOAD_DURATION:-10m}
THINK=${LOAD_THINK:-30}
ARTICLES=${LOAD_ARTICLES:-30000}
PEOPLE=${LOAD_PEOPLE:-300}
OUT=${LOADTEST_OUT:-loadtest-results}
SCENARIO=${LOAD_SCENARIO:-feed}
BURST=${LOAD_BURST:-100}   # people in a burst: who open "Try this" together (insights, at most LOAD_PEOPLE) or sign in (sso)
RAMP=${LOAD_RAMP:-100}     # seconds over which the sign-ins of the sso burst start (the verdict wants all done within 120)
case "$SCENARIO" in feed | insights | sso) ;; *) echo "LOAD_SCENARIO is feed, insights or sso"; exit 2 ;; esac
KEYCLOAK="https://auth.localhost:$HTTPS_PORT"
SSO_PASSWORD=$(openssl rand -hex 16)   # one throwaway password for the synthetic Keycloak users of this run
WORK=$(mktemp -d)
ENV_FILE="$WORK/env"
# k6 runs as the user who runs this script (`--user` below), so $WORK and every file in it stay
# private to that user: the sessions are valid sign-ins for the stack on 127.0.0.1, which every
# local user can reach, and the summaries decide the verdict (M5-13 review).
chmod 700 "$WORK"
mkdir -p "$OUT"

secret() { openssl rand -hex 24; }
# The development Keycloak's administrator, random for this run (F-54).
KEYCLOAK_ADMIN_PASSWORD=$(secret)
umask 077
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
PHAROS_KEYCLOAK_ADMIN_PASSWORD=$KEYCLOAK_ADMIN_PASSWORD
PHAROS_BIND_ADDRESS=127.0.0.1
PHAROS_APP_SUBNET=172.28.34.0/24
PHAROS_APP_DYNAMIC_RANGE=172.28.34.0/25
PHAROS_CADDY_IP=172.28.34.254
PHAROS_ENVIRONMENT=production
PHAROS_MIGRATE_ON_START=true
PHAROS_IMAGE_TAG=$TAG
PHAROS_PUBLIC_URL=$PUBLIC
PHAROS_DEFAULT_LOCALE=en
PHAROS_ORG_SLUG=$ORG_SLUG
PHAROS_LOCAL_LOGIN=break-glass
EOF
if [ "$SCENARIO" = sso ]; then
  cat >> "$ENV_FILE" <<EOF
PHAROS_KEYCLOAK_PUBLIC_URL=$KEYCLOAK
PHAROS_OIDC_ISSUER=$KEYCLOAK/realms/pharos
PHAROS_OIDC_CLIENT_ID=pharos
PHAROS_OIDC_CLIENT_SECRET=dev-only-client-secret
PHAROS_OIDC_PROVIDER_NAME=Keycloak (development)
EOF
fi
if [ "$SCENARIO" = insights ]; then
  cat >> "$ENV_FILE" <<EOF
PHAROS_LLM_PROVIDER=anthropic
PHAROS_LLM_MODEL_FILTER=stack-filter
PHAROS_LLM_MODEL_ENRICHMENT=stack-enrichment
PHAROS_LLM_MODEL_INSIGHT=stack-insight
# The burst measures how fast the calls are let through, not the day's budget (the default of two
# dollars covers only a few insights, see the pilot runbook; the stop at the limit is tested in the
# stack tests).
PHAROS_LLM_DAILY_BUDGET_MICRO_USD=1000000000
EOF
  # The worker needs the test provider (no model, no key, no cost): the test-only overlay of the
  # stack tests gives it, with one fake feed host that nothing here uses.
  export STACK_FEED_DIR="$WORK/feeds"
  mkdir "$STACK_FEED_DIR" && chmod 755 "$STACK_FEED_DIR"
fi

overlay=()
[ "$SCENARIO" = insights ] && overlay=(-f deploy/docker-compose.stack-e2e.yml)
[ "$SCENARIO" = sso ] && overlay=(-f deploy/docker-compose.sso-dev.yml)
compose() { docker compose -p "$PROJECT" -f deploy/docker-compose.yml "${overlay[@]}" --env-file "$ENV_FILE" "$@"; }
cleanup() {
  if [ "${LOAD_KEEP:-}" = 1 ]; then echo "kept: project $PROJECT, env $ENV_FILE (remove by hand)"; return; fi
  compose down --volumes --remove-orphans >/dev/null 2>&1
  docker rmi "pharos-web:$TAG" "pharos-backend:$TAG" >/dev/null 2>&1
  rm -r "$WORK"
}
trap cleanup EXIT

compose down --volumes --remove-orphans >/dev/null 2>&1
echo "== build and start the stack"
if ! compose up -d --build 2>&1 | tail -10; then echo "FAIL  docker compose up"; exit 1; fi

health() { docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$(compose ps -q "$1")" 2>/dev/null; }
extra_services=""
[ "$SCENARIO" = sso ] && extra_services=keycloak
for _ in $(seq 1 180); do
  pending=""
  for service in postgres redis web api worker caddy $extra_services; do
    [ "$(health "$service")" = "healthy" ] || pending="$pending $service"
  done
  [ -z "$pending" ] && break
  sleep 2
done
if [ -n "$pending" ]; then
  echo "FAIL  not healthy after 6 minutes:$pending"
  for service in $pending; do compose logs --tail 20 "$service" 2>&1; done
  exit 1
fi

psql_cmd() { compose exec -T postgres psql -q -tA -v ON_ERROR_STOP=1 -U postgres -d pharos "$@"; }

if [ "$SCENARIO" = sso ]; then
  echo "== organization and $BURST synthetic Keycloak users"
  compose run --rm migrate python -m pharos.cli create-org --slug "$ORG_SLUG" --name "Load test" || exit 1
  kc() { curl -sk --max-time 20 --resolve "auth.localhost:$HTTPS_PORT:127.0.0.1" "$@"; }
  # The password goes to curl from a private file, never on its command line.
  printf 'password=%s' "$KEYCLOAK_ADMIN_PASSWORD" >"$WORK/kc-admin"
  admin_token() {
    kc -d grant_type=password -d client_id=admin-cli -d username=admin -d @"$WORK/kc-admin" \
      "$KEYCLOAK/realms/master/protocol/openid-connect/token" |
      python3 -I -c "import json, sys; print(json.load(sys.stdin).get('access_token', ''))"
  }
  token=""
  for number in $(seq 1 "$BURST"); do
    # The administrator's token lives one minute; ask for a new one every 20 users.
    if [ -z "$token" ] || [ $(( number % 20 )) -eq 1 ]; then token=$(admin_token); fi
    id=$(printf '%03d' "$number")
    # The token and the password go through private files (mode 600), not onto a command line.
    printf 'Authorization: Bearer %s\n' "$token" > "$WORK/admin.hdr"
    printf '{"username":"load-%s","email":"load-%s@example.test","emailVerified":true,"enabled":true,"firstName":"Load","lastName":"User %s","credentials":[{"type":"password","value":"%s","temporary":false}]}' \
      "$id" "$id" "$id" "$SSO_PASSWORD" > "$WORK/user.json"
    code=$(kc -o /dev/null -w '%{http_code}' -H @"$WORK/admin.hdr" -H 'Content-Type: application/json' \
      -d @"$WORK/user.json" "$KEYCLOAK/admin/realms/pharos/users")
    if [ "$code" != 201 ]; then echo "FAIL  Keycloak user load-$id: HTTP $code"; exit 1; fi
  done
  echo "users in Keycloak: $BURST"
  cp scripts/loadtest/sso.js "$WORK/sso.js" && chmod 600 "$WORK/sso.js"
  # Mode 600, read by the docker CLI (`--env-file`), so the password is not in `docker run`'s arguments.
  printf 'SSO_PASSWORD=%s\n' "$SSO_PASSWORD" > "$WORK/k6.env"
  # Positive control: one person (not part of the burst) signs in with the script's own steps, so the
  # run below measures sign-ins and not a wall of refusals. Done with curl, as a browser would.
  jar="$WORK/probe-jar"
  browser() { curl -sk --max-time 20 --resolve "localhost:$HTTPS_PORT:127.0.0.1" --resolve "auth.localhost:$HTTPS_PORT:127.0.0.1" -b "$jar" -c "$jar" "$@"; }
  auth_url=$(browser -D - -o /dev/null "$PUBLIC/api/auth/oidc/login?next=/feed" | grep -i '^location:' | tr -d '\r' | cut -d' ' -f2-)
  form_action=$(browser -L "$auth_url" | python3 -I -c "import html, re, sys; m = re.search(r'<form[^>]*action=\"([^\"]+)\"', sys.stdin.read()); print(html.unescape(m.group(1)) if m else '')")
  printf 'username=load-001&password=%s&credentialId=' "$SSO_PASSWORD" > "$WORK/probe.form"
  callback_url=$(browser -D - -o /dev/null -d @"$WORK/probe.form" "$form_action" | grep -i '^location:' | tr -d '\r' | cut -d' ' -f2-)
  browser -o /dev/null "$callback_url"
  probe=$(browser "$PUBLIC/api/me")
  echo "probe: $(python3 -I -c "import json, sys; print(json.load(sys.stdin).get('user', {}).get('display_name', ''))" <<<"$probe")"
  if ! grep -q '"display_name":"Load User 001"' <<<"$probe"; then echo "FAIL  the probe sign-in"; exit 1; fi
  # The probe signed load-001 in once already; the burst signs the same person in again, which is
  # an ordinary second sign-in (the user row exists, a new session is made).
else
echo "== organization and synthetic data ($ARTICLES articles, $PEOPLE people)"
compose run --rm migrate python -m pharos.cli create-org --slug "$ORG_SLUG" --name "Load test" || exit 1
started=$(date +%s)
psql_cmd -v org_slug="$ORG_SLUG" -v articles="$ARTICLES" -v people="$PEOPLE" < scripts/loadtest/seed.sql || { echo "FAIL  seed"; exit 1; }
echo "seeded in $(( $(date +%s) - started )) s"
org_id=$(psql_cmd -c "SELECT id FROM organizations WHERE slug = '$ORG_SLUG'")
{ echo "$org_id"; psql_cmd -c "SELECT id FROM users WHERE subject LIKE 'load-%' ORDER BY subject"; } > "$WORK/ids.txt"
compose exec -T api python -c "$(cat scripts/loadtest/sessions.py)" < "$WORK/ids.txt" > "$WORK/sessions.txt" || { echo "FAIL  sessions"; exit 1; }
chmod 600 "$WORK/sessions.txt"
cp scripts/loadtest/feed.js "$WORK/feed.js" && chmod 600 "$WORK/feed.js"
cp scripts/loadtest/insights.js "$WORK/insights.js" && chmod 600 "$WORK/insights.js"
# One article for each person (the newest ones): the burst asks for $BURST different insights.
psql_cmd -c "SELECT id FROM items WHERE title LIKE 'Load %' ORDER BY published_at DESC, id LIMIT $BURST" > "$WORK/items.txt"
chmod 600 "$WORK/items.txt"
echo "sessions: $(wc -l < "$WORK/sessions.txt") (people $(( $(wc -l < "$WORK/ids.txt") - 1 )))"
if [ "$(wc -l < "$WORK/sessions.txt")" -ne "$PEOPLE" ]; then echo "FAIL  expected $PEOPLE sessions"; exit 1; fi

# Positive control: a session of the file is accepted and the feed answers with cards, so the run
# below measures real work and not a wall of 401s.
first_session=$(head -n 1 "$WORK/sessions.txt")
status=$(curl -sk -o "$WORK/probe.json" -w '%{http_code}' --max-time 20 \
  -H "Cookie: __Host-pharos_session=$first_session" "$PUBLIC/api/cards?limit=20")
cards=$(grep -o '"id"' "$WORK/probe.json" | wc -l)
echo "probe: HTTP $status, $cards cards on the first page"
if [ "$status" != "200" ] || [ "$cards" -lt 1 ]; then echo "FAIL  the probe of the feed"; exit 1; fi
fi

( while sleep 30; do
    docker stats --no-stream --format '{{.Name}} {{.CPUPerc}} {{.MemUsage}}' \
      $(compose ps -q) >> "$WORK/stats.txt" 2>/dev/null
  done ) &
sampler=$!
k6_status=0
summaries=()
[ "$SCENARIO" = insights ] && STEPS=$BURST
[ "$SCENARIO" = sso ] && STEPS=$BURST
script=feed.js
[ "$SCENARIO" = insights ] && script=insights.js
sso_env_file=()
if [ "$SCENARIO" = sso ]; then script=sso.js; sso_env_file=(--env-file "$WORK/k6.env"); fi
for people in ${STEPS//,/ }; do
  echo "== k6 ($SCENARIO): $people people"
  touch "$WORK/summary-$people.json" && chmod 600 "$WORK/summary-$people.json"
  docker run --rm --network host --user "$(id -u):$(id -g)" -v "$WORK:/work" \
    ${sso_env_file[@]+"${sso_env_file[@]}"} \
    -e BASE_URL="$PUBLIC" -e COOKIE_NAME=__Host-pharos_session \
    -e VUS="$people" -e DURATION="$DURATION" -e THINK="$THINK" -e RAMP="$RAMP" \
    "$K6_IMAGE" run --quiet --summary-export="/work/summary-$people.json" "/work/$script" 2>&1 | tee -a "$WORK/k6.txt"
  [ "${PIPESTATUS[0]}" -eq 0 ] || k6_status=1
  cp "$WORK/summary-$people.json" "$OUT/summary-$people.json"
  summaries+=("$OUT/summary-$people.json")
done
kill "$sampler" 2>/dev/null
# The sign-ins must have made exactly one user each: the report is told how many people to expect, and
# the database has to agree.
db_users=""
if [ "$SCENARIO" = sso ]; then
  db_users=$(psql_cmd -c "SELECT count(*) FROM users WHERE issuer <> 'local' AND email LIKE 'load-%@example.test'")
fi

{
  if [ "$SCENARIO" = sso ]; then
    echo "Load test, the single sign-on burst ($(date -u +%Y-%m-%dT%H:%MZ))"
    echo "  data:       $BURST synthetic Keycloak users (development Keycloak, start-dev mode); no articles"
    echo "  load:       $BURST people sign in, starts spread over $RAMP s, all from this one address, through Caddy over HTTPS; k6 $K6_IMAGE"
  elif [ "$SCENARIO" = insights ]; then
    echo "Load test, the insight burst ($(date -u +%Y-%m-%dT%H:%MZ))"
    echo "  data:       $ARTICLES synthetic articles, $PEOPLE synthetic people, $BURST of them each ask for the insight of another article"
    echo "  load:       $BURST people open \"Try this\" within 20 s and look again every 3 s until it is ready; the stack's test provider makes the insights; k6 $K6_IMAGE"
  else
    echo "Load test of the Home feed ($(date -u +%Y-%m-%dT%H:%MZ))"
    echo "  data:       $ARTICLES synthetic articles, $PEOPLE synthetic people, six in ten articles in clusters of about three"
    echo "  load:       $STEPS people at work (one run each) for $DURATION, a visit about every $THINK s, k6 $K6_IMAGE"
  fi
  echo "  hardware:   $(nproc) CPU threads, $(lscpu | sed -n 's/^Model name: *//p' | head -n 1)"
  echo "              $(free -m | awk '/^Mem:/{print $2" MB RAM"}'), load average at the start of the report: $(cut -d' ' -f1-3 /proc/loadavg)"
  echo "              Docker $(docker version --format '{{.Server.Version}}' 2>/dev/null), $(uname -sr)"
  echo "  other work: $(docker ps -q | wc -l) containers running on this host (the stack's own included)"
  echo "  results:"
  if [ "$SCENARIO" = sso ]; then
    python3 scripts/loadtest/report_sso.py --expected "$BURST" "${summaries[@]}"
    echo "  users made by the sign-ins in the database: $db_users, expected $BURST: $([ "$db_users" = "$BURST" ] && echo PASS || echo FAIL)"
  elif [ "$SCENARIO" = insights ]; then
    python3 scripts/loadtest/report_insights.py "${summaries[@]}"
    # The control: what was answered "ready" is in the database, made by the worker.
    echo "  insights stored in the database: $(psql_cmd -c "SELECT count(*) FROM item_insights WHERE status = 'ok'")"
  else
    python3 scripts/loadtest/report.py "${summaries[@]}"
  fi
  if [ -s "$WORK/stats.txt" ]; then
    echo "  peak CPU % per container (samples every 30 s):"
    awk '{gsub(/%/,"",$2); if ($2+0 > m[$1]) m[$1]=$2+0} END{for (k in m) printf "    %-40s %6.1f\n", k, m[k]}' "$WORK/stats.txt" | sort
  fi
} | tee "$OUT/report.txt"
echo "report: $OUT/report.txt  k6 summaries: $OUT/summary-*.json"
# Both must hold: k6 ran without an error of its own, and every number is inside its limit.
grep -qE "verdict on the (D5 feed targets|insight burst[^:]*|single sign-on burst[^:]*): PASS" "$OUT/report.txt" && [ "$k6_status" -eq 0 ] &&
  { [ "$SCENARIO" != sso ] || [ "$db_users" = "$BURST" ]; }
