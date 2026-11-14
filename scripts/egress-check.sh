#!/usr/bin/env bash
# Proves the egress rules of deploy/egress/pharos-egress.sh from inside the services (ADR-0023,
# M5-03, gate G7). Read-only: it opens TCP connections and closes them, and changes nothing.
#
#   scripts/egress-check.sh            on a running stack (docker compose exec into api and worker)
#
# Each line is one call that must be refused or one that must pass, from the service the rule is
# about. The calls that must pass are the controls: they show that a refusal comes from the rule,
# not from a network that is down. Printed as PASS, FAIL or NOT CHECKED; exit status 1 if anything
# FAILs, 2 for wrong usage.
#
# What is called (set the variables to use other addresses):
#   EGRESS_PUBLIC     a public address that answers on 80 and 443 (default 1.1.1.1)
#   EGRESS_NAME       a public name that must resolve (default example.com)
#   EGRESS_PRIVATE    a private address (default 10.255.255.1)
#   EGRESS_METADATA   the cloud metadata address (169.254.169.254)
#   EGRESS_GATEWAY    the host's address on the egress network (default: the first of the range);
#                     the script listens there for a moment, on EGRESS_GATEWAY_PORT, and first
#                     checks from the host that the listener answers
# On a real host the private and metadata calls usually find nothing listening: then "refused" says
# little, and the line says so. scripts/egress-netns-test.sh proves those rules with listeners.
#
# EGRESS_RUN_API and EGRESS_RUN_WORKER replace `docker compose exec` (the namespace test uses them).
set -uo pipefail
cd "$(dirname "$0")/.."

read -r -a COMPOSE <<<"${PHAROS_COMPOSE:-docker compose -f deploy/docker-compose.yml --env-file deploy/.env}"
read_env() {
  local name=$1 default=$2 value=
  value=${!name:-}
  if [ -z "$value" ] && [ -f deploy/.env ]; then
    value=$(sed -n "s/^${name}=//p" deploy/.env | tail -n 1)
  fi
  printf '%s' "${value:-$default}"
}
SUBNET=$(read_env PHAROS_EGRESS_SUBNET 172.28.40.0/24)
PUBLIC=${EGRESS_PUBLIC:-1.1.1.1}
NAME=${EGRESS_NAME:-example.com}
PRIVATE=${EGRESS_PRIVATE:-10.255.255.1}
METADATA=${EGRESS_METADATA:-169.254.169.254}
base=${SUBNET%/*}
GATEWAY=${EGRESS_GATEWAY:-${base%.*}.$(( ${base##*.} + 1 ))}
GATEWAY_PORT=${EGRESS_GATEWAY_PORT:-18999}
LISTENERS=${EGRESS_LISTENERS:-}  # "yes" when every target has a listener (the namespace test)

octet='(25[0-5]|2[0-4][0-9]|1[0-9][0-9]|[1-9]?[0-9])'
for address in "$PUBLIC" "$PRIVATE" "$METADATA" "$GATEWAY"; do
  [[ $address =~ ^$octet\.$octet\.$octet\.$octet$ ]] || { echo "not an IPv4 address: $address" >&2; exit 2; }
done
[[ $NAME =~ ^[a-z0-9]([a-z0-9.-]{0,251}[a-z0-9])?$ ]] || { echo "not a host name: $NAME" >&2; exit 2; }
[[ $GATEWAY_PORT =~ ^[0-9]{4,5}$ ]] || { echo "not a port: $GATEWAY_PORT" >&2; exit 2; }

failed=0
report() {
  printf '%-12s %-7s %s\n' "$1" "$2" "$3"
  [ "$1" = FAIL ] && failed=1
  return 0
}

run_in() {
  local service=$1 code=$2 runner
  if [ "$service" = api ]; then runner=${EGRESS_RUN_API:-}; else runner=${EGRESS_RUN_WORKER:-}; fi
  if [ -n "$runner" ]; then
    read -r -a prefix <<<"$runner"
    "${prefix[@]}" python3 -c "$code"
  else
    "${COMPOSE[@]}" exec -T "$service" python -c "$code"
  fi
}

# Prints open or closed: a connection made within three seconds, or not.
connect() {
  run_in "$1" "
import socket, sys
try:
    socket.create_connection(('$2', $3), timeout=3).close()
    print('open')
except OSError:
    print('closed')
" 2>/dev/null || echo error
}

resolves() {
  run_in "$1" "
import socket
try:
    socket.getaddrinfo('$NAME', 443)
    print('open')
except OSError:
    print('closed')
" 2>/dev/null || echo error
}

expect() {
  local service=$1 target=$2 port=$3 want=$4 why=$5 got
  got=$(connect "$service" "$target" "$port")
  if [ "$got" = "$want" ]; then
    report PASS "$service" "$target:$port $want ($why)"
  elif [ "$got" = error ]; then
    report "NOT CHECKED" "$service" "$target:$port: the service could not be asked"
  else
    report FAIL "$service" "$target:$port is $got, expected $want ($why)"
  fi
}

# A short listener on the host's address on the egress network, so the gateway rule has something to
# refuse; checked from the host first.
gateway_listener=
if [ -z "${EGRESS_RUN_API:-}" ] && command -v python3 >/dev/null; then
  python3 -c "
import socket, time
s = socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.bind(('$GATEWAY', $GATEWAY_PORT)); s.listen(8); s.settimeout(1)
end = time.time() + 120
while time.time() < end:
    try:
        s.accept()[0].close()
    except OSError:
        pass
" 2>/dev/null &
  gateway_listener=$!
  trap 'kill "$gateway_listener" 2>/dev/null' EXIT
  sleep 1
fi
gateway_control=no
if [ -n "${EGRESS_RUN_API:-}" ] || python3 -c "
import socket; socket.create_connection(('$GATEWAY', $GATEWAY_PORT), timeout=3).close()
" 2>/dev/null; then
  gateway_control=yes
fi

for service in api worker; do
  # Controls: what the rules must let through.
  expect "$service" "$PUBLIC" 443 open "public HTTPS is allowed"
  if [ "$(resolves "$service")" = open ]; then
    report PASS "$service" "$NAME resolves (DNS works)"
  else
    report FAIL "$service" "$NAME does not resolve: the resolvers are not allowed on port 53"
  fi
  if [ "$service" = worker ]; then
    expect worker "$PUBLIC" 80 open "public HTTP is allowed for feeds"
  else
    expect api "$PUBLIC" 80 closed "the API needs HTTPS only"
  fi
  # What must be refused.
  expect "$service" "$PUBLIC" 25 closed "no port but 80 and 443"
  if [ "$LISTENERS" = yes ]; then
    expect "$service" "$PRIVATE" 80 closed "no private address"
    expect "$service" "$METADATA" 80 closed "no cloud metadata"
  else
    for target in "$PRIVATE" "$METADATA"; do
      got=$(connect "$service" "$target" 80)
      if [ "$got" = closed ]; then
        report PASS "$service" "$target:80 closed (nothing may listen there on this host; proven with listeners by scripts/egress-netns-test.sh)"
      else
        report FAIL "$service" "$target:80 is $got, expected closed"
      fi
    done
  fi
  if [ "$gateway_control" = yes ]; then
    expect "$service" "$GATEWAY" "$GATEWAY_PORT" closed "not the host, not the bridge gateway"
  else
    report "NOT CHECKED" "$service" "$GATEWAY:$GATEWAY_PORT: no listener could be opened on the host"
  fi
done

exit "$failed"
