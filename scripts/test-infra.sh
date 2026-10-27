#!/usr/bin/env bash
# Throwaway PostgreSQL and Redis containers for the integration tests.
# Usage: scripts/test-infra.sh up | down | run
#   up   start the containers and wait until they accept connections
#   down remove them
#   run  up, run `pytest -m integration`, then down (even when tests fail)
set -euo pipefail

PG_NAME=pharos-test-pg
REDIS_NAME=pharos-test-redis
PG_PORT=${PHAROS_TEST_PG_PORT:-55432}
REDIS_PORT=${PHAROS_TEST_REDIS_PORT:-56379}
# The same images as the Compose stack, so the tests run against what is deployed.
root="$(cd "$(dirname "$0")/.." && pwd)"
PG_IMAGE=$("$root/scripts/compose-image.sh" postgres)
REDIS_IMAGE=$("$root/scripts/compose-image.sh" redis)
# Throwaway password for a container that listens on 127.0.0.1 only and is deleted afterwards.
PG_PASSWORD=pharos-test-only

up() {
  docker run -d --name "$PG_NAME" -e POSTGRES_PASSWORD="$PG_PASSWORD" \
    -p "127.0.0.1:$PG_PORT:5432" "$PG_IMAGE" >/dev/null
  docker run -d --name "$REDIS_NAME" -p "127.0.0.1:$REDIS_PORT:6379" "$REDIS_IMAGE" >/dev/null
  for _ in $(seq 1 60); do
    if docker exec "$PG_NAME" pg_isready -U postgres >/dev/null 2>&1 \
      && docker exec "$REDIS_NAME" redis-cli ping >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  echo "test infrastructure did not become ready in 60s" >&2
  docker logs "$PG_NAME" 2>&1 | tail -5 >&2
  return 1
}

down() {
  docker rm -f "$PG_NAME" "$REDIS_NAME" >/dev/null 2>&1 || true
}

case "${1:-}" in
  up) up ;;
  down) down ;;
  run)
    trap down EXIT
    down
    up
    export PHAROS_TEST_ADMIN_URL="postgresql+asyncpg://postgres:$PG_PASSWORD@127.0.0.1:$PG_PORT/postgres"
    export PHAROS_TEST_REDIS_URL="redis://127.0.0.1:$REDIS_PORT/0"
    uv run pytest backend/tests -m integration
    ;;
  *)
    echo "usage: $0 up|down|run" >&2
    exit 2
    ;;
esac
