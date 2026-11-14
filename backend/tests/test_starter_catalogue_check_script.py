"""`scripts/starter-catalogue-check.sh`: the starter catalogue fetched for real (M5-08).

The real run needs Docker and the internet and is manual. Here Docker and curl are stubs: the
tests check the script's own logic (usage, its project and ports, the teardown, the password never
on a command line, the exit code from what the sources API reports). Each safety check is also run
against a broken copy of the script, to show that it would catch the break.
"""

import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/starter-catalogue-check.sh"
RESERVED_PORTS = {"80", "443", "5432", "6379"}

DOCKER_STUB = """#!/usr/bin/env bash
echo "docker $*" >> "$STUB_LOG"
for name in PHAROS_IMAGE_TAG PHAROS_GITHUB_TOKEN PHAROS_LLM_API_KEY COMPOSE_PROFILES; do
  [ -z "${!name+set}" ] || echo "$name leaked" >> "$STUB_LOG.env"
done
case "$1" in
  inspect) echo healthy; exit 0 ;;
  compose)
    for arg in "$@"; do
      case "$arg" in
        ps) echo container-id; exit 0 ;;
        up) exit "${STUB_UP_EXIT:-0}" ;;
        exec) cat > /dev/null; exit 0 ;;
      esac
    done
    exit 0 ;;
esac
exit 0
"""
CURL_STUB = """#!/usr/bin/env bash
echo "curl $*" >> "$STUB_LOG"
out=/dev/null method=GET url= body=
while [ "$#" -gt 0 ]; do
  case "$1" in
    -o) out=$2; shift ;;
    -X) method=$2; shift ;;
    --data-binary) body=${2#@}; shift ;;
    https://*) url=$1 ;;
  esac
  shift
done
case "$method $url" in
  "POST "*/api/auth/local/login)
    cp "$body" "$STUB_LOG.login"
    [ "${STUB_LOGIN:-200}" = refused ] && { printf 000; exit 7; }
    printf '{"csrf_token": "token", "user": {}}' > "$out"
    printf '%s' "${STUB_LOGIN:-200}"; exit 0 ;;
  "GET "*/runs*) printf '%s' "$STUB_RUNS" > "$out" ;;
  "GET "*/api/sources*) printf '%s' "$STUB_SOURCES" > "$out" ;;
  "PATCH "*/api/sources/*) printf '{}' > "$out"; printf '%s' "${STUB_PATCH:-200}"; exit 0 ;;
  *) exit 7 ;;
esac
printf 200
"""


def source(name: str, *, ok: bool = True, failures: int = 0) -> dict[str, object]:
    return {
        "id": f"id-{name.lower()}",
        "name": name,
        "type": "rss",
        "last_success_at": "2026-11-11T10:00:00Z" if ok else None,
        "consecutive_failures": failures,
        "last_error_code": "bad_status" if failures else None,
    }


RUNS = {
    "items": [
        {
            "outcome": "bad_status",
            "items_new": None,
            "error_code": "bad_status",
            "http_status": 403,
        },
        {"outcome": "ok", "items_new": 7, "error_code": None, "http_status": 200},
    ]
}


