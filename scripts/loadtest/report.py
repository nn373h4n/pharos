"""Prints the numbers of the k6 summaries and the verdict on each target of decision D5 (M5-08).

    python3 scripts/loadtest/report.py loadtest-results/summary-50.json [summary-150.json ...]

One file is one step of the run (the number in the name is the number of people). The verdict is
worked out here from the numbers, not read from k6's own flags. The target step is the first file:
exit status 0 when every target held there, 1 when one did not. The report also says how many
people the stack carried with p95 under the limit and where it stopped doing so.
"""

import json
import re
import sys
from pathlib import Path

P95_LIMIT_MS = 400.0
FAILED_LIMIT = 0.01


def step(path: str) -> tuple[bool, str, int | None]:
    """Prints one step; returns (held, p95 of the first page, people)."""
    with open(path, encoding="utf-8") as handle:
        metrics = json.load(handle)["metrics"]
    found = re.search(r"summary-(\d+)\.json$", Path(path).name)
    people = int(found.group(1)) if found else None
    print(f"  --- {people if people is not None else '?'} people ---")
    held = True
    first_p95: float | None = None
    for name, label in (
        ("http_req_duration{name:feed_first}", "first page of the feed"),
        ("http_req_duration{name:feed_next}", "second page of the feed"),
    ):
        stats = metrics.get(name)
        if stats is None:
            print(f"  {label}: NO DATA")
            held = False
            continue
        if name.endswith("feed_first}"):
            first_p95 = stats["p(95)"]
        ok = stats["p(95)"] < P95_LIMIT_MS
        held = held and ok
        print(
            f"  {label}: p95 {stats['p(95)']:.0f} ms (limit {P95_LIMIT_MS:.0f})  "
            f"median {stats['med']:.0f}  p99 {stats['p(99)']:.0f}  max {stats['max']:.0f}  "
            f"{'PASS' if ok else 'FAIL'}"
        )
    failed = metrics["http_req_failed"]
    total = failed["passes"] + failed["fails"]
    rate = failed["passes"] / max(1, total)
    ok = rate < FAILED_LIMIT and total > 0
    held = held and ok
    print(
        f"  failed requests: {failed['passes']} of {total} "
        f"({rate * 100:.2f}%, limit {FAILED_LIMIT * 100:.0f}%)  {'PASS' if ok else 'FAIL'}"
    )
    answers = {
        label: int(metrics[name]["count"])
        for name, label in (
            ("answers_2xx", "2xx"),
            ("answers_429", "429 (the limit of 300 an hour for one person)"),
            ("answers_5xx", "5xx"),
            ("answers_other", "other (0 is a timeout or a refused connection)"),
        )
        if name in metrics
    }
    if answers:
        print("  answers: " + ", ".join(f"{label} {count}" for label, count in answers.items()))
    checks = metrics["checks"]
    print(f"  checks: {checks['passes']} passed, {checks['fails']} failed")
    print(
        f"  requests: {metrics['http_reqs']['count']} ({metrics['http_reqs']['rate']:.1f}/s), "
        f"visits: {metrics['iterations']['count']}"
    )
    return held, "" if first_p95 is None else f"{first_p95:.0f}", people


def main(paths: list[str]) -> int:
    results = [step(path) for path in paths]
    target_held = results[0][0]
    print(f"  verdict on the D5 feed targets: {'PASS' if target_held else 'FAIL'}")
    if len(results) > 1:
        carried = [people for held, _, people in results if held and people is not None]
        failed = [people for held, _, people in results if not held and people is not None]
        print(
            "  steps inside the limits: "
            + (", ".join(str(n) for n in carried) if carried else "none")
            + "; outside: "
            + (", ".join(str(n) for n in failed) if failed else "none")
        )
    return 0 if target_held else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
