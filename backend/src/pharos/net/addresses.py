"""Which network addresses a fetcher may connect to.

The rule is an allow-list: an address is acceptable only if it is a global unicast address
(`is_global`) and not multicast. On top of that a list of ranges is denied explicitly, for three
reasons: some are wrongly reported as global by older Python versions (192.0.0.9 and 192.0.0.10
before 3.12.4, CVE-2024-4032), some embed an IPv4 address that could be anything (NAT64, 6to4,
Teredo), and the list documents the intent. A hostname is accepted only if EVERY address it
resolves to is acceptable.
"""

import ipaddress
from collections.abc import Iterable

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
Network = ipaddress.IPv4Network | ipaddress.IPv6Network

_DENIED: tuple[str, ...] = (
    # IPv4
    "0.0.0.0/8",  # "this network"
    "10.0.0.0/8",
    "100.64.0.0/10",  # carrier-grade NAT
    "127.0.0.0/8",
    "169.254.0.0/16",  # link-local, including the cloud metadata address
    "172.16.0.0/12",
    "192.0.0.0/24",  # IETF protocol assignments
    "192.0.2.0/24",
    "192.88.99.0/24",
    "192.168.0.0/16",
    "198.18.0.0/15",  # benchmarking
    "198.51.100.0/24",
    "203.0.113.0/24",
    "224.0.0.0/4",  # multicast
    "240.0.0.0/4",  # reserved, includes the broadcast address
    # IPv6
    "::/96",  # IPv4-compatible (deprecated) and the unspecified and loopback addresses
    "::/128",
    "::1/128",
    "::ffff:0:0/96",  # IPv4-mapped: the mapped address is checked as IPv4 instead
    "64:ff9b::/96",  # NAT64: reaches any IPv4 address
    "64:ff9b:1::/48",  # local-use NAT64
    "100::/64",  # discard-only
    "2001::/32",  # Teredo
    "2001:db8::/32",  # documentation
    "2002::/16",  # 6to4
    "fc00::/7",  # unique local
    "fe80::/10",  # link-local
    "fec0::/10",  # deprecated site-local
    "ff00::/8",  # multicast
)
DENIED_NETWORKS: tuple[Network, ...] = tuple(ipaddress.ip_network(n) for n in _DENIED)


def parse_networks(values: Iterable[str]) -> tuple[Network, ...]:
    return tuple(ipaddress.ip_network(value, strict=True) for value in values)


class AddressPolicy:
    """Decides about one resolved address. Immutable; build it with `policy_from_settings`."""

    def __init__(self, extra_allowed: tuple[Network, ...] = ()) -> None:
        # `extra_allowed` exists for tests that need a fake server on the loopback or a Docker
        # address. It is only ever filled from PHAROS_FETCH_TEST_ALLOWED_NETWORKS, which Settings
        # refuses outside the test environment; it is not a parameter anyone should add elsewhere.
        self._extra_allowed = extra_allowed

    def violation(self, address: IPAddress | str) -> str | None:
        """None if the address may be used, otherwise a short reason (for logs and tests)."""
        ip = ipaddress.ip_address(address) if isinstance(address, str) else address
        if any(ip in network for network in self._extra_allowed):
            return None
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
            return self.violation(ip.ipv4_mapped)
        for network in DENIED_NETWORKS:
            if ip.version == network.version and ip in network:
                return f"denied_range:{network}"
        if ip.is_multicast:
            return "multicast"
        if not ip.is_global:
            return "not_global"
        return None

    def violations(self, addresses: Iterable[IPAddress | str]) -> str | None:
        """The first reason any of the addresses is unacceptable, or None. Empty is unacceptable."""
        seen = False
        for address in addresses:
            seen = True
            reason = self.violation(address)
            if reason is not None:
                return reason
        return None if seen else "no_address"
