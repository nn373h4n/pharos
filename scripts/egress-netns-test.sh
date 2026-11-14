#!/usr/bin/env bash
# Proves deploy/egress/pharos-egress.sh in network namespaces, never on this host's own firewall
# (ADR-0023, M5-03). Needs root, ip, iptables and python3. It builds, for a few seconds:
#
#   (api and worker are also on a second network, 172.28.50.0/24, where the host is 172.28.50.1)
#   <p>-api     172.28.40.250 \                                  / 198.51.100.10 (public: 80, 443, 25)
#                              > <p>-host (router, the bridge   <  10.255.255.1 (private: 80)
#   <p>-worker  172.28.40.251 /    gateway 172.28.40.1:18999)    \ 169.254.169.254 (metadata: 80)
#                                                                  10.255.255.53 (a resolver: 53/udp)
#                                         <p>-net holds every address on the right, with listeners.
#
# Phase 1, no rules: every call from api and worker connects (the listeners exist, the routes work).
# Phase 2, the rules applied in <p>-host (a DOCKER-USER chain stands in for Docker's): the matrix of
# scripts/egress-check.sh. Phase 3, the rules removed: everything connects again. All namespaces,
# their files and processes are removed at the end, also on failure. The host's own firewall,
# routes and interfaces are never touched. What this cannot show: Docker's own rules and its DNS
# forwarding on a real host; that is gate G7 (scripts/egress-check.sh on the pilot host).
set -euo pipefail
cd "$(dirname "$0")/.."

[ "$(id -u)" = 0 ] || { echo "needs root (it creates network namespaces)" >&2; exit 2; }
for tool in ip iptables python3; do
  command -v "$tool" >/dev/null || { echo "needs $tool" >&2; exit 2; }
done

P="pe$$"
HOST="$P-host"
API="$P-api"
WORKER="$P-worker"
NET="$P-net"
SUBNET=172.28.40.0/24
pids=()

cleanup() {
  for pid in ${pids[@]+"${pids[@]}"}; do kill "$pid" 2>/dev/null || true; done
  for ns in "$API" "$WORKER" "$NET" "$HOST"; do
    ip netns del "$ns" 2>/dev/null || true
    rm -rf "/etc/netns/$ns"
  done
}
trap cleanup EXIT

in_ns() { local ns=$1; shift; ip netns exec "$ns" "$@"; }

for ns in "$HOST" "$API" "$WORKER" "$NET"; do
  ip netns add "$ns"
  in_ns "$ns" ip link set lo up
done
in_ns "$HOST" sysctl -qw net.ipv4.ip_forward=1

# The egress "bridge" in the router, with api and worker on it.
ip -n "$HOST" link add br0 type bridge
ip -n "$HOST" addr add 172.28.40.1/24 dev br0
ip -n "$HOST" link set br0 up
# A second network (as `data`): the host has an address on it too, which the rules must guard.
ip -n "$HOST" link add br1 type bridge
ip -n "$HOST" addr add 172.28.50.1/24 dev br1
ip -n "$HOST" link set br1 up
for pair in "api:$API:172.28.40.250:172.28.50.10" "worker:$WORKER:172.28.40.251:172.28.50.11"; do
  IFS=: read -r name ns address data <<<"$pair"
  ip -n "$HOST" link add "d-$name" type veth peer name eth1 netns "$ns"
  ip -n "$HOST" link set "d-$name" master br1 up
  ip -n "$ns" addr add "$data/24" dev eth1
  ip -n "$ns" link set eth1 up
  ip -n "$HOST" link add "v-$name" type veth peer name eth0 netns "$ns"
  ip -n "$HOST" link set "v-$name" master br0 up
  ip -n "$ns" addr add "$address/24" dev eth0
  ip -n "$ns" link set eth0 up
  ip -n "$ns" route add default via 172.28.40.1
  # Names are asked of the private resolver (ip netns exec bind-mounts this file).
  mkdir -p "/etc/netns/$ns"
  echo "nameserver 10.255.255.53" >"/etc/netns/$ns/resolv.conf"
done

# "The internet" and the private addresses, behind a link of their own.
ip -n "$HOST" link add v-net type veth peer name eth0 netns "$NET"
ip -n "$HOST" addr add 192.0.2.1/30 dev v-net
ip -n "$HOST" link set v-net up
ip -n "$NET" addr add 192.0.2.2/30 dev eth0
ip -n "$NET" link set eth0 up
ip -n "$NET" route add "$SUBNET" via 192.0.2.1
for address in 198.51.100.10 10.255.255.1 169.254.169.254 168.63.129.16 10.255.255.53; do
  ip -n "$NET" addr add "$address/32" dev lo
  ip -n "$HOST" route add "$address/32" via 192.0.2.2
done

# Listeners: TCP on every target, and a resolver that answers every A question with the public
# address.
# Started with `ip netns exec` itself, not the shell function, so $! is the listener and the
# clean-up can stop it.
ip netns exec "$NET" python3 -c '
import selectors, socket
sel = selectors.DefaultSelector()
for host, port in [("198.51.100.10", 80), ("198.51.100.10", 443), ("198.51.100.10", 25),
                   ("10.255.255.1", 80), ("169.254.169.254", 80), ("168.63.129.16", 80)]:
    s = socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind((host, port)); s.listen(16); s.setblocking(False)
    sel.register(s, selectors.EVENT_READ)
