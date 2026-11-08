"""The verdict of the single sign-on burst report (M5-08): a run in which somebody did not get in,
was refused by the limit, met a server error, or finished after the two minutes must never read as
a pass."""

import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "loadtest" / "report_sso.py"


def load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("report_sso", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def summary(
    ok: int = 100,
    failed: int = 0,
    server: int = 0,
    other: int = 0,
    limited: int = 0,
    last_ms: float = 110_000,
    failed_at_callback: int = 0,
) -> dict[str, object]:
    def counter(value: int) -> dict[str, float]:
        return {"count": value, "rate": 1.0}

    return {
        "metrics": {
            "signins_ok": counter(ok),
            "signins_failed": counter(failed),
            "signins_failed_at_callback": counter(failed_at_callback),
            "answers_2xx": counter(300),
            "answers_3xx": counter(300),
            "answers_429": counter(limited),
            "answers_5xx": counter(server),
            "answers_other": counter(other),
            "signin_ms": {"med": 2500, "p(95)": 4100, "max": 6000},
            "signin_done_at_ms": {"med": 50_000, "p(95)": 100_000, "max": last_ms},
        }
    }


def verdict(
    tmp_path: Path, data: dict[str, object], capsys: pytest.CaptureFixture[str], expected: int = 100
) -> int:
    path = tmp_path / "summary.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    status: int = load().main(str(path), expected)
    capsys.readouterr()
    return status


def test_everybody_in_within_two_minutes_and_no_refusal_passes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert verdict(tmp_path, summary(), capsys) == 0


@pytest.mark.parametrize(
    "broken",
    [
        {"ok": 99, "failed": 1},  # one person did not get in
        {"server": 1},  # one 5xx is one too many
        {"other": 1},  # a timeout or a refused connection
        {"limited": 1},  # the per-address limit turned somebody away: a finding, not a pass
        {"last_ms": 120_001},  # the last person came in after the two minutes
        {"ok": 0},  # nobody at all: the run never happened
    ],
)
def test_any_of_these_is_a_fail(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], broken: dict[str, int]
) -> None:
    assert verdict(tmp_path, summary(), capsys) == 0  # the control
    assert verdict(tmp_path, summary(**broken), capsys) == 1


@pytest.mark.parametrize(
    "counted",
    [
        {"ok": 99, "failed": 0},  # one person never counted (a script that stopped half way)
        {"ok": 50, "failed": 0},
        {"ok": 101, "failed": 0},  # more than the burst: the numbers do not belong to this run
    ],
)
def test_a_total_that_is_not_the_expected_number_of_people_is_a_fail(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], counted: dict[str, int]
) -> None:
    """Everybody who was counted signed in, and yet the burst did not happen as asked."""
    assert verdict(tmp_path, summary(), capsys) == 0  # the control
    assert verdict(tmp_path, summary(**counted), capsys) == 1


def test_an_expected_number_of_zero_is_a_fail(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert verdict(tmp_path, summary(ok=0, failed=0), capsys, expected=0) == 1


def test_the_command_line_needs_the_expected_number(tmp_path: Path) -> None:
    import subprocess
    import sys

    path = tmp_path / "summary.json"
    path.write_text(json.dumps(summary()), encoding="utf-8")
    for arguments in ([str(path)], ["--expected", "many", str(path)]):
        result = subprocess.run(  # noqa: S603 - fixed arguments, no untrusted input
            [sys.executable, "-I", str(SCRIPT), *arguments], capture_output=True, text=True
        )
        assert result.returncode != 0 and "usage" in result.stderr
    result = subprocess.run(  # noqa: S603 - fixed arguments, no untrusted input
        [sys.executable, "-I", str(SCRIPT), "--expected", "100", str(path)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0 and ": PASS" in result.stdout


def test_the_boundary_of_two_minutes_is_inside(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert verdict(tmp_path, summary(last_ms=120_000), capsys) == 0


def test_a_summary_without_the_time_of_the_last_sign_in_is_a_fail(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    data = summary()
    metrics = data["metrics"]
    assert isinstance(metrics, dict)
    del metrics["signin_done_at_ms"]
    assert verdict(tmp_path, data, capsys) == 1


def test_the_report_prints_the_people_where_they_failed_and_the_time(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "summary.json"
    path.write_text(
        json.dumps(summary(ok=97, failed=3, failed_at_callback=3, server=2)), encoding="utf-8"
    )
    load().main(str(path), 100)
    shown = capsys.readouterr().out
    assert "people: 100 of 100 expected (signed in 97, failed 3)" in shown
    assert "callback 3" in shown and "5xx 2" in shown
    assert "median 2500" in shown and "110 s after" in shown and ": FAIL" in shown
