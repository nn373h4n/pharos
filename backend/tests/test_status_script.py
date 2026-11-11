"""`scripts/status.sh` reports the state of a running installation and changes nothing (M5-11).

Compose, Docker and `df` are stubs, so every state is made on purpose: a stopped service, an
unhealthy one, a failing readiness check, a missing or old backup, a full disk.
"""

import os
import subprocess
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/status.sh"

# The stub for `docker compose ...`; every call is logged, so a test can see that only reads ran.
COMPOSE_STUB = """#!/usr/bin/env bash
echo "$1 ${2-} ${3-} ${4-}" >> "$STUB_LOG"  # not "$*": the probe is a many-line script
case "$1" in
  config) [ "$STUB_CONFIG_EXIT" = 0 ] || exit 1; printf '%s\\n' $STUB_SERVICES ;;
  ps) printf '%b' "$STUB_PS" ;;
  exec) [ "$STUB_EXEC_EXIT" = 0 ] || exit 1; printf '%b' "$STUB_READY" ;;
  *) echo "unexpected: $*" >&2; exit 99 ;;
esac
"""
DEFAULTS = {
    "STUB_CONFIG_EXIT": "0",
    "STUB_EXEC_EXIT": "0",
    "STUB_SERVICES": "web api worker beat migrate",
    "STUB_PS": "web|running|\\napi|running|healthy\\nworker|running|healthy\\n"
    "beat|running|\\nmigrate|exited|\\n",
    "STUB_READY": "status=ok\\ndatabase=ok\\nredis=ok\\nworker=ok\\n",
}
DOCKER_STUB = """#!/usr/bin/env bash
echo "docker $*" >> "$STUB_LOG"
[ "$1" = info ] && printf '%s' "${STUB_DOCKER_ROOT-}"
exit 0
"""
DF_STUB = """#!/usr/bin/env bash
printf 'Filesystem B Used Avail Capacity Mount\\n/dev/x 100 1 1 %s%% /\\n' "${STUB_DISK:-40}"
"""


