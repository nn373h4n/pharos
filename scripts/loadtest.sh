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
PUBLIC="https://localhost:$HTTPS_PORT"
ORG_SLUG=load
STEPS=${LOAD_STEPS:-50}   # people at work at the same time, one run of k6 for each, the first is the D5 target
DURATION=${LOAD_DURATION:-10m}
THINK=${LOAD_THINK:-30}
ARTICLES=${LOAD_ARTICLES:-30000}
PEOPLE=${LOAD_PEOPLE:-300}
OUT=${LOADTEST_OUT:-loadtest-results}
SCENARIO=${LOAD_SCENARIO:-feed}
BURST=${LOAD_BURST:-100}   # people who open "Try this" together in the insight burst (at most LOAD_PEOPLE)
case "$SCENARIO" in feed | insights) ;; *) echo "LOAD_SCENARIO is feed or insights"; exit 2 ;; esac
WORK=$(mktemp -d)
ENV_FILE="$WORK/env"
# The k6 container runs as an unprivileged user: it reads the files in $WORK (the env file with the
# passwords stays private, mode 600) and may write only the summary files made for it, below.
chmod 755 "$WORK"
mkdir -p "$OUT"

secret() { openssl rand -hex 24; }
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
if [ "$SCENARIO" = insights ]; then
  cat >> "$ENV_FILE" <<EOF
PHAROS_LLM_PROVIDER=anthropic
PHAROS_LLM_MODEL_FILTER=stack-filter
PHAROS_LLM_MODEL_ENRICHMENT=stack-enrichment
PHAROS_LLM_MODEL_INSIGHT=stack-insight
# The burst measures how fast the calls are let through, not the day's budget (which, at the default
# of two dollars, covers about 62 insights; the stop at the limit is tested in the stack tests).
PHAROS_LLM_DAILY_BUDGET_MICRO_USD=1000000000
EOF
  # The worker needs the test provider (no model, no key, no cost): the test-only overlay of the
  # stack tests gives it, with one fake feed host that nothing here uses.
  export STACK_FEED_DIR="$WORK/feeds"
  mkdir "$STACK_FEED_DIR" && chmod 755 "$STACK_FEED_DIR"
fi

overlay=()
[ "$SCENARIO" = insights ] && overlay=(-f deploy/docker-compose.stack-e2e.yml)
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
for _ in $(seq 1 120); do
  pending=""
  for service in postgres redis web api worker caddy; do
    [ "$(health "$service")" = "healthy" ] || pending="$pending $service"
  done
  [ -z "$pending" ] && break
  sleep 2
done
if [ -n "$pending" ]; then
  echo "FAIL  not healthy after 4 minutes:$pending"
  for service in $pending; do compose logs --tail 20 "$service" 2>&1; done
  exit 1
fi

psql_cmd() { compose exec -T postgres psql -q -tA -v ON_ERROR_STOP=1 -U postgres -d pharos "$@"; }

echo "== organization and synthetic data ($ARTICLES articles, $PEOPLE people)"
compose run --rm migrate python -m pharos.cli create-org --slug "$ORG_SLUG" --name "Load test" || exit 1
started=$(date +%s)
psql_cmd -v org_slug="$ORG_SLUG" -v articles="$ARTICLES" -v people="$PEOPLE" < scripts/loadtest/seed.sql || { echo "FAIL  seed"; exit 1; }
echo "seeded in $(( $(date +%s) - started )) s"
org_id=$(psql_cmd -c "SELECT id FROM organizations WHERE slug = '$ORG_SLUG'")
{ echo "$org_id"; psql_cmd -c "SELECT id FROM users WHERE subject LIKE 'load-%' ORDER BY subject"; } > "$WORK/ids.txt"
compose exec -T api python -c "$(cat scripts/loadtest/sessions.py)" < "$WORK/ids.txt" > "$WORK/sessions.txt" || { echo "FAIL  sessions"; exit 1; }
chmod 644 "$WORK/sessions.txt"
cp scripts/loadtest/feed.js "$WORK/feed.js" && chmod 644 "$WORK/feed.js"
cp scripts/loadtest/insights.js "$WORK/insights.js" && chmod 644 "$WORK/insights.js"
# One article for each person (the newest ones): the burst asks for $BURST different insights.
psql_cmd -c "SELECT id FROM items WHERE title LIKE 'Load %' ORDER BY published_at DESC, id LIMIT $BURST" > "$WORK/items.txt"
chmod 644 "$WORK/items.txt"
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

( while sleep 30; do
    docker stats --no-stream --format '{{.Name}} {{.CPUPerc}} {{.MemUsage}}' \
      $(compose ps -q) >> "$WORK/stats.txt" 2>/dev/null
  done ) &
sampler=$!
k6_status=0
summaries=()
[ "$SCENARIO" = insights ] && STEPS=$BURST
script=feed.js
[ "$SCENARIO" = insights ] && script=insights.js
for people in ${STEPS//,/ }; do
  echo "== k6 ($SCENARIO): $people people"
  touch "$WORK/summary-$people.json" && chmod 666 "$WORK/summary-$people.json"
  docker run --rm --network host -v "$WORK:/work" \
    -e BASE_URL="$PUBLIC" -e COOKIE_NAME=__Host-pharos_session \
    -e VUS="$people" -e DURATION="$DURATION" -e THINK="$THINK" \
    "$K6_IMAGE" run --quiet --summary-export="/work/summary-$people.json" "/work/$script" 2>&1 | tee -a "$WORK/k6.txt"
  [ "${PIPESTATUS[0]}" -eq 0 ] || k6_status=1
  cp "$WORK/summary-$people.json" "$OUT/summary-$people.json"
  summaries+=("$OUT/summary-$people.json")
done
kill "$sampler" 2>/dev/null

{
  if [ "$SCENARIO" = insights ]; then
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
  if [ "$SCENARIO" = insights ]; then
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
grep -qE "verdict on the (D5 feed targets|insight burst[^:]*): PASS" "$OUT/report.txt" && [ "$k6_status" -eq 0 ]
