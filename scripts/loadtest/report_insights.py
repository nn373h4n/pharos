"""Prints the numbers of the insight burst and the verdict on it (M5-08, decision D5).

    python3 scripts/loadtest/report_insights.py loadtest-results/summary-300.json

PASS when no answer was a server error (5xx), no request failed outright, and every person got an
insight that is ready (not left waiting, not `unavailable`). Worked out here from the numbers.
Exit status 0 when it held, 1 when it did not.
"""

import json
import sys


def count(metrics: dict[str, dict[str, float]], name: str) -> int:
    return int(metrics.get(name, {}).get("count", 0))


def main(path: str) -> int:
    with open(path, encoding="utf-8") as handle:
        metrics = json.load(handle)["metrics"]
    ready = count(metrics, "insights_ready")
    unavailable = count(metrics, "insights_unavailable")
    gave_up = count(metrics, "insights_gave_up")
    people = ready + unavailable + gave_up
    server = count(metrics, "answers_5xx")
    other = count(metrics, "answers_other")
    limited = count(metrics, "answers_429")
    requests = count(metrics, "answers_2xx") + limited + server + other
    print(
        f"  people: {people} (ready {ready}, unavailable {unavailable}, gave up waiting {gave_up})"
    )
    print(
        f"  answers: {requests} in all: 2xx {count(metrics, 'answers_2xx')}, 429 {limited}, "
        f"5xx {server}, other (0 is a timeout) {other}"
    )
    stats = metrics.get("insight_seconds_to_finish")
    if stats:
        print(
            f"  time until the insight was ready (ms): median {stats['med']:.0f}  "
            f"p95 {stats['p(95)']:.0f}  max {stats['max']:.0f}"
        )
    held = people > 0 and server == 0 and other == 0 and ready == people
    print(
        "  verdict on the insight burst (no 5xx, nobody left waiting or without an insight): "
        + ("PASS" if held else "FAIL")
    )
    return 0 if held else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
