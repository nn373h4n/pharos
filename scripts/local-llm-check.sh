#!/usr/bin/env bash
# Checks, on a throwaway copy of the local model server (profile "local-llm", ADR-0020), the claims
# the documents make: it runs without privilege, has no route out of its own, is reached by the
# members of its network only, and reads its models without being able to change them.
#
# It starts the server and the volume helper in a project of its own with random settings, runs the
# checks, and removes everything again. It pulls the Ollama image on the first run (several GB), so
# it is not part of `make test-compose`. Usage: scripts/local-llm-check.sh   (needs Docker)
set -uo pipefail
cd "$(dirname "$0")/.."

PROJECT=pharos-llmcheck
WORK=$(mktemp -d)
ENV_FILE="$WORK/env"
secret() { openssl rand -hex 16; }
umask 077
cat > "$ENV_FILE" <<EOF
POSTGRES_PASSWORD=$(secret)
PHAROS_OWNER_PASSWORD=$(secret)
PHAROS_APP_PASSWORD=$(secret)
PHAROS_WORKER_DB_PASSWORD=$(secret)
PHAROS_REDIS_PASSWORD=$(secret)
PHAROS_REDIS_WORKER_PASSWORD=$(secret)
PHAROS_SECRET_KEY=$(secret)$(secret)
PHAROS_PUBLIC_URL=https://localhost
PHAROS_OLLAMA_SUBNET=172.28.31.0/24
PHAROS_OLLAMA_IP=172.28.31.10
PHAROS_OLLAMA_MEMORY=3g
EOF
compose() { docker compose -p "$PROJECT" -f deploy/docker-compose.yml --env-file "$ENV_FILE" "$@"; }
cleanup() { compose --profile local-llm --profile local-llm-pull down -v --remove-orphans >/dev/null 2>&1; rm -r "$WORK"; }
trap cleanup EXIT

fail=0
check() { # check "name" command...
  local name=$1; shift
  if "$@" >/dev/null 2>&1; then echo "PASS  $name"; else echo "FAIL  $name"; fail=1; fi
}
refused() { # refused "name" command...: the command must NOT succeed
  local name=$1; shift
  if "$@" >/dev/null 2>&1; then echo "FAIL  $name"; fail=1; else echo "PASS  $name"; fi
}

compose --profile local-llm down -v --remove-orphans >/dev/null 2>&1
echo "== start the server (the image is pulled on the first run)"
compose --profile local-llm up -d ollama 2>&1 | tail -3
server=$(compose ps -q ollama)
for _ in $(seq 1 60); do
  [ "$(docker inspect --format '{{.State.Health.Status}}' "$server" 2>/dev/null)" = healthy ] && break
  sleep 2
done
check "the server becomes healthy" test "$(docker inspect --format '{{.State.Health.Status}}' "$server")" = healthy

image=$(docker inspect --format '{{.Config.Image}}' "$server")
network="${PROJECT}_llm"
inspect() { docker inspect --format "$1" "$server"; }

echo "== the server itself"
check "runs as a non-root user" test "$(docker exec "$server" id -u)" = 10001
check "root file system is read-only" test "$(inspect '{{.HostConfig.ReadonlyRootfs}}')" = true
check "every capability is dropped" test "$(inspect '{{.HostConfig.CapDrop}}')" = "[ALL]"
check "no new privileges" test "$(inspect '{{.HostConfig.SecurityOpt}}')" = "[no-new-privileges:true]"
check "no port is published" test "$(inspect '{{json .NetworkSettings.Ports}}' | tr -d ' ')" = '{"11434/tcp":null}'
check "the models are mounted read-only" test "$(inspect '{{range .Mounts}}{{if eq .Destination "/models"}}{{.RW}}{{end}}{{end}}')" = false
check "it is on one network only" test "$(inspect '{{len .NetworkSettings.Networks}}')" = 1
check "that network is internal" test "$(docker network inspect "$network" --format '{{.Internal}}')" = true
check "it has the fixed address" test "$(inspect '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}')" = 172.28.31.10

echo "== reach"
client() { docker run --rm "$@" -e OLLAMA_HOST=http://172.28.31.10:11434 --entrypoint ollama "$image" list; }
check "a member of the network reaches it (positive control)" client --network "$network"
refused "a container outside the network does not" client
refused "the server cannot reach the registry (no route out)" docker exec "$server" ollama pull library/smollm2:135m
refused "the server cannot change its models (read-only)" docker exec "$server" sh -c 'touch /models/x'

[ "$fail" -eq 0 ] && echo "ALL PASS" || echo "SOME CHECKS FAILED"
exit "$fail"
