"""The egress rules and their check (ADR-0023, M5-03), with iptables and the services stubbed.

`deploy/egress/pharos-egress.sh` is tested for the rules it writes and the order it writes them in;
`scripts/egress-check.sh` for its verdicts. The rules themselves are proven against real packets by
`scripts/egress-netns-test.sh` in network namespaces (the last test here, root only).
"""

import os
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
RULES = ROOT / "deploy/egress/pharos-egress.sh"
CHECK = ROOT / "scripts/egress-check.sh"
NETNS = ROOT / "scripts/egress-netns-test.sh"


def rules(
    tmp_path: Path, *args: str, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    resolv = tmp_path / "resolv.conf"
    if not resolv.exists():
        resolv.write_text("nameserver 127.0.0.53\n")
    return subprocess.run(  # noqa: S603
        [str(RULES), *args],
        env={"PATH": os.environ["PATH"], "PHAROS_EGRESS_RESOLV": str(resolv), **(env or {})},
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_the_rules_allow_https_for_the_api_and_http_for_the_worker_and_nothing_else(
    tmp_path: Path,
) -> None:
    out = rules(tmp_path, "print").stdout.splitlines()
    assert "-A PHAROS-EGRESS -s 172.28.40.250 -p tcp --dport 443 -j RETURN" in out
    assert "-A PHAROS-EGRESS -s 172.28.40.251 -p tcp -m multiport --dports 80,443 -j RETURN" in out
    chain = [line for line in out if line.startswith("-A PHAROS-EGRESS ")]
    assert chain[0].endswith("--ctstate ESTABLISHED,RELATED -j RETURN")
    assert chain[-1] == "-A PHAROS-EGRESS -j DROP"  # everything else from the range
    # Every private, loopback, metadata, shared and multicast range is dropped before any allow.
    drops = [line.split()[3] for line in chain if line.endswith("-j DROP") and " -d " in line]
    for needed in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16",
                   "127.0.0.0/8", "100.64.0.0/10", "224.0.0.0/4"):  # fmt: skip
        assert needed in drops
    first_allow = next(i for i, line in enumerate(chain) if " -s " in line)
    last_drop = max(i for i, line in enumerate(chain) if " -d " in line and "DROP" in line)
    assert last_drop < first_allow
    assert "168.63.129.16/32" in drops  # Azure's WireServer
    assert "-A PHAROS-EGRESS-HOST -j DROP" in out  # the host and its bridge gateways
    # The host is guarded from every Pharos network, not only egress: an internal network still
    # reaches it through its gateway (review of 2026-11-14).
    assert out[-5:] == [
        "-I DOCKER-USER 1 -s 172.28.40.0/24 -j PHAROS-EGRESS",
        "-I INPUT 1 -s 172.28.40.0/24 -j PHAROS-EGRESS-HOST",
        "-I INPUT 1 -s 172.28.20.0/24 -j PHAROS-EGRESS-HOST",
        "-I INPUT 1 -s 172.28.50.0/24 -j PHAROS-EGRESS-HOST",
        "-I INPUT 1 -s 172.28.30.0/24 -j PHAROS-EGRESS-HOST",
    ]


def test_resolvers_are_allowed_on_53_before_the_private_ranges_and_loopback_ones_need_nothing(
    tmp_path: Path,
) -> None:
    out = rules(tmp_path, "print", env={"PHAROS_EGRESS_DNS": "10.1.1.53, 127.0.0.53"}).stdout
    lines = out.splitlines()
    udp = lines.index("-A PHAROS-EGRESS -d 10.1.1.53 -p udp --dport 53 -j RETURN")
    assert "-A PHAROS-EGRESS -d 10.1.1.53 -p tcp --dport 53 -j RETURN" in lines
    assert udp < lines.index("-A PHAROS-EGRESS -d 10.0.0.0/8 -j DROP")
    # A resolver on an address of the host itself is reached through INPUT.
    host_dns = lines.index("-A PHAROS-EGRESS-HOST -d 10.1.1.53 -p udp --dport 53 -j RETURN")
    assert host_dns < lines.index("-A PHAROS-EGRESS-HOST -j DROP")
    assert "127.0.0.53" not in out
    (tmp_path / "resolv.conf").write_text("nameserver 192.168.5.1\nnameserver ::1\n")
    from_file = rules(tmp_path, "print").stdout
    assert "-A PHAROS-EGRESS -d 192.168.5.1 -p udp --dport 53 -j RETURN" in from_file


def test_the_addresses_come_from_the_environment(tmp_path: Path) -> None:
    out = rules(
        tmp_path,
        "print",
        env={
            "PHAROS_EGRESS_SUBNET": "10.99.0.0/24",
            "PHAROS_EGRESS_API_IP": "10.99.0.250",
            "PHAROS_EGRESS_WORKER_IP": "10.99.0.251",
            "PHAROS_APP_SUBNET": "10.98.0.0/24",
            "PHAROS_DATA_SUBNET": "10.97.0.0/24",
            "PHAROS_OLLAMA_SUBNET": "10.96.0.0/24",
        },
    ).stdout
    assert "-I DOCKER-USER 1 -s 10.99.0.0/24 -j PHAROS-EGRESS" in out
    for range_ in ("10.99.0.0/24", "10.98.0.0/24", "10.97.0.0/24", "10.96.0.0/24"):
        assert f"-I INPUT 1 -s {range_} -j PHAROS-EGRESS-HOST" in out
    assert "-s 10.99.0.250 -p tcp --dport 443" in out and "-s 10.99.0.251 -p tcp" in out


@pytest.mark.parametrize(
    "env",
    [
        {"PHAROS_EGRESS_SUBNET": "172.28.40.0/33"},
        {"PHAROS_EGRESS_SUBNET": "172.28.40.0; iptables -F"},
        {"PHAROS_EGRESS_API_IP": "172.28.40.256"},
        {"PHAROS_EGRESS_WORKER_IP": "$(reboot)"},
        {"PHAROS_EGRESS_DNS": "10.0.0.1 -j ACCEPT"},
        {"PHAROS_EGRESS_FORWARD_CHAIN": "FORWARD -j ACCEPT"},
        {"PHAROS_DATA_SUBNET": "172.28.50.0/24 -j ACCEPT"},
        {"PHAROS_APP_SUBNET": "$(id)"},
        {"PHAROS_EGRESS_API_IP": "10.0.0.5"},  # not inside the egress range
    ],
)
def test_anything_that_is_not_an_address_is_refused_before_a_rule_is_made(
    tmp_path: Path, env: dict[str, str]
) -> None:
    result = rules(tmp_path, "print", env=env)
    assert result.returncode == 2 and result.stdout == ""


def iptables_stub(
    tmp_path: Path, chains: str = "DOCKER-USER INPUT", rules: str = "", fail_on: str = "__never__"
) -> dict[str, str]:
    """A fake iptables: knows the chains named, prints `rules` for -S, fails on one rule."""
    stub = tmp_path / "iptables"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        'echo "$*" >> "$STUB_LOG"\n'
        'if [ "$2" = -n ] && [ "$3" = -L ]; then [[ " $STUB_CHAINS " == *" $4 "* ]]; exit; fi\n'
        'if [ "$2" = -S ]; then printf "%b" "$STUB_RULES" | grep -- "^-A $3 " || true; exit 0; fi\n'
        '[[ "$*" == *"$STUB_FAIL_ON"* ]] && exit 1\n'
        "exit 0\n"
    )
    stub.chmod(0o755)
    log = tmp_path / "iptables.log"
    log.write_text("")
    return {
        "PHAROS_IPTABLES": str(stub),
        "STUB_LOG": str(log),
        "STUB_CHAINS": chains,
        "STUB_RULES": rules,
        "STUB_FAIL_ON": fail_on,
    }


