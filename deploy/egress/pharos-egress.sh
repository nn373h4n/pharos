#!/usr/bin/env bash
# Host firewall rules for the services that reach the internet (ADR-0023, M5-03, F-04, F-06).
#
#   deploy/egress/pharos-egress.sh print     show the rules, change nothing
#   deploy/egress/pharos-egress.sh apply     put them in place (as root), replacing earlier ones
#   deploy/egress/pharos-egress.sh remove    take them out again
#
# Only `api` and `worker` are on the `egress` network; it has a fixed range and they have fixed
# addresses on it (deploy/docker-compose.yml). From that range:
#   api      TCP 443 to public addresses only (the identity provider and Microsoft Graph; a firewall
#            cannot name them, so "public, 443" is the rule)
#   worker   TCP 80 and 443 to public addresses only (feeds, the model provider)
#   anything else from the range, and every private, loopback, link-local (cloud metadata), shared
#   or multicast destination and Azure's WireServer (168.63.129.16): dropped
# And from every Pharos network (egress, app, data, llm), the host itself (each network's gateway
# address and every other address of the host): dropped, except answers and the host's resolvers
# on port 53. A container on an internal network still reaches the host through that network's
# gateway, so all four ranges are named (review of 2026-11-14).
#
# Answers to connections already allowed pass. DNS: the containers ask Docker's own resolver, which
# may pass a question on from the container's own network namespace, so the host's resolvers are
# allowed on port 53, before anything is dropped (a company resolver is often private, or the host
# itself). They are read from PHAROS_EGRESS_DNS (addresses separated by spaces or commas) or,
# without it, from the resolv.conf Docker uses; loopback resolvers need no rule.
#
# The rules live in two chains of their own, PHAROS-EGRESS (reached from DOCKER-USER, which Docker
# keeps and evaluates first for forwarded traffic) and PHAROS-EGRESS-HOST (reached from INPUT).
# `apply` builds the new chains beside the old ones and hooks them in before it unhooks the old
# ones, so there is no moment without rules; a failure on the way leaves the old rules in place.
# They are not saved across a reboot: persist them the way this host does (netfilter-persistent, a
# systemd unit), and run scripts/egress-check.sh after every start of the stack and every change.
# IPv6 is off on Pharos's networks; if you switch it on, add the same rules with ip6tables first.
#
# The ranges come from the environment or deploy/.env, with the Compose defaults: PHAROS_EGRESS_SUBNET,
# PHAROS_EGRESS_API_IP, PHAROS_EGRESS_WORKER_IP, PHAROS_APP_SUBNET, PHAROS_DATA_SUBNET and
# PHAROS_OLLAMA_SUBNET. PHAROS_EGRESS_FORWARD_CHAIN, PHAROS_EGRESS_RESOLV and PHAROS_IPTABLES exist
# for the tests (scripts/egress-netns-test.sh); leave them alone.
set -euo pipefail
cd "$(dirname "$0")/../.."

read_env() {
  local name=$1 default=$2 value=
  value=${!name:-}
  if [ -z "$value" ] && [ -f deploy/.env ]; then
    value=$(sed -n "s/^${name}=//p" deploy/.env | tail -n 1)
  fi
  printf '%s' "${value:-$default}"
}

SUBNET=$(read_env PHAROS_EGRESS_SUBNET 172.28.40.0/24)
API_IP=$(read_env PHAROS_EGRESS_API_IP 172.28.40.250)
WORKER_IP=$(read_env PHAROS_EGRESS_WORKER_IP 172.28.40.251)
APP_SUBNET=$(read_env PHAROS_APP_SUBNET 172.28.20.0/24)
DATA_SUBNET=$(read_env PHAROS_DATA_SUBNET 172.28.50.0/24)
LLM_SUBNET=$(read_env PHAROS_OLLAMA_SUBNET 172.28.30.0/24)
FORWARD_CHAIN=${PHAROS_EGRESS_FORWARD_CHAIN:-DOCKER-USER}
IPTABLES=${PHAROS_IPTABLES:-iptables}
RESOLV=${PHAROS_EGRESS_RESOLV:-}
if [ -z "$RESOLV" ]; then
  RESOLV=/etc/resolv.conf
  [ -f /run/systemd/resolve/resolv.conf ] && RESOLV=/run/systemd/resolve/resolv.conf
fi
DNS=${PHAROS_EGRESS_DNS:-$(awk '$1 == "nameserver" { print $2 }' "$RESOLV" 2>/dev/null || true)}

octet='(25[0-5]|2[0-4][0-9]|1[0-9][0-9]|[1-9]?[0-9])'
ipv4="^$octet\\.$octet\\.$octet\\.$octet\$"
as_number() {
  local IFS=. a b c d
  read -r a b c d <<<"$1"
  echo $(((a << 24) | (b << 16) | (c << 8) | d))
}
for range in "$SUBNET" "$APP_SUBNET" "$DATA_SUBNET" "$LLM_SUBNET"; do
  [[ ${range%/*} =~ $ipv4 && ${range#*/} =~ ^([8-9]|[12][0-9]|30)$ ]] \
    || { echo "not an IPv4 range: $range" >&2; exit 2; }
