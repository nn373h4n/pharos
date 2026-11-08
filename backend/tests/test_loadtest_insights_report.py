"""The verdict of the insight-burst report (M5-08): a burst that left somebody waiting, or that was
answered with a server error even once, must never read as a pass."""

import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "loadtest" / "report_insights.py"


def load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("report_insights", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def summary(
    ready: int = 300,
    unavailable: int = 0,
    gave_up: int = 0,
    server: int = 0,
    other: int = 0,
    limited: int = 0,
) -> dict[str, object]:
    def counter(value: int) -> dict[str, float]:
        return {"count": value, "rate": 1.0}

    return {
        "metrics": {
            "insights_ready": counter(ready),
            "insights_unavailable": counter(unavailable),
            "insights_gave_up": counter(gave_up),
            "answers_2xx": counter(2000),
            "answers_429": counter(limited),
            "answers_5xx": counter(server),
            "answers_other": counter(other),
            "insight_seconds_to_finish": {"med": 9000, "p(95)": 20000, "max": 31000},
        }
    }


def verdict(tmp_path: Path, data: dict[str, object], capsys: pytest.CaptureFixture[str]) -> int:
    path = tmp_path / "summary.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    status: int = load().main(str(path))
    capsys.readouterr()
    return status


def test_everybody_ready_and_no_server_error_passes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert verdict(tmp_path, summary(), capsys) == 0


@pytest.mark.parametrize(
    "broken",
    [
        {"server": 1},  # one 5xx is one too many
        {"other": 1},  # a timeout or a refused connection
        {"ready": 299, "gave_up": 1},  # somebody still waiting at the end
        {"ready": 299, "unavailable": 1},  # somebody told there is no insight
        {"ready": 0},  # nobody at all: the run never happened
    ],
)
def test_any_of_these_is_a_fail(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], broken: dict[str, int]
) -> None:
    assert verdict(tmp_path, summary(), capsys) == 0  # the control
    assert verdict(tmp_path, summary(**broken), capsys) == 1


def test_a_429_is_shown_but_does_not_by_itself_fail_a_burst_that_finished(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The limit of asks for one person is a control working; the burst is judged by its result."""
    assert verdict(tmp_path, summary(limited=5), capsys) == 0


def test_the_report_prints_the_people_the_answers_and_the_time(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "summary.json"
    path.write_text(json.dumps(summary(server=2)), encoding="utf-8")
    load().main(str(path))
    shown = capsys.readouterr().out
    assert "people: 300 (ready 300" in shown and "5xx 2" in shown
    assert "median 9000" in shown and ": FAIL" in shown