OLD = (
    "-A DOCKER-USER -s 172.28.99.0/24 -j PHAROS-EGRESS\\n"  # a former run, another range
    "-A INPUT -s 172.28.99.0/24 -j PHAROS-EGRESS-HOST\\n"
    "-A INPUT -s 10.0.0.0/8 -j ACCEPT\\n"  # somebody else's rule: never touched
)


def test_apply_hooks_the_new_rules_in_before_it_unhooks_the_old_ones(tmp_path: Path) -> None:
    env = iptables_stub(
        tmp_path, chains="DOCKER-USER INPUT PHAROS-EGRESS PHAROS-EGRESS-HOST", rules=OLD
    )
    result = rules(tmp_path, "apply", env=env)
    assert result.returncode == 0, result.stderr
    calls = [c.removeprefix("-w ") for c in (tmp_path / "iptables.log").read_text().splitlines()]
    assert calls[0] == "-n -L DOCKER-USER"  # Docker's chain must be there
    written = [c for c in calls if c.startswith(("-N ", "-A ", "-I "))]
    expected = rules(tmp_path, "print").stdout.replace("PHAROS-EGRESS", "PHAROS-EGRESS-NEW")
    expected = expected.replace("PHAROS-EGRESS-NEW-HOST", "PHAROS-EGRESS-HOST-NEW")
    assert written == expected.splitlines()
    last_hook = max(i for i, c in enumerate(calls) if c.startswith("-I "))
    unhooked = [i for i, c in enumerate(calls) if c.startswith("-D ")]
    # The old jumps go, whatever range they named, and only after every new jump is in place.
    assert [calls[i] for i in unhooked] == [
        "-D DOCKER-USER -s 172.28.99.0/24 -j PHAROS-EGRESS",
        "-D INPUT -s 172.28.99.0/24 -j PHAROS-EGRESS-HOST",
    ]
    assert min(unhooked) > last_hook
    assert calls[-2:] == [
        "-E PHAROS-EGRESS-NEW PHAROS-EGRESS",
        "-E PHAROS-EGRESS-HOST-NEW PHAROS-EGRESS-HOST",
    ]
    assert not any("10.0.0.0/8 -j ACCEPT" in c for c in calls if c.startswith("-D "))