def run(
    tmp_path: Path,
    sources: list[dict[str, object]],
    *args: str,
    script: Path = SCRIPT,
    env: dict[str, str] | None = None,
) -> tuple[subprocess.CompletedProcess[str], list[str], Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    for name, text in (("docker", DOCKER_STUB), ("curl", CURL_STUB)):
        stub = bin_dir / name
        stub.write_text(text)
        stub.chmod(0o755)
    log = tmp_path / "calls.log"
    log.write_text("")
    work = tmp_path / "tmp"
    work.mkdir(exist_ok=True)
    full_env = {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "HOME": str(tmp_path),
        "TMPDIR": str(work),
        "STUB_LOG": str(log),
        "STUB_SOURCES": json.dumps({"items": sources, "total": len(sources)}),
        "STUB_RUNS": json.dumps(RUNS),
        "CATALOGUE_POLL_SECONDS": "0",
        "CATALOGUE_LOCK": str(tmp_path / "catalogue.lock"),
        **(env or {}),
    }
    result = subprocess.run(  # noqa: S603
        [str(script), *args],
        env=full_env,
        capture_output=True,
        text=True,
        timeout=60,
        stdin=subprocess.DEVNULL,
    )
    return result, log.read_text().splitlines(), work


def broken_copy(tmp_path: Path, old: str, new: str) -> Path:
    text = SCRIPT.read_text()
    assert old in text, old
    copy = tmp_path / "scripts" / SCRIPT.name
    copy.parent.mkdir(parents=True, exist_ok=True)
    copy.write_text(text.replace(old, new))
    copy.chmod(0o755)
    shutil.copytree(ROOT / "scripts/lib", copy.parent / "lib", dirs_exist_ok=True)
    return copy


def reserved_ports_used(text: str) -> set[str]:
    """Port numbers of other projects that the script would publish or reach."""
    code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    defaults = set(re.findall(r"PORT:-(\d+)\}", code))
    literal = set(re.findall(r"(?<![\d.])(80|443|5432|6379)(?![\d.])", code))
    return (defaults | literal) & RESERVED_PORTS


def tears_down(text: str) -> bool:
    cleanup = re.search(r"cleanup\(\) \{(.*?)\n\}", text, re.S)
    return (
        cleanup is not None
        and "down --volumes --remove-orphans" in cleanup.group(1)
        and "docker rmi" in cleanup.group(1)
        and re.search(r"^trap cleanup EXIT$", text, re.M) is not None
    )


def compose_calls(log: list[str]) -> list[str]:
    return [line for line in log if line.startswith("docker compose")]


def torn_down_after_up(log: list[str]) -> bool:
    up = next(i for i, line in enumerate(log) if " up -d --build" in line)
    after = log[up + 1 :]
    return any(" down --volumes --remove-orphans" in line for line in after) and any(
        line.startswith("docker rmi ") for line in after
    )


def assert_one_failing_source_exits_1(tmp_path: Path, script: Path = SCRIPT) -> None:
    result, log, work = run(
        tmp_path, [source("Alpha"), source("Broken", ok=False, failures=2)], script=script
    )
    assert result.returncode == 1
    assert re.search(r"^Broken\s+rss\s+failed\s+7\s+bad_status \(HTTP 403\)$", result.stdout, re.M)
    assert "2 source(s): 1 ok, 1 failed, 0 not fetched in time" in result.stdout
    assert torn_down_after_up(log)
    assert list(work.iterdir()) == []


def sibling_settings(pattern: str) -> dict[str, set[str]]:
    """Values matching `pattern` in each of the other scripts (ports, subnets)."""
    found: dict[str, set[str]] = {}
    for path in (ROOT / "scripts").glob("*.sh"):
        if path != SCRIPT:
            found[path.name] = set(re.findall(pattern, path.read_text()))
    return found


# --- the script as it is


def test_the_script_is_executable_and_parses() -> None:
    assert os.access(SCRIPT, os.X_OK)
    subprocess.run(["bash", "-n", str(SCRIPT)], check=True)  # noqa: S603, S607


def test_it_uses_its_own_project_and_none_of_the_reserved_ports() -> None:
    text = SCRIPT.read_text()
    assert re.search(r"^PROJECT=pharos-catalogue$", text, re.M)
    assert reserved_ports_used(text) == set()
    # docker compose is only ever called through the function that names the project.
    calls = [line for line in text.splitlines() if "docker compose" in line]
    assert calls and all('-p "$PROJECT"' in line for line in calls)


def test_it_installs_a_trap_that_tears_everything_down() -> None:
    assert tears_down(SCRIPT.read_text())


@pytest.mark.parametrize(
    "args", [("--bogus",), ("--timeout",), ("--timeout", "soon"), ("--timeout", "0"), ("x",)]
)
def test_wrong_usage_exits_2_without_calling_docker(tmp_path: Path, args: tuple[str, ...]) -> None:
    result, log, _ = run(tmp_path, [source("A")], *args)
    assert result.returncode == 2
    assert "usage:" in result.stderr
    assert log == []


def test_all_sources_ok_exits_0_and_prints_the_table(tmp_path: Path) -> None:
    result, log, work = run(tmp_path, [source("Alpha"), source("Beta")])
    assert result.returncode == 0, result.stdout + result.stderr
    assert re.search(r"^Alpha\s+rss\s+ok\s+7\s*$", result.stdout, re.M)
    assert "2 source(s): 2 ok, 0 failed, 0 not fetched in time" in result.stdout
    # Every source was switched on through the API.
    assert sum(" PATCH " in line for line in log) == 2
    assert all(" -p pharos-catalogue " in line for line in compose_calls(log))
    # Torn down at the end, and its temporary directory is gone.
    up = next(i for i, line in enumerate(log) if " up -d --build" in line)
    assert any(" down --volumes --remove-orphans" in line for line in log[up:])
    removed = [line for line in log if line.startswith("docker rmi ")]
    assert removed == ["docker rmi pharos-web:catalogue pharos-backend:catalogue"]
    assert list(work.iterdir()) == []


def test_one_failing_source_exits_1_and_says_why(tmp_path: Path) -> None:
    assert_one_failing_source_exits_1(tmp_path)


def test_a_source_never_fetched_in_time_exits_1(tmp_path: Path) -> None:
    result, _, _ = run(tmp_path, [source("Alpha"), source("Slow", ok=False)], "--timeout", "1")
    assert result.returncode == 1
    assert re.search(r"^Slow\s+rss\s+not fetched", result.stdout, re.M)


def test_no_imported_source_is_a_failure(tmp_path: Path) -> None:
    result, log, _ = run(tmp_path, [])
    assert result.returncode == 1
    assert "no source was imported" in result.stderr
    assert any(" down --volumes" in line for line in log[-3:])


def test_the_password_never_appears_on_a_command_line(tmp_path: Path) -> None:
    result, log, _ = run(tmp_path, [source("Alpha")])
    assert result.returncode == 0
    sent = json.loads((tmp_path / "calls.log.login").read_text())
    assert sent["username"] == "catalogue-admin"
    assert len(sent["password"]) >= 32
    assert not any(sent["password"] in line for line in log)
    assert sent["password"] not in result.stdout + result.stderr


@pytest.mark.parametrize(
    ("env", "message"),
    [
        ({"STUB_UP_EXIT": "1"}, "the stack did not start"),
        ({"STUB_LOGIN": "401"}, "sign-in answered 401"),
        ({"STUB_LOGIN": "refused"}, "sign-in answered 000"),
        ({"STUB_PATCH": "422"}, "answered 422"),
    ],
)
def test_every_failure_on_the_way_still_tears_down(
    tmp_path: Path, env: dict[str, str], message: str
) -> None:
    result, log, work = run(tmp_path, [source("Alpha")], env=env)
    assert result.returncode == 1
    assert message in result.stderr
    assert torn_down_after_up(log)
    assert list(work.iterdir()) == []


def test_settings_exported_in_the_shell_never_reach_compose(tmp_path: Path) -> None:
    exported = {
        "PHAROS_IMAGE_TAG": "local",
        "PHAROS_GITHUB_TOKEN": "fake-token",
        "PHAROS_LLM_API_KEY": "fake-key",
        "COMPOSE_PROFILES": "local-llm",
    }
    result, log, _ = run(tmp_path, [source("Alpha")], env=exported)
    assert result.returncode == 0, result.stderr
    assert compose_calls(log)
    assert not (tmp_path / "calls.log.env").exists()
    assert not any("local" in line.split() for line in log if line.startswith("docker rmi"))


def test_a_second_run_at_the_same_time_is_refused_and_touches_nothing(tmp_path: Path) -> None:
    lock = tmp_path / "catalogue.lock"
    holder = subprocess.Popen(  # noqa: S603
        ["flock", str(lock), "sleep", "30"],  # noqa: S607
    )
    try:
        for _ in range(50):
            busy = subprocess.run(  # noqa: S603
                ["flock", "-n", str(lock), "true"],  # noqa: S607
                check=False,
            )
            if busy.returncode != 0:
                break
            time.sleep(0.1)
        result, log, _ = run(tmp_path, [source("Alpha")])
    finally:
        holder.kill()
        holder.wait()
    assert result.returncode == 1
    assert "another starter catalogue check is running" in result.stderr
    assert log == []


def test_its_ports_and_subnets_are_used_by_no_other_script() -> None:
    text = SCRIPT.read_text()
    ports = set(re.findall(r"PORT:-(\d+)\}", text))
    subnets = set(re.findall(r"172\.28\.\d+\.0/24", text))
    assert ports == {"19880", "19843"}
    assert subnets == {"172.28.36.0/24", "172.28.37.0/24"}
    for name, used in sibling_settings(r"PORT:-(\d+)\}").items():
        assert not ports & used, name
    for name, used in sibling_settings(r"172\.28\.\d+\.0/24").items():
        assert not subnets & used, name


# --- broken copies: each check above would catch the break


def test_a_copy_without_the_trap_is_caught(tmp_path: Path) -> None:
    copy = broken_copy(tmp_path, "trap cleanup EXIT\n", "\n")
    assert not tears_down(copy.read_text())
    _, log, work = run(tmp_path, [source("Alpha")], script=copy)
    up = next(i for i, line in enumerate(log) if " up -d --build" in line)
    assert not any(" down --volumes" in line for line in log[up:])
    assert list(work.iterdir()) != []


def test_a_copy_on_port_443_is_caught(tmp_path: Path) -> None:
    copy = broken_copy(tmp_path, "HTTPS_PORT:-19843}", "HTTPS_PORT:-443}")
    assert reserved_ports_used(copy.read_text()) == {"443"}


def test_a_copy_that_ignores_a_failing_source_is_caught(tmp_path: Path) -> None:
    copy = broken_copy(
        tmp_path, 'sys.exit(0 if counts["ok"] == len(sources) and sources else 1)', "sys.exit(0)"
    )
    with pytest.raises(AssertionError):
        assert_one_failing_source_exits_1(tmp_path, script=copy)


def test_a_copy_that_keeps_the_exported_settings_is_caught(tmp_path: Path) -> None:
    copy = broken_copy(tmp_path, "\ndrop_installation_env\n", "\n:\n")
    result, _, _ = run(tmp_path, [source("Alpha")], script=copy, env={"PHAROS_IMAGE_TAG": "local"})
    assert result.returncode == 0
    assert set((tmp_path / "calls.log.env").read_text().splitlines()) == {"PHAROS_IMAGE_TAG leaked"}


def test_a_copy_without_the_project_name_is_caught(tmp_path: Path) -> None:
    copy = broken_copy(tmp_path, 'docker compose -p "$PROJECT" -f', "docker compose -f")
    calls = [line for line in copy.read_text().splitlines() if "docker compose" in line]
    assert not all('-p "$PROJECT"' in line for line in calls)