dns = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); dns.bind(("10.255.255.53", 53))
dns.setblocking(False); sel.register(dns, selectors.EVENT_READ)
import time
until = time.time() + 300  # a second guard: gone after five minutes whatever happens
while time.time() < until:
    for key, _ in sel.select(1):
        s = key.fileobj
        if s is dns:
            q, peer = dns.recvfrom(512)
            if len(q) < 12:
                continue
            end = 12
            while end < len(q) and q[end] != 0:
                end += q[end] + 1
            question = q[12:end + 5]
            qtype = q[end + 1:end + 3]
            answers = 1 if qtype == b"\x00\x01" else 0
            head = q[:2] + b"\x81\x80" + b"\x00\x01" + answers.to_bytes(2, "big") + b"\x00\x00\x00\x00"
            body = question + (b"\xc0\x0c\x00\x01\x00\x01\x00\x00\x00\x3c\x00\x04" + bytes([198, 51, 100, 10]) if answers else b"")
            dns.sendto(head + body, peer)
        else:
            conn, _ = s.accept(); conn.close()
' &
pids+=($!)
ip netns exec "$HOST" python3 -c '
import socket, time
socks = []
for address in ("172.28.40.1", "172.28.50.1"):
    s = socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind((address, 18999)); s.listen(16); s.settimeout(0.2); socks.append(s)
until = time.time() + 300  # a second guard: gone after five minutes whatever happens
while time.time() < until:
    for s in socks:
        try:
            s.accept()[0].close()
        except OSError:
            pass
' &
pids+=($!)
sleep 1

# Docker's chain, which the rules hook into.
in_ns "$HOST" iptables -w -N DOCKER-USER
in_ns "$HOST" iptables -w -A FORWARD -j DOCKER-USER

probe() {
  in_ns "$1" python3 -c "
import socket
try:
    socket.create_connection(('$2', $3), timeout=3).close(); print('open')
except OSError:
    print('closed')"
}

failed=0
all_open() {
  local phase=$1 ns target
  for ns in "$API" "$WORKER"; do
    for target in 198.51.100.10:443 198.51.100.10:80 198.51.100.10:25 10.255.255.1:80 \
      169.254.169.254:80 168.63.129.16:80 172.28.40.1:18999 172.28.50.1:18999; do
      if [ "$(probe "$ns" "${target%:*}" "${target#*:}")" = open ]; then
        printf '%-12s %-7s %s\n' PASS "${ns#"$P"-}" "$target open ($phase)"
      else
        printf '%-12s %-7s %s\n' FAIL "${ns#"$P"-}" "$target closed ($phase): the test network is broken"
        failed=1
      fi
    done
  done
}

check() {
  EGRESS_RUN_API="ip netns exec $API" EGRESS_RUN_WORKER="ip netns exec $WORKER" \
    EGRESS_PUBLIC=198.51.100.10 EGRESS_PRIVATE=10.255.255.1 \
    EGRESS_GATEWAYS="egress=172.28.40.1 data=172.28.50.1" \
    EGRESS_GATEWAY_PORT=18999 EGRESS_LISTENERS=yes EGRESS_NAME=example.com \
    PHAROS_EGRESS_SUBNET="$SUBNET" PHAROS_DATA_SUBNET=172.28.50.0/24 scripts/egress-check.sh
}

echo "== phase 1: no rules, every call connects (controls)"
all_open "no rules"

echo "== phase 2: the rules"
# First as if the range had been another one: the next apply must leave no jump of the old range.
in_ns "$HOST" env PHAROS_EGRESS_DNS=10.255.255.53 PHAROS_EGRESS_SUBNET=172.28.99.0/24 \
  PHAROS_EGRESS_API_IP=172.28.99.250 PHAROS_EGRESS_WORKER_IP=172.28.99.251 \
  deploy/egress/pharos-egress.sh apply >/dev/null
in_ns "$HOST" env PHAROS_EGRESS_DNS=10.255.255.53 PHAROS_EGRESS_SUBNET="$SUBNET" \
  PHAROS_EGRESS_API_IP=172.28.40.250 PHAROS_EGRESS_WORKER_IP=172.28.40.251 \
  deploy/egress/pharos-egress.sh apply
# Applied twice is the same as once: the script replaces its earlier rules.
in_ns "$HOST" env PHAROS_EGRESS_DNS=10.255.255.53 PHAROS_EGRESS_SUBNET="$SUBNET" \
  PHAROS_EGRESS_API_IP=172.28.40.250 PHAROS_EGRESS_WORKER_IP=172.28.40.251 \
  deploy/egress/pharos-egress.sh apply >/dev/null
jumps=$(in_ns "$HOST" iptables -w -S DOCKER-USER | grep -c -- "-j PHAROS-EGRESS" || true)
stale=$(in_ns "$HOST" iptables -w -S | grep -c -- "172.28.99" || true)
chains=$(in_ns "$HOST" iptables -w -S | grep -c -- "^-N PHAROS" || true)
if [ "$jumps" = 1 ] && [ "$stale" = 0 ] && [ "$chains" = 2 ]; then
  printf '%-12s %-7s %s\n' PASS host "applied three times, the last with a changed range: one jump, two chains, nothing of the old range"
else
  printf '%-12s %-7s %s\n' FAIL host "after three applies: $jumps jumps, $chains chains, $stale lines of the old range"
  failed=1
fi
check || failed=1

echo "== phase 3: the rules removed, every call connects again"
in_ns "$HOST" env PHAROS_EGRESS_SUBNET="$SUBNET" deploy/egress/pharos-egress.sh remove >/dev/null
left=$(in_ns "$HOST" iptables -w -S | grep -c -- "PHAROS" || true)
if [ "$left" = 0 ]; then
  printf '%-12s %-7s %s\n' PASS host "removed: no chain and no jump left"
else
  printf '%-12s %-7s %s\n' FAIL host "removed, but $left lines of the rules are left"
  failed=1
fi
all_open "rules removed"

exit "$failed"