def run(
    tmp_path: Path, *args: str, env: dict[str, str] | None = None, cwd: Path | None = None
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    for name, text in (("compose", COMPOSE_STUB), ("docker", DOCKER_STUB), ("df", DF_STUB)):
        stub = bin_dir / name
        stub.write_text(text)
        stub.chmod(0o755)
    log = tmp_path / "calls.log"
    log.write_text("")
    docker_root = tmp_path / "docker-root"
    docker_root.mkdir(exist_ok=True)
    full_env = {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "PHAROS_COMPOSE": str(bin_dir / "compose"),
        "STUB_LOG": str(log),
        "STUB_DOCKER_ROOT": str(docker_root),
        "HOME": str(tmp_path),
        **DEFAULTS,
        **(env or {}),
    }
    result = subprocess.run(  # noqa: S603
        [str(SCRIPT), *args], env=full_env, capture_output=True, text=True, timeout=60, cwd=cwd
    )
    return result, log.read_text().splitlines()


def levels(result: subprocess.CompletedProcess[str]) -> dict[str, str]:
    """Check name -> level, from the report lines."""
    out = {}
    for line in result.stdout.splitlines():
        for level in ("NOT CHECKED", "PASS", "WARN", "FAIL"):
            if line.startswith(level):
                out[line[len(level) :].split()[0]] = level
                break
    return out


def make_dump(base: Path, age_hours: float, where: str = "daily") -> None:
    folder = base / where
    folder.mkdir(parents=True, exist_ok=True)
    dump = folder / "pharos-20261110T020000Z.dump"
    dump.write_text("x")
    when = time.time() - age_hours * 3600
    os.utime(dump, (when, when))


def test_a_healthy_installation_passes_and_changes_nothing(tmp_path: Path) -> None:
    backups = tmp_path / "dumps"
    make_dump(backups, 3)
    result, calls = run(tmp_path, "--backups", str(backups))
    assert result.returncode == 0, result.stdout + result.stderr
    assert levels(result) == {
        "services": "PASS",
        "readyz": "PASS",
        "backup": "PASS",
        "disk": "PASS",
    }
    # Only reads: no up, down, restart, run, rm, stop, kill, or a shell in a container.
    verbs = {call.split()[0] for call in calls if not call.startswith("docker ")}
    assert verbs <= {"config", "ps", "exec"}, verbs
    assert not any(
        call.startswith("docker ") and not call.startswith("docker info") for call in calls
    )


def test_the_one_shot_services_are_not_required_to_run(tmp_path: Path) -> None:
    result, _ = run(
        tmp_path,
        env={
            "STUB_PS": "web|running|\\napi|running|healthy\\nworker|running|healthy\\n"
            "beat|running|\\n"
        },
    )
    assert levels(result)["services"] == "PASS"  # `migrate` is absent from `ps` and that is fine


@pytest.mark.parametrize(
    "ps, needle",
    [
        ("web|running|\\napi|exited|\\nworker|running|healthy\\nbeat|running|\\n", "api(exited)"),
        (
            "web|running|\\napi|running|unhealthy\\nworker|running|healthy\\nbeat|running|\\n",
            "api(running/unhealthy)",
        ),
        (
            "web|running|\\napi|running|starting\\nworker|running|healthy\\nbeat|running|\\n",
            "api(running/starting)",
        ),
        ("web|running|\\nworker|running|healthy\\nbeat|running|\\n", "api(missing)"),
        (
            "web|restarting|\\napi|running|healthy\\nworker|running|healthy\\nbeat|running|\\n",
            "web(restarting)",
        ),
        ("", "web(missing)"),
    ],
)
def test_a_service_that_is_down_or_unhealthy_fails_and_is_named(
    tmp_path: Path, ps: str, needle: str
) -> None:
    result, _ = run(tmp_path, env={"STUB_PS": ps})
    assert result.returncode == 1
    assert levels(result)["services"] == "FAIL"
    assert needle in result.stdout


def test_a_service_name_that_is_a_prefix_of_another_is_not_mistaken_for_it(tmp_path: Path) -> None:
    # `api` is down; `old-api` is up. A substring match would call `api` fine.
    result, _ = run(
        tmp_path,
        env={
            "STUB_SERVICES": "web api",
            "STUB_PS": "web|running|\\nold-api|running|\\n",
        },
    )
    assert levels(result)["services"] == "FAIL"
    assert "api(missing)" in result.stdout


@pytest.mark.parametrize(
    "services",
    ["Web API", "web my$proxy", "migrate operator", "ollama-init ollama-pull migrate"],
)
def test_nothing_checked_is_never_a_pass(tmp_path: Path, services: str) -> None:
    # Names the script cannot match safely, or only one-shot services: no PASS with 0 services.
    result, _ = run(tmp_path, env={"STUB_SERVICES": services})
    assert result.returncode == 1
    assert levels(result)["services"] == "FAIL"


def test_a_service_name_with_a_dot_is_checked_like_any_other(tmp_path: Path) -> None:
    ps = "web|running|\\nmy.proxy|exited|\\n"
    result, _ = run(tmp_path, env={"STUB_SERVICES": "web my.proxy", "STUB_PS": ps})
    assert levels(result)["services"] == "FAIL"
    assert "my.proxy(exited)" in result.stdout


def test_the_local_model_s_one_shot_services_are_not_required_to_run(tmp_path: Path) -> None:
    result, _ = run(
        tmp_path,
        env={"STUB_SERVICES": "web api worker beat migrate ollama-init ollama-pull"},
    )
    assert levels(result)["services"] == "PASS"


def test_compose_not_answering_is_a_failure_not_a_pass(tmp_path: Path) -> None:
    result, _ = run(tmp_path, env={"STUB_CONFIG_EXIT": "1"})
    assert result.returncode == 1
    assert levels(result)["services"] == "FAIL"
    result, _ = run(tmp_path, env={"STUB_SERVICES": ""})
    assert levels(result)["services"] == "FAIL"  # no services listed is not "all up"


@pytest.mark.parametrize(
    "ready, failing",
    [
        ("status=unavailable\\ndatabase=ok\\nredis=fail\\nworker=ok\\n", "redis"),
        ("status=unavailable\\nworker=fail\\nmigrations=fail\\n", "worker migrations"),
        ("status=ok\\ndatabase=ok\\nredis=fail\\n", "redis"),  # one bad check beats an "ok" total
        ("status=unavailable\\ndatabase=ok\\n", ""),  # unavailable with no named cause still fails
        ("", ""),  # an empty answer
        ("<html>502</html>\\n", ""),  # something that is not our report
    ],
)
def test_readiness_failures_fail_and_name_the_checks(
    tmp_path: Path, ready: str, failing: str
) -> None:
    result, _ = run(tmp_path, env={"STUB_READY": ready})
    assert result.returncode == 1
    assert levels(result)["readyz"] == "FAIL"
    for name in failing.split():
        assert name in result.stdout


def test_an_unreachable_api_container_fails(tmp_path: Path) -> None:
    result, _ = run(tmp_path, env={"STUB_EXEC_EXIT": "1"})
    assert result.returncode == 1
    assert levels(result)["readyz"] == "FAIL"


def test_text_from_the_container_is_not_echoed(tmp_path: Path) -> None:
    hostile = "status=ok\\nsecret_token=hunter2x\\nredis=ok; rm -rf\\ndatabase=OK\\n"
    result, _ = run(tmp_path, env={"STUB_READY": hostile})
    assert "hunter2" not in result.stdout + result.stderr
    assert "rm -rf" not in result.stdout + result.stderr


@pytest.mark.parametrize(
    "hours, level",
    [
        (1, "PASS"),
        (29, "PASS"),
        (30, "WARN"),  # the limits themselves belong to the worse level
        (31, "WARN"),
        (53, "WARN"),
        (54, "FAIL"),
        (55, "FAIL"),
        (500, "FAIL"),
        (-48, "FAIL"),  # a dump dated in the future is not fresh
    ],
)
def test_the_age_of_the_newest_dump_sets_the_level(tmp_path: Path, hours: int, level: str) -> None:
    make_dump(tmp_path / "dumps", hours)
    result, _ = run(tmp_path, "--backups", str(tmp_path / "dumps"))
    assert levels(result)["backup"] == level
    assert (result.returncode == 1) == (level == "FAIL")


@pytest.mark.parametrize("folder", ["daily", "weekly", "monthly"])
def test_a_dump_in_any_of_the_three_folders_is_found(tmp_path: Path, folder: str) -> None:
    make_dump(tmp_path / "dumps", 2, folder)
    result, _ = run(tmp_path, "--backups", str(tmp_path / "dumps"))
    assert levels(result)["backup"] == "PASS"


def test_a_relative_backups_path_is_read_from_where_the_script_was_run(tmp_path: Path) -> None:
    make_dump(tmp_path / "dumps", 2)
    result, _ = run(tmp_path, "--backups", "dumps", cwd=tmp_path)
    assert levels(result)["backup"] == "PASS"


def test_the_newest_dump_counts_even_when_an_older_one_is_in_another_folder(tmp_path: Path) -> None:
    make_dump(tmp_path / "dumps", 400, "monthly")
    make_dump(tmp_path / "dumps", 2, "daily")
    result, _ = run(tmp_path, "--backups", str(tmp_path / "dumps"))
    assert levels(result)["backup"] == "PASS"


def test_a_missing_directory_or_no_dump_fails(tmp_path: Path) -> None:
    result, _ = run(tmp_path, "--backups", str(tmp_path / "nowhere"))
    assert levels(result)["backup"] == "FAIL"
    (tmp_path / "empty").mkdir()
    (tmp_path / "empty" / "daily").mkdir()
    (tmp_path / "empty" / "daily" / "notes.txt").write_text("x")  # not a dump
    result, _ = run(tmp_path, "--backups", str(tmp_path / "empty"))
    assert levels(result)["backup"] == "FAIL"


def test_without_backups_the_check_is_not_run_and_says_so(tmp_path: Path) -> None:
    result, _ = run(tmp_path)
    assert levels(result)["backup"] == "NOT CHECKED"
    assert result.returncode == 0  # not run is not a failure, but it is never printed as PASS


@pytest.mark.parametrize(
    "used, level", [("40", "PASS"), ("84", "PASS"), ("85", "WARN"), ("94", "WARN"), ("95", "FAIL")]
)
def test_disk_use_sets_the_level(tmp_path: Path, used: str, level: str) -> None:
    result, _ = run(tmp_path, env={"STUB_DISK": used})
    assert levels(result)["disk"] == level


def test_an_unreadable_docker_directory_is_not_checked_not_passed(tmp_path: Path) -> None:
    result, _ = run(tmp_path, env={"STUB_DOCKER_ROOT": str(tmp_path / "missing")})
    assert levels(result)["disk"] == "NOT CHECKED"


@pytest.mark.parametrize(
    "args",
    [("--backups",), ("--nope",), ("extra",), ("--backups", "a", "b")],
)
def test_wrong_usage_is_refused_before_any_call(tmp_path: Path, args: tuple[str, ...]) -> None:
    result, calls = run(tmp_path, *args)
    assert result.returncode == 2
    assert calls == []


def test_one_failure_among_passes_still_exits_one(tmp_path: Path) -> None:
    result, _ = run(tmp_path, env={"STUB_DISK": "99"})
    assert result.returncode == 1
    assert levels(result)["services"] == "PASS"
