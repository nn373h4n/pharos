#!/usr/bin/env bash
# Checks the development identity provider setup (docker-compose.sso-dev.yml) in a throwaway
# project: own name, own volumes, own image tag, random passwords, high ports. Removes everything
# when it ends. Usage: make test-sso-dev (needs Docker, takes minutes)
set -uo pipefail
cd "$(dirname "$0")/.."

PROJECT=pharos-sso
TAG=ssodev
HTTP_PORT=${PHAROS_SSO_HTTP_PORT:-19080}
HTTPS_PORT=${PHAROS_SSO_HTTPS_PORT:-19443}
PUBLIC="https://localhost:$HTTPS_PORT"
KEYCLOAK="https://auth.localhost:$HTTPS_PORT"
ISSUER="$KEYCLOAK/realms/pharos"
WORK=$(mktemp -d)
ENV_FILE="$WORK/env"

secret() { openssl rand -hex 24; }
umask 077
cat > "$ENV_FILE" <<EOF
POSTGRES_PASSWORD=$(secret)
PHAROS_OWNER_PASSWORD=$(secret)
PHAROS_APP_PASSWORD=$(secret)
PHAROS_REDIS_PASSWORD=$(secret)
PHAROS_DOMAIN=localhost
PHAROS_TLS=internal
PHAROS_HTTP_PORT=$HTTP_PORT
PHAROS_HTTPS_PORT=$HTTPS_PORT
PHAROS_ENVIRONMENT=production
PHAROS_MIGRATE_ON_START=true
PHAROS_IMAGE_TAG=$TAG
PHAROS_PUBLIC_URL=$PUBLIC
PHAROS_SECRET_KEY=$(secret)
PHAROS_KEYCLOAK_PUBLIC_URL=$KEYCLOAK
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

failures=0
pass() { printf 'PASS  %s\n' "$1"; }
fail() { printf 'FAIL  %s\n' "$1"; failures=$((failures + 1)); }
check() {
  local description=$1
  shift
  if "$@" >/dev/null 2>&1; then pass "$description"; else fail "$description"; fi
}

compose down --volumes --remove-orphans >/dev/null 2>&1
echo "== build and start (Keycloak takes a minute)"
if ! compose up -d --build 2>&1 | tail -3; then fail "docker compose up"; exit 1; fi

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
  fail "not healthy after 6 minutes:$pending"
  for service in $pending; do compose logs --tail 20 "$service" 2>&1; done
  exit 1
fi
pass "every service is healthy, Keycloak included"

echo "== the issuer seen from the browser side"
kc() { curl -sk --max-time 10 --resolve "auth.localhost:$HTTPS_PORT:127.0.0.1" "$@"; }
discovery=$(kc "$KEYCLOAK/realms/pharos/.well-known/openid-configuration")
field() { python3 -I -c "import json, sys; print(json.load(sys.stdin).get(sys.argv[1], ''))" "$1" <<<"$discovery"; }
[ "$(field issuer)" = "$ISSUER" ] && pass "issuer is $ISSUER" || fail "issuer is '$(field issuer)', expected $ISSUER"
for endpoint in authorization_endpoint token_endpoint jwks_uri end_session_endpoint; do
  case "$(field "$endpoint")" in
    "$ISSUER"/*) pass "$endpoint is under the issuer" ;;
    *) fail "$endpoint is '$(field "$endpoint")'" ;;
  esac
done
check "PKCE S256 is advertised" grep -q '"S256"' <<<"$discovery"

echo "== the same issuer seen from the API container"
for _ in $(seq 1 60); do
  compose exec -T api test -s /dev-ca/root.crt >/dev/null 2>&1 && break
  sleep 1
done
check "the API sees the public root certificate and nothing else of Caddy's data" \
  bash -c "$(declare -f compose); PROJECT='$PROJECT'; ENV_FILE='$ENV_FILE'; compose exec -T api sh -c 'test -s /dev-ca/root.crt && [ \"\$(ls /dev-ca)\" = root.crt ] && ! ls /dev-ca/*.key >/dev/null 2>&1'"
probe='
import json, socket, ssl, sys, urllib.request
issuer, port = sys.argv[1], int(sys.argv[2])
address = socket.getaddrinfo("auth.localhost", port, proto=socket.IPPROTO_TCP)[0][4][0]
print("address", address)
body = json.load(urllib.request.urlopen(issuer + "/.well-known/openid-configuration", timeout=10))
print("issuer", body["issuer"])
keys = json.load(urllib.request.urlopen(body["jwks_uri"], timeout=10))["keys"]
print("keys", len(keys))
'
inside=$(compose exec -T api python -c "$probe" "$ISSUER" "$HTTPS_PORT" 2>&1)
echo "$inside" | sed 's/^/      /'
[ "$(grep '^issuer ' <<<"$inside" | cut -d' ' -f2)" = "$ISSUER" ] && pass "the API sees the same issuer" || fail "the API does not see the same issuer"
check "the API fetched the signing keys" grep -qE '^keys [1-9]' <<<"$inside"
address=$(grep '^address ' <<<"$inside" | cut -d' ' -f2)
case "$address" in 127.* | "" | ::1) fail "auth.localhost resolves to '$address' inside the API container" ;; *) pass "auth.localhost resolves to the Caddy container ($address), not to loopback" ;; esac
if compose exec -T -e SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt api python -c "$probe" "$ISSUER" "$HTTPS_PORT" >/dev/null 2>&1; then
  fail "the API accepts the certificate without trusting Caddy's root (the trust file has no effect)"
else
  pass "without Caddy's root certificate the API rejects the connection (the trust file is what makes it work)"
fi

echo "== the realm"
token=$(kc -d grant_type=password -d client_id=admin-cli -d username=admin -d password=dev-only-admin-password \
  "$KEYCLOAK/realms/master/protocol/openid-connect/token" | python3 -I -c "import json, sys; print(json.load(sys.stdin).get('access_token', ''))")
alice=$(kc -H "Authorization: Bearer $token" "$KEYCLOAK/admin/realms/pharos/users?username=alice&exact=true")
check "the development user alice has her department and job title attributes" \
  bash -c "grep -q '\"department\":\\[\"Finance\"\\]' <<<'$alice' && grep -q '\"jobTitle\":\\[\"Accountant\"\\]' <<<'$alice'"
clients=$(kc -H "Authorization: Bearer $token" "$KEYCLOAK/admin/realms/pharos/clients?clientId=pharos")
check "the client requires PKCE S256" grep -q '"pkce.code.challenge.method":"S256"' <<<"$clients"
check "the client redirect URI is $PUBLIC/api/auth/oidc/callback" grep -q "\"$PUBLIC/api/auth/oidc/callback\"" <<<"$clients"
check "direct access grants (password flow) are off for the client" grep -q '"directAccessGrantsEnabled":false' <<<"$clients"

echo "== the main site still works with the shared port numbers"
check "https://localhost serves the web app" curl -sk --max-time 10 --resolve "localhost:$HTTPS_PORT:127.0.0.1" -f "$PUBLIC/healthz"
location=$(curl -s --max-time 10 -o /dev/null -w '%{redirect_url}' -H "Host: localhost:$HTTP_PORT" "http://127.0.0.1:$HTTP_PORT/")
[ "$location" = "$PUBLIC/" ] && pass "HTTP redirects to $PUBLIC/ (the right port)" || fail "HTTP redirects to '$location'"

echo
if [ "$failures" -eq 0 ]; then echo "All checks passed."; else echo "$failures check(s) FAILED."; exit 1; fi
