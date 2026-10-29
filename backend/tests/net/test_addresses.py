"""Which addresses a fetcher may connect to: those refused, and some that must be accepted."""

import ipaddress

import pytest

from pharos.net.addresses import DENIED_NETWORKS, AddressPolicy, parse_networks

POLICY = AddressPolicy()

DENIED = [
    # IPv4: loopback, private, link-local (the cloud metadata address), "this network", broadcast
    "127.0.0.1",
    "127.255.255.255",
    "10.0.0.1",
    "10.255.255.255",
    "172.16.0.1",
    "172.31.255.255",
    "192.168.1.1",
    "169.254.169.254",
    "169.254.0.1",
    "0.0.0.0",  # noqa: S104  (a test value, nothing binds to it)
    "0.1.2.3",
    "255.255.255.255",
    # IPv4: carrier-grade NAT, protocol assignments (192.0.0.9/.10 are wrongly "global" before
    # Python 3.12.4), documentation, benchmarking, 6to4 relay, multicast, reserved
    "100.64.0.1",
    "100.127.255.254",
    "192.0.0.1",
    "192.0.0.9",
    "192.0.0.10",
    "192.0.2.1",
    "198.18.0.1",
    "198.19.255.255",
    "198.51.100.1",
    "203.0.113.1",
    "192.88.99.1",
    "224.0.0.1",
    "239.255.255.255",
    "240.0.0.1",
    # IPv6
    "::",
    "::1",
    "::7f00:1",  # IPv4-compatible towards 127.0.0.1
    "::102:304",
    "::ffff:127.0.0.1",
    "::ffff:10.0.0.1",
    "::ffff:169.254.169.254",
    "::ffff:7f00:1",
    "64:ff9b::7f00:1",  # NAT64 towards 127.0.0.1
    "64:ff9b::a00:1",  # NAT64 towards 10.0.0.1
    "64:ff9b:1::1",
    "100::1",
    "2001::1",  # Teredo
    "2001:0:4136:e378:8000:63bf:3fff:fdd2",
    "2001:db8::1",
    "2002:7f00:1::",  # 6to4 towards 127.0.0.1
    "2002:a00:1::",
    "fc00::1",
    "fd12:3456::1",
    "fe80::1",
    "fec0::1",
    "ff02::1",
    "ff05::1:3",
]

ALLOWED = [
    "8.8.8.8",
    "1.1.1.1",
    "93.184.216.34",
    "140.82.112.3",
    "151.101.1.69",
    "172.32.0.1",  # just outside 172.16.0.0/12
    "100.128.0.1",  # just outside 100.64.0.0/10
    "2606:4700:4700::1111",
    "2001:4860:4860::8888",
    "2620:fe::fe",
    "::ffff:8.8.8.8",  # a public IPv4 address in mapped form
    "::ffff:808:808",
]


@pytest.mark.parametrize("address", DENIED)
def test_unsafe_addresses_are_refused(address: str) -> None:
    assert POLICY.violation(address) is not None, address


@pytest.mark.parametrize("address", ALLOWED)
def test_public_addresses_are_accepted(address: str) -> None:
    assert POLICY.violation(address) is None, address


@pytest.mark.parametrize("address", ["2001:10::1", "2001:2::1"])
def test_a_non_global_address_outside_the_deny_list_is_still_refused(address: str) -> None:
    # Not in DENIED_NETWORKS, so only the allow-list rule (`is_global`) can stop it.
    ip = ipaddress.ip_address(address)
    assert not any(ip in network for network in DENIED_NETWORKS)
    assert POLICY.violation(address) == "not_global"


def test_the_lists_cover_every_denied_range() -> None:
    # Each explicitly denied range has at least one example above, so deleting one fails a test.
    examples = [ipaddress.ip_address(a) for a in DENIED]
    for network in DENIED_NETWORKS:
        assert any(e.version == network.version and e in network for e in examples), network


def test_a_name_with_one_bad_address_is_refused_as_a_whole() -> None:
    assert POLICY.violations(["93.184.216.34", "10.0.0.1"]) is not None
    assert POLICY.violations(["10.0.0.1", "93.184.216.34"]) is not None
    assert POLICY.violations(["93.184.216.34", "2606:4700:4700::1111"]) is None


def test_no_address_at_all_is_refused() -> None:
    assert POLICY.violations([]) == "no_address"


def test_the_reason_names_the_range() -> None:
    assert POLICY.violation("169.254.169.254") == "denied_range:169.254.0.0/16"
    assert POLICY.violation("::ffff:10.1.2.3") == "denied_range:10.0.0.0/8"


def test_a_mapped_address_is_judged_by_the_address_inside_it() -> None:
    assert POLICY.violation("::ffff:93.184.216.34") is None
    assert POLICY.violation("::ffff:192.168.0.1") is not None


def test_text_and_parsed_addresses_give_the_same_answer() -> None:
    for text in DENIED + ALLOWED:
        assert POLICY.violation(text) == POLICY.violation(ipaddress.ip_address(text))


def test_an_extra_allowance_opens_only_what_it_names() -> None:
    policy = AddressPolicy(parse_networks(["127.0.0.1/32"]))
    assert policy.violation("127.0.0.1") is None
    assert policy.violation("127.0.0.2") is not None
    assert policy.violation("10.0.0.1") is not None
    assert AddressPolicy().violation("127.0.0.1") is not None  # the default policy is unchanged


def test_parse_networks_refuses_host_bits() -> None:
    with pytest.raises(ValueError, match="host bits"):
        parse_networks(["10.0.0.1/8"])
