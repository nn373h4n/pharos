"""Prints the numbers of the single sign-on burst and the verdict on it (M5-08, decision D5).

    python3 scripts/loadtest/report_sso.py --expected 100 loadtest-results/summary-100.json

PASS when exactly the expected number of people were counted (a person whose script stopped before
being counted must not vanish from the total), every one of them signed in (own name on /api/me),
no answer was a server error (5xx) or a refusal by the limit (429), no request failed outright,
and the last person was signed in within two minutes of the start of the burst. The limit is a
control working, but D5 asks for the sign-ins to fit under it; so a 429 here is a FAIL of the
target and a finding about the limit.
Exit status 0 when it held, 1 when it did not.
"""

import json
import sys

DEADLINE_MS = 120_000
STEPS = ("start", "keycloak_form", "keycloak_submit", "callback", "me")


def count(metrics: dict[str, dict[str, float]], name: str) -> int:
    return int(metrics.get(name, {}).get("count", 0))


def main(path: str, expected: int) -> int:
    with open(path, encoding="utf-8") as handle:
        metrics = json.load(handle)["metrics"]
    ok = count(metrics, "signins_ok")
    failed = count(metrics, "signins_failed")
    people = ok + failed
    server = count(metrics, "answers_5xx")
    other = count(metrics, "answers_other")
    limited = count(metrics, "answers_429")
    requests = (
        count(metrics, "answers_2xx") + count(metrics, "answers_3xx") + limited + server + other
    )
    print(f"  people: {people} of {expected} expected (signed in {ok}, failed {failed})")
    if failed:
        where = ", ".join(f"{step} {count(metrics, f'signins_failed_at_{step}')}" for step in STEPS)
        print(f"  failed at: {where}")
    print(
        f"  answers: {requests} in all: 2xx {count(metrics, 'answers_2xx')}, "
        f"3xx {count(metrics, 'answers_3xx')}, 429 {limited}, 5xx {server}, "
        f"other (0 is a timeout) {other}"
    )
    stats = metrics.get("signin_ms")
    if stats:
        print(
            f"  one sign-in from start to own name (ms): median {stats['med']:.0f}  "
            f"p95 {stats['p(95)']:.0f}  max {stats['max']:.0f}"
        )
    done = metrics.get("signin_done_at_ms")
    last = float(done["max"]) if done else None
    if last is not None:
        print(f"  the last person was signed in {last / 1000:.0f} s after the burst began")
    held = (
        expected > 0
        and people == expected
        and ok == expected
        and server == 0
        and other == 0
        and limited == 0
        and last is not None
        and last <= DEADLINE_MS
    )
    print(
        "  verdict on the single sign-on burst (everybody signed in within 2 minutes, no 429, "
        "no 5xx): " + ("PASS" if held else "FAIL")
    )
    return 0 if held else 1


if __name__ == "__main__":
    if len(sys.argv) != 4 or sys.argv[1] != "--expected" or not sys.argv[2].isdigit():
        sys.exit("usage: report_sso.py --expected N summary.json")
    sys.exit(main(sys.argv[3], int(sys.argv[2])))
