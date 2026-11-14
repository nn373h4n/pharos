"""The throwaway stacks take no setting from the calling shell (M5-13 review).

`scripts/lib/throwaway-env.sh` drops every PHAROS_*, POSTGRES_* and COMPOSE_* variable but the
ports a script reads, and every script that starts a throwaway stack sources it before its first
Compose call and publishes its ports on 127.0.0.1 only.
"""

import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
HELPER = ROOT / "scripts/lib/throwaway-env.sh"
THROWAWAY = {
    "loadtest.sh": "LOAD",
    "resilience-check.sh": "RESIL",
    "compose-smoke.sh": "SMOKE",
    "sso-dev-check.sh": "SSO",
    "stack-e2e.sh": "STACK",
    "starter-catalogue-check.sh": None,
}


def test_the_helper_drops_installation_settings_and_keeps_the_named_ports() -> None:
    exported = {
        "PHAROS_IMAGE_TAG": "local",
        "PHAROS_LLM_API_KEY": "fake-key",
        "PHAROS_OIDC_CLIENT_SECRET": "fake-secret",
        "PHAROS_BIND_ADDRESS": "0.0.0.0",  # noqa: S104 (a value the helper must drop)
        "POSTGRES_PASSWORD": "fake-password",
        "COMPOSE_PROFILES": "local-llm",
        "COMPOSE_PROJECT_NAME": "pharos",
        "PHAROS_SMOKE_HTTPS_PORT": "28443",
        "PHAROS_SMOKE_HTTP_PORT": "28080",
        "UNRELATED": "kept",
    }
    script = (
        f"source {HELPER}\n"
        "drop_installation_env PHAROS_SMOKE_HTTP_PORT PHAROS_SMOKE_HTTPS_PORT\n"
        "compgen -e | grep -E '^(PHAROS_|POSTGRES_|COMPOSE_|UNRELATED)' | sort\n"
    )
    left = subprocess.run(  # noqa: S603
        ["/bin/bash", "-c", script],
        env={"PATH": "/usr/bin:/bin", **exported},
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    assert left == ["PHAROS_SMOKE_HTTPS_PORT", "PHAROS_SMOKE_HTTP_PORT", "UNRELATED"]


@pytest.mark.parametrize(("name", "tag"), sorted(THROWAWAY.items()))
def test_every_throwaway_stack_drops_the_shell_settings_before_compose(
    name: str, tag: str | None
) -> None:
    lines = (ROOT / "scripts" / name).read_text().splitlines()
    source = lines.index("source scripts/lib/throwaway-env.sh")
    call = next(i for i, line in enumerate(lines) if line.startswith("drop_installation_env"))
    compose = next(i for i, line in enumerate(lines) if "docker compose" in line)
    assert source < call < compose
    # Only the script's own ports are kept, and they were read before the drop.
    kept = lines[call].split()[1:]
    assert kept == ([f"PHAROS_{tag}_HTTP_PORT", f"PHAROS_{tag}_HTTPS_PORT"] if tag else [])
    for port in kept:
        assert any(f"${{{port}:-" in line for line in lines[:call]), port


@pytest.mark.parametrize("name", sorted(THROWAWAY))
def test_every_throwaway_stack_publishes_on_this_machine_only(name: str) -> None:
    text = (ROOT / "scripts" / name).read_text()
    assert re.search(r"^PHAROS_BIND_ADDRESS=127\.0\.0\.1$", text, re.MULTILINE), name


def test_the_load_test_keeps_its_sessions_and_summaries_private() -> None:
    """k6 runs as the caller, so nothing in the work directory needs to be readable by others."""
    text = (ROOT / "scripts/loadtest.sh").read_text()
    assert 'chmod 700 "$WORK"' in text
    assert not re.search(r"chmod 6[0-7][1-7]|chmod 7[0-7][1-7] \"\$WORK\"", text)
    k6 = [line for line in text.splitlines() if "docker run" in line and "--network host" in line]
    assert k6 and all('--user "$(id -u):$(id -g)"' in line for line in k6)