done
for address in "$API_IP" "$WORKER_IP"; do
  [[ $address =~ $ipv4 ]] || { echo "not an IPv4 address: $address" >&2; exit 2; }
  # Inside the egress range, or the rules would name an address the services never have.
  bits=${SUBNET#*/}
  mask=$(((0xFFFFFFFF << (32 - bits)) & 0xFFFFFFFF))
  if (( ($(as_number "$address") & mask) != ($(as_number "${SUBNET%/*}") & mask) )); then
    echo "$address is not inside $SUBNET" >&2
    exit 2
  fi
done
[[ $FORWARD_CHAIN =~ ^[A-Z][A-Z0-9-]{0,28}$ ]] || { echo "not a chain name" >&2; exit 2; }
RESOLVERS=()
for address in ${DNS//,/ }; do
  case "$address" in
    127.* | *:*) continue ;; # loopback (Docker asks it itself) or IPv6 (off on these networks)
  esac
  [[ $address =~ $ipv4 ]] || { echo "not an IPv4 resolver: $address" >&2; exit 2; }
  RESOLVERS+=("$address")
done

# Not the internet: RFC 6890 special-purpose ranges a fetch or a token request has no reason to
# reach, and Azure's WireServer (a global address that answers the VM agent and the platform's DNS).
NOT_PUBLIC=(
  0.0.0.0/8 10.0.0.0/8 100.64.0.0/10 127.0.0.0/8 169.254.0.0/16 172.16.0.0/12 192.0.0.0/24
  192.168.0.0/16 198.18.0.0/15 224.0.0.0/4 240.0.0.0/4 168.63.129.16/32
)
PHAROS_RANGES=("$SUBNET" "$APP_SUBNET" "$DATA_SUBNET" "$LLM_SUBNET")

# The rules, for chains named with a suffix (`apply` builds new ones beside the old ones).
rules() {
  local forward="PHAROS-EGRESS$1" host="PHAROS-EGRESS-HOST$1" range resolver
  echo "-N $forward"
  echo "-A $forward -m conntrack --ctstate ESTABLISHED,RELATED -j RETURN"
  for resolver in ${RESOLVERS[@]+"${RESOLVERS[@]}"}; do
    echo "-A $forward -d $resolver -p udp --dport 53 -j RETURN"
    echo "-A $forward -d $resolver -p tcp --dport 53 -j RETURN"
  done
  for range in "${NOT_PUBLIC[@]}"; do
    echo "-A $forward -d $range -j DROP"
  done
  echo "-A $forward -s $API_IP -p tcp --dport 443 -j RETURN"
  echo "-A $forward -s $WORKER_IP -p tcp -m multiport --dports 80,443 -j RETURN"
  echo "-A $forward -j DROP"
  echo "-N $host"
  echo "-A $host -m conntrack --ctstate ESTABLISHED,RELATED -j RETURN"
  for resolver in ${RESOLVERS[@]+"${RESOLVERS[@]}"}; do
    echo "-A $host -d $resolver -p udp --dport 53 -j RETURN"
    echo "-A $host -d $resolver -p tcp --dport 53 -j RETURN"
  done
  echo "-A $host -j DROP"
  echo "-I $FORWARD_CHAIN 1 -s $SUBNET -j $forward"
  for range in "${PHAROS_RANGES[@]}"; do
    echo "-I INPUT 1 -s $range -j $host"
  done
}

ipt() { "$IPTABLES" -w "$@"; }

# Every jump into the chains of one suffix, whatever range a former run named, then the chains.
unhook() {
  local suffix=$1 chain rule
  for chain in "$FORWARD_CHAIN" INPUT; do
    while read -r rule; do
      # shellcheck disable=SC2086  # a rule as iptables printed it, its words split on purpose
      ipt ${rule/-A/-D}
    done < <(ipt -S "$chain" 2>/dev/null | grep -E -- "-j PHAROS-EGRESS(-HOST)?$suffix\$" || true)
  done
  for chain in "PHAROS-EGRESS$suffix" "PHAROS-EGRESS-HOST$suffix"; do
    if ipt -n -L "$chain" >/dev/null 2>&1; then
      ipt -F "$chain"
      ipt -X "$chain"
    fi
  done
}

apply() {
  ipt -n -L "$FORWARD_CHAIN" >/dev/null 2>&1 || {
    echo "chain $FORWARD_CHAIN does not exist: is Docker running on this host?" >&2
    exit 1
  }
  unhook -NEW # what a failed run may have left
  local rule
  while read -r rule; do
    # shellcheck disable=SC2086  # each rule is a fixed list of words made above
    if ! ipt $rule; then
      unhook -NEW
      echo "apply failed at: $rule; the earlier rules, if any, are still in place" >&2
      exit 1
    fi
  done < <(rules -NEW)
  unhook "" # the old chains: the new ones already guard every range
  ipt -E PHAROS-EGRESS-NEW PHAROS-EGRESS
  ipt -E PHAROS-EGRESS-HOST-NEW PHAROS-EGRESS-HOST
  echo "applied: $(rules "" | grep -c -- '-A ') rules for $SUBNET (api $API_IP, worker $WORKER_IP)" \
    "and the host guarded from ${#PHAROS_RANGES[@]} ranges"
}

case "${1:-}" in
  print) rules "" ;;
  apply) apply ;;
  remove) unhook -NEW && unhook "" && echo "removed" ;;
  *) echo "usage: $0 print|apply|remove" >&2; exit 2 ;;
esac
