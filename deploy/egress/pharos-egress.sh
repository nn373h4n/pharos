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
#   or multicast destination: dropped
#   the host itself (the bridge gateway and every address of the host): dropped, except answers
# Answers to connections already allowed pass. DNS: the containers ask Docker's own resolver, which
# may pass a question on from the container's own network namespace, so the host's resolvers are
# allowed on port 53, before the private ranges are dropped (a company resolver is often private).
# They are read from PHAROS_EGRESS_DNS (addresses separated by spaces or commas) or, without it,
# from the resolv.conf Docker uses; loopback resolvers need no rule (Docker asks them itself).
#
# The rules live in two chains of their own, PHAROS-EGRESS (reached from DOCKER-USER, which Docker
# keeps and evaluates first for forwarded traffic) and PHAROS-EGRESS-HOST (reached from INPUT).
# They are not saved across a reboot: persist them the way this host does (netfilter-persistent, a
# systemd unit), and run scripts/egress-check.sh after every start of the stack and every change.
# IPv6 is off on Pharos's networks; if you switch it on, add the same rules with ip6tables first.
#
# The addresses come from the environment or deploy/.env (PHAROS_EGRESS_SUBNET, PHAROS_EGRESS_API_IP,
# PHAROS_EGRESS_WORKER_IP), with the Compose defaults. PHAROS_EGRESS_FORWARD_CHAIN and
# PHAROS_IPTABLES exist for the namespace test (scripts/egress-netns-test.sh); leave them alone.
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
FORWARD_CHAIN=${PHAROS_EGRESS_FORWARD_CHAIN:-DOCKER-USER}
RESOLV=${PHAROS_EGRESS_RESOLV:-}
if [ -z "$RESOLV" ]; then
  RESOLV=/etc/resolv.conf
  [ -f /run/systemd/resolve/resolv.conf ] && RESOLV=/run/systemd/resolve/resolv.conf
fi
DNS=${PHAROS_EGRESS_DNS:-$(awk '$1 == "nameserver" { print $2 }' "$RESOLV" 2>/dev/null || true)}
IPTABLES=${PHAROS_IPTABLES:-iptables}

octet='(25[0-5]|2[0-4][0-9]|1[0-9][0-9]|[1-9]?[0-9])'
ipv4="^$octet\\.$octet\\.$octet\\.$octet\$"
[[ ${SUBNET%/*} =~ $ipv4 && ${SUBNET#*/} =~ ^([8-9]|[12][0-9]|30)$ ]] \
  || { echo "PHAROS_EGRESS_SUBNET is not an IPv4 range: $SUBNET" >&2; exit 2; }
for address in "$API_IP" "$WORKER_IP"; do
  [[ $address =~ $ipv4 ]] || { echo "not an IPv4 address: $address" >&2; exit 2; }
done
[[ $FORWARD_CHAIN =~ ^[A-Z][A-Z0-9-]{0,28}$ ]] || { echo "not a chain name" >&2; exit 2; }
RESOLVERS=()
for address in ${DNS//,/ }; do
  case "$address" in
    127.* | *:*) continue ;;  # loopback (Docker asks it itself) or IPv6 (off on these networks)
  esac
  [[ $address =~ $ipv4 ]] || { echo "not an IPv4 resolver: $address" >&2; exit 2; }
  RESOLVERS+=("$address")
done

# Not the internet: RFC 6890 special-purpose ranges a fetch or a token request has no reason to reach.
NOT_PUBLIC=(
  0.0.0.0/8 10.0.0.0/8 100.64.0.0/10 127.0.0.0/8 169.254.0.0/16 172.16.0.0/12 192.0.0.0/24
  192.168.0.0/16 198.18.0.0/15 224.0.0.0/4 240.0.0.0/4
)

rules() {
  # PHAROS-EGRESS: forwarded traffic from the egress range.
  echo "-N PHAROS-EGRESS"
  echo "-A PHAROS-EGRESS -m conntrack --ctstate ESTABLISHED,RELATED -j RETURN"
  for resolver in ${RESOLVERS[@]+"${RESOLVERS[@]}"}; do
    echo "-A PHAROS-EGRESS -d $resolver -p udp --dport 53 -j RETURN"
    echo "-A PHAROS-EGRESS -d $resolver -p tcp --dport 53 -j RETURN"
  done
  for range in "${NOT_PUBLIC[@]}"; do
    echo "-A PHAROS-EGRESS -d $range -j DROP"
  done
  echo "-A PHAROS-EGRESS -s $API_IP -p tcp --dport 443 -j RETURN"
  echo "-A PHAROS-EGRESS -s $WORKER_IP -p tcp -m multiport --dports 80,443 -j RETURN"
  echo "-A PHAROS-EGRESS -j DROP"
  # PHAROS-EGRESS-HOST: the host's own addresses, the bridge gateway among them.
  echo "-N PHAROS-EGRESS-HOST"
  echo "-A PHAROS-EGRESS-HOST -m conntrack --ctstate ESTABLISHED,RELATED -j RETURN"
  echo "-A PHAROS-EGRESS-HOST -j DROP"
  # The jumps, first in their chains.
  echo "-I $FORWARD_CHAIN 1 -s $SUBNET -j PHAROS-EGRESS"
  echo "-I INPUT 1 -s $SUBNET -j PHAROS-EGRESS-HOST"
}

ipt() { "$IPTABLES" -w "$@"; }

remove() {
  # Every jump into the two chains, however many runs put there, then the chains themselves.
  while ipt -D "$FORWARD_CHAIN" -s "$SUBNET" -j PHAROS-EGRESS 2>/dev/null; do :; done
  while ipt -D INPUT -s "$SUBNET" -j PHAROS-EGRESS-HOST 2>/dev/null; do :; done
  for chain in PHAROS-EGRESS PHAROS-EGRESS-HOST; do
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
  remove
  local rule
  while read -r rule; do
    # shellcheck disable=SC2086  # each rule is a fixed list of words made above
    ipt $rule
  done < <(rules)
  echo "applied: $(rules | grep -c -- '-A ') rules for $SUBNET (api $API_IP, worker $WORKER_IP)"
}

case "${1:-}" in
  print) rules ;;
  apply) apply ;;
  remove) remove && echo "removed" ;;
  *) echo "usage: $0 print|apply|remove" >&2; exit 2 ;;
esac
