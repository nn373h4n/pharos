#!/usr/bin/env bash
# Two resilience checks of the worker on a THROWAWAY copy of the real stack (M5-08):
#
#   1. Memory: a feed of 10,000 entries is fetched and read; the worker's peak memory (the
#      container's own peak, from its cgroup) must stay under 300 MiB. The parser reads at most 200
#      entries, which is what keeps it small.
#   2. A worker killed in the middle of a fetch (`docker kill`) leaves nothing half done: after it
#      is back and its lease has run out, the source is fetched in full with no entry lost and none
#      doubled, and a second run adds nothing.
#
# The stack is built from the checkout (own project name, volumes, image tag, random passwords,
# high ports), uses the test-only overlay of the stack tests (one fake feed host, the only address
# the worker may reach) and is removed again. The lease of a fetch lasts five minutes, so the
# second check waits for it: the whole run takes about 15 minutes.
#
# Usage: make test-resilience   (needs Docker and python3); RESIL_ONLY=memory runs the first check only
set -uo pipefail
cd "$(dirname "$0")/.."

PROJECT=pharos-resil
TAG=resil
HTTP_PORT=${PHAROS_RESIL_HTTP_PORT:-19380}
HTTPS_PORT=${PHAROS_RESIL_HTTPS_PORT:-19743}
# The generated env file is the only source of settings (scripts/lib/throwaway-env.sh).
source scripts/lib/throwaway-env.sh
drop_installation_env PHAROS_RESIL_HTTP_PORT PHAROS_RESIL_HTTPS_PORT
ORG_SLUG=resil
MEMORY_LIMIT_MIB=300
SLOW_ENTRIES=120
WORK=$(mktemp -d)
ENV_FILE="$WORK/env"
export STACK_FEED_DIR="$WORK/feeds"
mkdir "$STACK_FEED_DIR"
chmod 755 "$STACK_FEED_DIR"

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
# Only this machine can reach the throwaway stack (M5-13 review).
PHAROS_BIND_ADDRESS=127.0.0.1
PHAROS_APP_SUBNET=172.28.35.0/24
PHAROS_APP_DYNAMIC_RANGE=172.28.35.0/25
PHAROS_CADDY_IP=172.28.35.254
PHAROS_ENVIRONMENT=production
PHAROS_MIGRATE_ON_START=true
PHAROS_IMAGE_TAG=$TAG
PHAROS_PUBLIC_URL=https://localhost:$HTTPS_PORT
PHAROS_DEFAULT_LOCALE=en
PHAROS_ORG_SLUG=$ORG_SLUG
PHAROS_LOCAL_LOGIN=break-glass
PHAROS_LLM_PROVIDER=anthropic
PHAROS_LLM_MODEL_FILTER=stack-filter
PHAROS_LLM_MODEL_ENRICHMENT=stack-enrichment
PHAROS_LLM_MODEL_INSIGHT=stack-insight
EOF

# The feed host: a file server that also sends one file slowly (see scripts/resilience/feed_server.py).
cp scripts/resilience/feed_server.py "$STACK_FEED_DIR/serve.py"
chmod 644 "$STACK_FEED_DIR/serve.py"
cat > "$WORK/slow-host.yml" <<'EOF'
services:
  feedhost:
    command: ["python", "/feeds/serve.py"]
EOF
python3 - "$STACK_FEED_DIR" "$SLOW_ENTRIES" <<'PYEOF'
import sys

directory, slow = sys.argv[1], int(sys.argv[2])


def feed(count: int, prefix: str) -> str:
    items = "".join(
        f"<item><guid isPermaLink=\"false\">{prefix}-{n}</guid><title>{prefix} entry {n}</title>"
        f"<link>https://e.example.org/{prefix}/{n}</link>"
        f"<pubDate>Tue, 20 Oct 2026 10:00:00 GMT</pubDate></item>"
        for n in range(count)
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel>'
        f"<title>{prefix}</title>{items}</channel></rss>"
    )


for name, count in (("big", 10_000), ("slow", slow)):
    with open(f"{directory}/{name}.xml", "w", encoding="utf-8") as handle:
        handle.write(feed(count, name))
