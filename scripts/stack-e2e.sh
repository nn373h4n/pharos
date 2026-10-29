#!/usr/bin/env bash
# Runs the browser tests against the REAL stack: Caddy over HTTPS, the web app, the API, PostgreSQL,
# Redis and a development Keycloak. A throwaway copy of the stack (own project name, volumes, image
# tag, random passwords, high ports) is built, filled with an organization and an administrator,
# tested with Playwright and removed again.
#
# Usage: make test-stack-e2e   (needs Docker, Node/pnpm and: pnpm --dir frontend exec playwright install chromium)
set -uo pipefail
cd "$(dirname "$0")/.."

PROJECT=pharos-stack
TAG=stacke2e
HTTP_PORT=${PHAROS_STACK_HTTP_PORT:-19180}
HTTPS_PORT=${PHAROS_STACK_HTTPS_PORT:-19543}
PUBLIC="https://localhost:$HTTPS_PORT"
KEYCLOAK="https://auth.localhost:$HTTPS_PORT"
ISSUER="$KEYCLOAK/realms/pharos"
ORG_SLUG=stack
WORK=$(mktemp -d)
ENV_FILE="$WORK/env"

secret() { openssl rand -hex 24; }
ADMIN_PASSWORD=$(secret)
umask 077
cat > "$ENV_FILE" <<EOF
POSTGRES_PASSWORD=$(secret)
PHAROS_OWNER_PASSWORD=$(secret)
PHAROS_APP_PASSWORD=$(secret)
PHAROS_REDIS_PASSWORD=$(secret)
PHAROS_REDIS_WORKER_PASSWORD=$(secret)
PHAROS_SECRET_KEY=$(secret)
PHAROS_DOMAIN=localhost
PHAROS_TLS=internal
PHAROS_HTTP_PORT=$HTTP_PORT
PHAROS_HTTPS_PORT=$HTTPS_PORT
PHAROS_APP_SUBNET=172.28.33.0/24
PHAROS_APP_DYNAMIC_RANGE=172.28.33.0/25
PHAROS_CADDY_IP=172.28.33.254
PHAROS_ENVIRONMENT=production
PHAROS_MIGRATE_ON_START=true
PHAROS_IMAGE_TAG=$TAG
PHAROS_PUBLIC_URL=$PUBLIC
PHAROS_DEFAULT_LOCALE=en
PHAROS_ORG_SLUG=$ORG_SLUG
PHAROS_LOCAL_LOGIN=break-glass
PHAROS_KEYCLOAK_PUBLIC_URL=$KEYCLOAK
PHAROS_OIDC_ISSUER=$ISSUER
PHAROS_OIDC_CLIENT_ID=pharos
PHAROS_OIDC_CLIENT_SECRET=dev-only-client-secret
PHAROS_OIDC_PROVIDER_NAME=Keycloak (development)
EOF

compose() {
  docker compose -p "$PROJECT" -f deploy/docker-compose.yml -f deploy/docker-compose.sso-dev.yml \
    --env-file "$ENV_FILE" "$@"
}
cleanup() {
  compose down --volumes --remove-orphans >/dev/null 2>&1
  docker rmi "pharos-web:$TAG" "pharos-backend:$TAG" >/dev/null 2>&1
  rm -r "$WORK"
}
trap cleanup EXIT

compose down --volumes --remove-orphans >/dev/null 2>&1
echo "== build and start the stack (Keycloak takes a minute)"
if ! compose up -d --build 2>&1 | tail -15; then echo "FAIL  docker compose up"; exit 1; fi

health() { docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$(compose ps -q "$1")" 2>/dev/null; }
for _ in $(seq 1 180); do
  pending=""
  for service in postgres redis web api worker caddy keycloak; do
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

echo "== operator steps: organization and break-glass administrator"
compose run --rm migrate python -m pharos.cli create-org --slug "$ORG_SLUG" --name "Stack test" || exit 1
printf '%s\n' "$ADMIN_PASSWORD" |
  compose exec -T api python -m pharos.cli create-admin --username stack-admin --password-stdin || exit 1

echo "== browser tests"
# Lets the specs change the database the way an operator would (disable an account, read the audit log).
psql_cmd() { compose exec -T postgres psql -q -tA -U postgres -d pharos "$@"; }
export STACK_URL="$PUBLIC"
export STACK_KEYCLOAK_URL="$KEYCLOAK"
export STACK_ADMIN_USER=stack-admin
export STACK_ADMIN_PASSWORD="$ADMIN_PASSWORD"
export STACK_ORG_SLUG="$ORG_SLUG"
export STACK_PSQL="docker compose -p $PROJECT -f $PWD/deploy/docker-compose.yml -f $PWD/deploy/docker-compose.sso-dev.yml --env-file $ENV_FILE exec -T postgres psql -q -tA -U postgres -d pharos"
(cd frontend && pnpm exec playwright test -c playwright.stack.config.ts "$@")
status=$?
if [ "$status" -ne 0 ]; then
  echo "== API log (last lines)"
  compose logs --tail 40 api 2>&1 | sed 's/^/      /'
fi
exit "$status"
