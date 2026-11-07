"""Prints the numbers of a k6 summary and the verdict on each target of decision D5 (M5-08).

    python3 scripts/loadtest/report.py loadtest-results/summary.json

The verdicts are worked out here from the numbers, not read from k6's own flags. Exit status is 0
when every target held and 1 when one did not.
"""

import json
import sys

P95_LIMIT_MS = 400.0
FAILED_LIMIT = 0.01


def main(path: str) -> int:
    with open(path, encoding="utf-8") as handle:
        metrics = json.load(handle)["metrics"]
    held = True
    for name, label in (
        ("http_req_duration{name:feed_first}", "first page of the feed"),
        ("http_req_duration{name:feed_next}", "second page of the feed"),
    ):
        stats = metrics.get(name)
        if stats is None:
            print(f"  {label}: NO DATA")
            held = False
            continue
        ok = stats["p(95)"] < P95_LIMIT_MS
        held = held and ok
        print(
            f"  {label}: p95 {stats['p(95)']:.0f} ms (limit {P95_LIMIT_MS:.0f})  "
            f"median {stats['med']:.0f}  p99 {stats['p(99)']:.0f}  max {stats['max']:.0f}  "
            f"{'PASS' if ok else 'FAIL'}"
        )
    failed = metrics["http_req_failed"]
    rate = failed["passes"] / max(1, failed["passes"] + failed["fails"])
    ok = rate < FAILED_LIMIT and failed["passes"] + failed["fails"] > 0
    held = held and ok
    print(
        f"  failed requests: {failed['passes']} of {failed['passes'] + failed['fails']} "
        f"({rate * 100:.2f}%, limit {FAILED_LIMIT * 100:.0f}%)  {'PASS' if ok else 'FAIL'}"
    )
    checks = metrics["checks"]
    print(f"  checks: {checks['passes']} passed, {checks['fails']} failed")
    print(
        f"  requests: {metrics['http_reqs']['count']} ({metrics['http_reqs']['rate']:.1f}/s), "
        f"visits: {metrics['iterations']['count']}"
    )
    print(f"  verdict on the D5 feed targets: {'PASS' if held else 'FAIL'}")
    return 0 if held else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