PYEOF
chmod 644 "$STACK_FEED_DIR"/*.xml
# The fetcher refuses an answer over 2 MiB (too_large): the big feed must be under it, or this would
# measure the refusal and not the reading.
if [ "$(wc -c < "$STACK_FEED_DIR/big.xml")" -ge 2097152 ]; then echo "FAIL  big.xml is not under 2 MiB"; exit 1; fi
echo "feed files: big.xml $(wc -c < "$STACK_FEED_DIR/big.xml") bytes (10,000 entries), slow.xml $(wc -c < "$STACK_FEED_DIR/slow.xml") bytes ($SLOW_ENTRIES entries)"

compose() {
  docker compose -p "$PROJECT" -f deploy/docker-compose.yml -f deploy/docker-compose.stack-e2e.yml \
    -f "$WORK/slow-host.yml" --env-file "$ENV_FILE" "$@"
}
cleanup() {
  compose down --volumes --remove-orphans >/dev/null 2>&1
  docker rmi "pharos-web:$TAG" "pharos-backend:$TAG" >/dev/null 2>&1
  rm -r -- "$WORK"
}
trap cleanup EXIT

failures=0
pass() { echo "PASS  $1"; }
fail() { echo "FAIL  $1"; failures=$((failures + 1)); }
check() { # description, command...  (the command must succeed)
  local description=$1
  shift
  if "$@" >/dev/null 2>&1; then pass "$description"; else fail "$description"; fi
}

compose down --volumes --remove-orphans >/dev/null 2>&1
echo "== build and start"
if ! compose up -d --build 2>&1 | tail -8; then echo "FAIL  docker compose up"; exit 1; fi
health() { docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$(compose ps -q "$1")" 2>/dev/null; }
for _ in $(seq 1 120); do
  pending=""
  for service in postgres redis api worker feedhost; do
    [ "$(health "$service")" = "healthy" ] || [ "$(health "$service")" = "running" ] || pending="$pending $service"
  done
  [ -z "$pending" ] && break
  sleep 2
done
[ -z "$pending" ] || { echo "FAIL  not up after 4 minutes:$pending"; exit 1; }

q() { compose exec -T postgres psql -q -tA -v ON_ERROR_STOP=1 -U postgres -d pharos -c "$1"; }
compose run --rm migrate python -m pharos.cli create-org --slug "$ORG_SLUG" --name "Resilience" || exit 1

add_source() { # name, file
  q "INSERT INTO sources (org_id, type, name, config, dedupe_key, enabled, interval_seconds) SELECT id, 'rss', '$1', '{\"url\": \"http://feed.example.test:8080/$2\"}'::jsonb, md5('$1$2') || md5('$2'), true, 3600 FROM organizations WHERE slug = '$ORG_SLUG'" >/dev/null
}
make_due() { q "UPDATE sources SET next_fetch_at = now() - interval '1 second', lease_until = NULL, lease_token = NULL WHERE name = '$1'" >/dev/null; }
source_id() { q "SELECT id FROM sources WHERE name = '$1'"; }
runs_where() { q "SELECT count(*) FROM fetch_runs WHERE source_id = '$(source_id "$1")' AND $2"; }
peak_mib() { # the worker container's own peak memory since it started, in MiB
  local bytes
  bytes=$(compose exec -T worker sh -c 'cat /sys/fs/cgroup/memory.peak 2>/dev/null || cat /sys/fs/cgroup/memory/memory.max_usage_in_bytes 2>/dev/null' | tr -d '\r\n ')
  [[ "$bytes" =~ ^[0-9]+$ ]] || { echo ""; return; }
  echo $((bytes / 1048576))
}
wait_for() { # seconds, description, command...  (polls until the command succeeds)
  local seconds=$1 end
  shift 2
  end=$((SECONDS + seconds))
  until "$@" >/dev/null 2>&1; do
    [ "$SECONDS" -lt "$end" ] || return 1
    sleep 1
  done
}

echo "== 1. memory: a feed of 10,000 entries"
before=$(peak_mib)
check "the worker's cgroup shows a memory peak (the measure exists)" test -n "$before"
add_source Big big.xml
make_due Big
big_done() { test "$(runs_where Big "outcome <> 'running'")" -ge 1; }
check "the 10,000-entry feed was fetched within 4 minutes" wait_for 240 "big" big_done
check "the run ended well (outcome ok)" test "$(runs_where Big "outcome = 'ok'")" = 1
read_entries=$(q "SELECT count(*) FROM raw_items WHERE source_id = '$(source_id Big)'")
echo "info  $read_entries entries of 10,000 were read (the parser's cap)"
check "no more than 200 entries were read (the cap holds)" test "${read_entries:-0}" -le 200
after=$(peak_mib)
echo "info  worker memory peak: ${before:-?} MiB when the check started, ${after:-?} MiB after the big feed (limit $MEMORY_LIMIT_MIB)"
if [ "$(runs_where Big "outcome = 'ok'")" = 1 ] && [ "${read_entries:-0}" -ge 1 ]; then
  check "the worker's peak memory is under $MEMORY_LIMIT_MIB MiB" test -n "$after" -a "${after:-99999}" -lt "$MEMORY_LIMIT_MIB"
else
  fail "the worker's peak memory was NOT MEASURED: the feed was not read, so the number says nothing"
fi
[ "${RESIL_ONLY:-}" = memory ] && { echo; [ "$failures" -eq 0 ] && echo "All checks passed." || echo "$failures check(s) FAILED."; exit "$((failures > 0))"; }

echo "== 2. a worker killed in the middle of a fetch"
add_source Slow slow.xml
make_due Slow
running_now() { test "$(runs_where Slow "outcome = 'running'")" -ge 1; }
check "the slow fetch began (a run is open)" wait_for 150 "slow" running_now
sleep 2   # the file takes about six seconds to arrive: this is the middle of it
echo "info  open runs: $(runs_where Slow "outcome = 'running'"), entries stored: $(q "SELECT count(*) FROM raw_items WHERE source_id = '$(source_id Slow)'")"
docker kill "$(compose ps -q worker)" >/dev/null
echo "info  worker killed with docker kill"
sleep 8
if [ "$(health worker)" != "healthy" ] && [ "$(health worker)" != "running" ] && [ "$(health worker)" != "starting" ]; then
  compose start worker >/dev/null 2>&1
  echo "info  worker started again by hand"
else
  echo "info  worker came back by itself (its restart policy)"
fi
check "nothing was stored by the interrupted fetch" test "$(q "SELECT count(*) FROM raw_items WHERE source_id = '$(source_id Slow)'")" = 0
left_open=$(runs_where Slow "outcome = 'running'")
echo "info  the killed run stays open as 'running': $left_open (it is history, the lease decides what happens next)"

echo "== waiting for the lease (five minutes) and the next dispatch"
slow_ok() { test "$(runs_where Slow "outcome = 'ok'")" -ge 1; }
check "the source was fetched in full after the worker came back" wait_for 480 "slow" slow_ok
check "exactly one run ended well" test "$(runs_where Slow "outcome = 'ok'")" = 1
stored=$(q "SELECT count(*) FROM raw_items WHERE source_id = '$(source_id Slow)'")
distinct=$(q "SELECT count(DISTINCT external_id) FROM raw_items WHERE source_id = '$(source_id Slow)'")
echo "info  entries stored: $stored, distinct: $distinct, expected $SLOW_ENTRIES"
check "every entry arrived (none lost)" test "$stored" = "$SLOW_ENTRIES"
check "no entry is there twice" test "$stored" = "$distinct"
items_after_first=$(q "SELECT count(*) FROM item_sources WHERE source_id = '$(source_id Slow)'")
check "an article was made for each entry" test "$items_after_first" = "$SLOW_ENTRIES"

make_due Slow
second_done() { test "$(runs_where Slow "outcome <> 'running'")" -ge 2; }
check "a second run ended" wait_for 240 "second" second_done
newest_new=$(q "SELECT items_new FROM fetch_runs WHERE source_id = '$(source_id Slow)' AND outcome <> 'running' ORDER BY started_at DESC LIMIT 1")
check "the second run added nothing (items_new = 0)" test "${newest_new:-x}" = 0
check "and the number of stored entries is the same" \
  test "$(q "SELECT count(*) FROM raw_items WHERE source_id = '$(source_id Slow)'")" = "$SLOW_ENTRIES"

echo
if [ "$failures" -eq 0 ]; then echo "All checks passed."; else echo "$failures check(s) FAILED."; fi
exit "$((failures > 0))"
