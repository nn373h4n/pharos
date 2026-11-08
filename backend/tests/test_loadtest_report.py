"""The verdict of the load-test report (M5-08): it is worked out from the numbers, so it is tested
at its edges and for the cases that must never read as a pass."""

import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "loadtest" / "report.py"


def load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("loadtest_report", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def trend(p95: float) -> dict[str, float]:
    return {"p(95)": p95, "med": p95 / 2, "p(99)": p95 * 1.2, "max": p95 * 2, "avg": p95 / 2}


def summary(
    first: float | None = 200.0,
    nxt: float | None = 200.0,
    failed: int = 0,
    fine: int = 1000,
) -> dict[str, object]:
    metrics: dict[str, object] = {
        "http_req_failed": {"passes": failed, "fails": fine},
        "checks": {"passes": 10, "fails": 0},
        "http_reqs": {"count": failed + fine, "rate": 5.0},
        "iterations": {"count": 500, "rate": 2.5},
    }
    if first is not None:
        metrics["http_req_duration{name:feed_first}"] = trend(first)
    if nxt is not None:
        metrics["http_req_duration{name:feed_next}"] = trend(nxt)
    return {"metrics": metrics}


def verdict(tmp_path: Path, data: dict[str, object], capsys: pytest.CaptureFixture[str]) -> int:
    path = tmp_path / "summary.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    status: int = load().main([str(path)])
    capsys.readouterr()
    return status


def test_a_run_inside_every_limit_passes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert verdict(tmp_path, summary(), capsys) == 0


def test_the_limit_itself_is_not_a_pass(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert verdict(tmp_path, summary(first=399.9), capsys) == 0  # the control: just inside
    assert verdict(tmp_path, summary(first=400.0), capsys) == 1  # "under 400 ms" means under
    assert verdict(tmp_path, summary(nxt=400.0), capsys) == 1


def test_one_percent_of_failed_requests_is_not_a_pass(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert verdict(tmp_path, summary(failed=9, fine=991), capsys) == 0  # 0.9 per cent
    assert verdict(tmp_path, summary(failed=10, fine=990), capsys) == 1  # 1.0 per cent


def test_missing_numbers_are_never_a_pass(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert verdict(tmp_path, summary(first=None), capsys) == 1
    assert verdict(tmp_path, summary(nxt=None), capsys) == 1
    # No requests at all (a run that never started) is not a clean run either.
    assert verdict(tmp_path, summary(failed=0, fine=0), capsys) == 1


def test_the_report_prints_the_numbers_and_the_verdict(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "summary.json"
    path.write_text(json.dumps(summary(first=21_000.0)), encoding="utf-8")
    load().main([str(path)])
    shown = capsys.readouterr().out
    assert "p95 21000 ms" in shown and "FAIL" in shown
    assert "verdict on the D5 feed targets: FAIL" in shown


def test_the_first_file_is_the_target_and_the_other_steps_say_where_it_stopped(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    files = []
    for people, p95 in ((50, 120.0), (150, 380.0), (300, 2_500.0)):
        path = tmp_path / f"summary-{people}.json"
        path.write_text(json.dumps(summary(first=p95, nxt=p95)), encoding="utf-8")
        files.append(str(path))
    status = load().main(files)
    shown = capsys.readouterr().out
    assert status == 0  # the first step (50 people) is the target and it held
    assert "steps inside the limits: 50, 150; outside: 300" in shown
    # The other way round: a failing target is a failing run, whatever the larger steps did.
    status = load().main(list(reversed(files)))
    capsys.readouterr()
    assert status == 1