def test_a_failure_while_applying_leaves_the_old_rules_in_place(tmp_path: Path) -> None:
    env = iptables_stub(
        tmp_path,
        chains="DOCKER-USER INPUT PHAROS-EGRESS PHAROS-EGRESS-HOST PHAROS-EGRESS-NEW",
        rules=OLD,
        fail_on="--dport 443 -j RETURN",
    )
    result = rules(tmp_path, "apply", env=env)
    assert result.returncode != 0 and "earlier rules, if any, are still in place" in result.stderr
    calls = (tmp_path / "iptables.log").read_text().splitlines()
    assert not any(" -E " in c for c in calls)
    assert not any("-D DOCKER-USER -s 172.28.99.0/24" in c for c in calls)  # old jump kept
    assert any("-X PHAROS-EGRESS-NEW" in c for c in calls)  # the half-built chain removed


def test_apply_refuses_a_host_without_docker(tmp_path: Path) -> None:
    env = iptables_stub(tmp_path, chains="INPUT")
    result = rules(tmp_path, "apply", env=env)
    assert result.returncode == 1 and "is Docker running" in result.stderr
    calls = (tmp_path / "iptables.log").read_text().splitlines()
    assert not any(" -A " in c or " -N " in c for c in calls)


def test_remove_takes_every_jump_and_chain_out(tmp_path: Path) -> None:
    env = iptables_stub(
        tmp_path, chains="DOCKER-USER INPUT PHAROS-EGRESS PHAROS-EGRESS-HOST", rules=OLD
    )
    assert rules(tmp_path, "remove", env=env).returncode == 0
    calls = (tmp_path / "iptables.log").read_text().splitlines()
    assert "-w -D DOCKER-USER -s 172.28.99.0/24 -j PHAROS-EGRESS" in calls
    assert "-w -X PHAROS-EGRESS" in calls and "-w -X PHAROS-EGRESS-HOST" in calls


# --- the check -------------------------------------------------------------------------------

RUNNER = """#!/usr/bin/env bash
# Stands in for `docker compose exec` / `ip netns exec`: $1 is the service, then python3 -c CODE.
service=$1; code=$4
if [[ $code == *getaddrinfo* ]]; then
  [[ " $STUB_DNS " == *" $service "* ]] && echo open || echo closed; exit 0
fi
target=$(grep -oE "\\('[0-9.]+', [0-9]+\\)" <<<"$code" | tr -d "()' " | tr , :)
[ "$target" = "$STUB_BROKEN" ] && exit 1
[[ " $STUB_OPEN " == *" $service:$target "* ]] && echo open || echo closed
"""

GOOD_OPEN = "api:198.51.100.10:443 worker:198.51.100.10:443 worker:198.51.100.10:80"


