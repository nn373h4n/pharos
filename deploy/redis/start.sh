#!/bin/sh
# Starts Redis with one user per process, so a compromised worker cannot touch sign-in state.
#
# The API is trusted: it owns the session records (pharos:session:*), the rate-limit counters
# (pharos:rl:*) and the sign-in state (pharos:oidc:*). The worker and the scheduler handle content
# from outside, so their user sees only what Celery needs (the queue, kombu's bookkeeping) and the
# worker's own keys (pharos:worker:*). Anything else is refused with NOPERM. Neither user can run
# the dangerous commands (CONFIG, KEYS, FLUSHALL, DEBUG, ACL, ...), and the default user is off.
# The worker cannot even list key names (SCAN, RANDOMKEY); the names are hashes anyway.
#
# Used by deploy/docker-compose.yml and by scripts/test-infra.sh, so the tests run against the rules
# that are deployed. Closes follow-up F-14 (ADR-0012).
set -eu

: "${REDIS_API_PASSWORD:?set REDIS_API_PASSWORD}"
: "${REDIS_WORKER_PASSWORD:?set REDIS_WORKER_PASSWORD}"
case "$REDIS_API_PASSWORD$REDIS_WORKER_PASSWORD" in
  *[!A-Za-z0-9_.-]*) echo "Redis passwords may only contain letters, digits, _ . -" >&2; exit 1 ;;
esac
[ "$REDIS_API_PASSWORD" != "$REDIS_WORKER_PASSWORD" ] || {
  echo "The API and the worker need different Redis passwords." >&2
  exit 1
}

# The command line carries SHA-256 hashes (#hash), not the passwords, so `ps` shows nothing usable.
hash_of() { printf '%s' "$1" | sha256sum | cut -d' ' -f1; }
api_hash=$(hash_of "$REDIS_API_PASSWORD")
worker_hash=$(hash_of "$REDIS_WORKER_PASSWORD")

# noeviction: sessions and rate-limit counters must never be dropped to make room; when --maxmemory
# is reached Redis refuses writes instead (and sign-in then fails closed). The limit is below the
# container's memory limit, so the kernel never has to kill Redis.
#
# The worker's key patterns are exact on purpose. Celery's node-control (pidbox) keys are
# `celery@<host>.celery.pidbox` (covered by celery*), `<uuid>.reply.celery.pidbox` and, in tests,
# `gen<pid>@<host>.celery.pidbox`; a pattern like `*.celery.pidbox*` would also match a key such as
# `pharos:rl:<x>.celery.pidbox`. Functions are off for the worker (scripts stay: kombu's lock release uses EVALSHA, and a script is checked against the key patterns too), and so are the
# commands that would show the size or the names of what it cannot read.
exec redis-server \
  --appendonly yes --maxmemory 200mb --maxmemory-policy noeviction \
  --user default off -@all \
  --user pharos_api on "#$api_hash" '~*' '&*' +@all -@dangerous \
  --user pharos_worker on "#$worker_hash" \
  '~celery*' '~gen*@*.celery.pidbox*' '~[0-9a-f]*-*-*-*-*.reply.celery.pidbox*' \
  '~_kombu.*' '~unacked*' '~pharos:worker:*' \
  '&*' +@all -@dangerous -@admin -function -scan -randomkey -dbsize -select \
  -client\|tracking