def check(tmp_path: Path, **stub: str) -> subprocess.CompletedProcess[str]:
    runner = tmp_path / "runner"
    runner.write_text(RUNNER)
    runner.chmod(0o755)
    env = {
        "PATH": os.environ["PATH"],
        "EGRESS_RUN_API": f"{runner} api",
        "EGRESS_RUN_WORKER": f"{runner} worker",
        "EGRESS_PUBLIC": "198.51.100.10",
        "EGRESS_GATEWAYS": "egress=172.28.40.1 data=172.28.50.1",
        "EGRESS_LISTENERS": "yes",
        "PHAROS_EGRESS_SUBNET": "172.28.40.0/24",
        "STUB_OPEN": GOOD_OPEN,
        "STUB_DNS": "api worker",
        "STUB_BROKEN": "",
        **stub,
    }
    return subprocess.run(  # noqa: S603
        [str(CHECK)], env=env, capture_output=True, text=True, timeout=60
    )


def levels(result: subprocess.CompletedProcess[str]) -> list[str]:
    return [line.split()[0] for line in result.stdout.splitlines() if line.strip()]


def test_rules_that_hold_pass_every_line(tmp_path: Path) -> None:
    result = check(tmp_path)
    assert result.returncode == 0, result.stdout
    assert set(levels(result)) == {"PASS"} and len(levels(result)) == 18


@pytest.mark.parametrize(
    ("extra", "words"),
    [
        ("api:198.51.100.10:80", "198.51.100.10:80 is open, expected closed"),
        ("worker:198.51.100.10:25", "198.51.100.10:25 is open"),
        ("worker:10.255.255.1:80", "10.255.255.1:80 is open"),
        ("api:169.254.169.254:80", "169.254.169.254:80 is open"),
        ("worker:172.28.40.1:18999", "172.28.40.1:18999 is open"),
        ("api:172.28.50.1:18999", "172.28.50.1:18999 is open"),  # through the data network
        ("worker:168.63.129.16:80", "168.63.129.16:80 is open"),
    ],
)
def test_a_call_the_rules_should_refuse_fails_the_check(
    tmp_path: Path, extra: str, words: str
) -> None:
    result = check(tmp_path, STUB_OPEN=f"{GOOD_OPEN} {extra}")
    assert result.returncode == 1
    assert words in result.stdout


def test_a_refused_control_or_no_dns_fails_the_check(tmp_path: Path) -> None:
    blocked = check(tmp_path, STUB_OPEN="worker:198.51.100.10:443 worker:198.51.100.10:80")
    assert blocked.returncode == 1
    assert "198.51.100.10:443 is closed, expected open" in blocked.stdout
    no_dns = check(tmp_path, STUB_DNS="api")
    assert no_dns.returncode == 1 and "example.com does not resolve" in no_dns.stdout


def test_a_service_that_cannot_be_asked_is_not_checked_not_passed(tmp_path: Path) -> None:
    result = check(tmp_path, STUB_BROKEN="198.51.100.10:25")
    assert (
        "NOT CHECKED" in result.stdout and "PASS     api     198.51.100.10:25" not in result.stdout
    )
    assert result.returncode == 0  # NOT CHECKED is no failure, and no pass either


@pytest.mark.parametrize(
    "env",
    [{"EGRESS_PUBLIC": "1.1.1.1; rm -rf /"}, {"EGRESS_NAME": "a.example'); print('x"},
     {"EGRESS_GATEWAY_PORT": "80 or 1"}, {"EGRESS_GATEWAYS": "egress=1.2.3.4');x('"},
     {"EGRESS_GATEWAYS": "evil=1.2.3.4"}],
)  # fmt: skip
def test_targets_that_are_not_addresses_are_refused(tmp_path: Path, env: dict[str, str]) -> None:
    result = check(tmp_path, **env)
    assert result.returncode == 2


@pytest.mark.skipif(
    os.environ.get("PHAROS_NETNS_TEST") != "1" or os.geteuid() != 0,
    reason="network namespaces: root and PHAROS_NETNS_TEST=1 only (never in CI)",
)
def test_the_rules_hold_against_real_packets_in_network_namespaces() -> None:
    result = subprocess.run(  # noqa: S603
        [str(NETNS)], capture_output=True, text=True, timeout=300
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "FAIL" not in result.stdout
    assert len(re.findall(r"^PASS", result.stdout, re.MULTILINE)) == 52


def test_a_range_is_validated_before_any_arithmetic(tmp_path: Path) -> None:
    """Review of 2026-11-14: `$(( ... ))` on an unchecked range ran a command substitution."""
    marker = tmp_path / "ran"
    result = subprocess.run(  # noqa: S603
        [str(CHECK)],
        env={
            "PATH": os.environ["PATH"],
            "PHAROS_EGRESS_SUBNET": f"1.2.3.a[$(touch {marker})0]/24",
        },
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 2 and not marker.exists()
