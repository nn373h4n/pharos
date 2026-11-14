# M5 readiness report

- Task: M5-13 (whole-milestone security review, docs, readiness report)
- Date: 2026-11-14
- Code reviewed: `b9c4f4d` (the M5 plan approved) to `fad60c3` (M5, the follow-ups F-22 to F-61 and ADR-0028)
- Written by: the assistant. Each line below is PASS, FAIL or NOT RUN, as the plan requires.
  A gate that nobody has run is NOT RUN, never PASS.

## Verdict

**Not pilot ready yet.** The code side is in place except for three tasks that wait for gate G1:
M5-03 (egress), M5-05 (the worker boundary) and M5-06 (model-layer leftovers). Every gate from G1
to G11 needs people and is NOT RUN. Acceptance criteria 2, 4 and 6 are not met, because they
depend on those tasks, on a disposable host, or on a published release. No open P0 or P1 finding
was found.

## The pilot-ready checklist ([M5 plan](../plans/m5-pilot-readiness.md#gates-kind-b-passed-by-people-never-by-a-code-change))

| # | Check | Result |
| --- | --- | --- |
| 1 | M5 acceptance criteria 1 to 10 | **FAIL**: 1, 3, 5, 7, 8, 9 and 10 PASS; 2 partly; 4 and 6 NOT RUN (see below) |
| 2 | G1: the Proposed ADRs of D1 accepted | NOT RUN (the maintainer) |
| 3 | G2: internal approval and the pilot teams (M0-01) | NOT RUN |
| 4 | G3: F-36, the evaluation bar met in Azerbaijani per pilot profession by two native raters | NOT RUN |
| 5 | G4: F-01, the az wording reviewed by a native speaker (now including the hold phrases of F-24 and the master prompt's translation) | NOT RUN |
| 6 | G5: Entra ID registration and the real sign-in checklist on the pilot server | NOT RUN |
| 7 | G6: pilot server and domain; v0.1.0 installed by the runbook, valid TLS, `/readyz` green | NOT RUN (no release tag yet) |
| 8 | G7: egress rules on the pilot host, `scripts/egress-check.sh` PASS there | NOT RUN (M5-03 waits for G1) |
| 9 | G8: backups scheduled on the pilot server, an off-host copy, one restore rehearsed there | NOT RUN |
| 10 | G9: pilot key, budget and approved AI tool entered; "test the key" PASS | NOT RUN |
| 11 | G10: professions and sources; first fetches succeed; at least 20 cards per pilot profession | NOT RUN |
| 12 | G11: participants briefed on data safety (R1) | NOT RUN (the briefing template exists) |
| 13 | No open P0 or P1 | **PASS** for findings: two independent reviews on 2026-11-14 found none, and none is open from earlier reviews. The P0 risk R1 (sensitive data pasted into an AI tool) is reduced, not removed, and stays open as a behaviour risk |

## Acceptance criteria

| # | Criterion | Result | Evidence |
| --- | --- | --- | --- |
| 1 | `make lint`, `test`, `test-integration`, `test-compose`, `test-stack-e2e`, `test-sso-dev` green locally and in CI | PASS | CI requires 14 checks on `main`, among them Docker Compose, Single sign-on stack and Stack end-to-end, and every pull request merged on 2026-11-13 and 2026-11-14 passed them. Locally on 2026-11-14: `make lint` PASS, backend unit tests 3177 passed, the whole integration suite 1256 passed, front end 820 passed, `make test-compose` 180 PASS. Locally, `make test-stack-e2e` (130 tests) and `make test-sso-dev` (35 checks) last ran on 2026-11-07 (M5-12); since then they run only in CI |
| 2 | Each named mutation makes a named test fail, recorded in the pull request | **PARTLY**: PASS for 4 of 11; 5 NOT RUN; 2 not itemized | PASS: unpin a base image (PR #108, 14 mutations); an unsigned or tampered image passes verification (PR #110: fails for an unsigned image, a wrong key and a moved tag in the local rehearsal; PR #111: comparison disabled and digest pattern loosened, both caught); the retention job's boundary (PR #88, 22 mutations of `test_retention.py`); skip the dump check (PR #85: three mutations on the real stack, among them a restore that restores nothing). Not itemized: "restore without the roles" and "leave a person's interactions after deletion". PR #85 and #88 record rounds of mutations without naming these two. NOT RUN, because the code is not built (waits for G1): remove a firewall rule (M5-03), drop the F-33 policy and make a new personal feed due at once (M5-05), let GET queue an insight and skip a budget audit event (M5-06) |
| 3 | The restore rehearsal of M5-02 passes, with its time recorded | PASS | `make test-compose` backs up, destroys and restores a stack with synthetic data on every pull request. Backup and restore took 1 to 2 seconds on the small test database (PR #85); that says nothing about a large one |
| 4 | The egress check passes with positive controls on a disposable host | NOT RUN | M5-03 waits for G1; it stays gate G7 |
| 5 | The load targets of D5 met on the stated hardware; the resilience checks of M5-08 pass | PASS | 50 people for 10 minutes, 30,000 articles, 2 CPU threads: first page p95 134 ms, second page 85 ms, 0 failed (after F-49). After F-61, steps of 3 minutes with a period boundary inside: 300 people p95 140 ms, p99 707 ms, 0 failed. Resilience: the 10,000-entry feed with the worker's peak at 215 MiB (limit 300 MiB); a worker killed mid-fetch loses and doubles nothing. Insight burst: 100 of 100 ready, no 5xx. SSO burst: 100 of 100 within 100 s from one address, no 429 or 5xx (development Keycloak). Not proven: 30,000 users; Entra ID |
| 6 | A fresh install of v0.1.0 by the runbook alone works in a throwaway environment, time recorded against the two-hour target | NOT RUN | There is no tag yet. The runbook was rehearsed on images built from the branch: about 2 minutes of command time, no command failed. Reading time, DNS, certificates and SSO were not measured |
| 7 | Approved tools: Copy only by default; "Open in" only to a listed address; four locales, axe clean, keyboard | PASS | M5-07 (PR #86): the list is empty by default and the card offers Copy only. A link goes only to a listed https address, which is checked in the service, the database and the browser. Four locales; axe 4 × 2 × 2; keyboard tests. The 2026-11-14 review found nothing |
| 8 | Retention and deletion behave as D6 says, tested at the boundaries | PASS | M5-09 (PR #88): jobs tested at the boundary day; floors enforced; deletion removes the profile, password, interactions, feeds and sessions and leaves one entry without content. The 2026-11-14 review found that the address hash of a single sign-on person's first sign-in stayed; this is fixed on this branch with a test and three mutations caught |
| 9 | No secret, prompt or answer in the real stack's logs or audit log | PASS for the test provider | M5-12 (PR #92) searches the real stack's logs, the audit log and the usage table for a prompt marker and the test provider's answers, after proving they exist. The check was broken on purpose twice and both times a named test failed. `make test-compose` checks that `status.sh` prints no password. NOT RUN with a real model provider |
| 10 | An independent whole-milestone security review has no open P0 or P1 | PASS | Two reviews on 2026-11-14 (runtime and operations/supply chain, each a separate agent that did not edit anything): no P0 or P1. Findings and what was done are listed below. The fixes have not been reviewed a second time |

## The 2026-11-14 security review

Each review was a separate agent given the project's `security-reviewer` instructions and the
`security-checklist`. Each was read-only, and its scope was the diff above.

**Runtime** (backend, migrations, front end; about 5,000 lines): no P0, P1 or P2.

| Finding | Severity | What was done |
| --- | --- | --- |
| `delete-person` left the address hash on the `user.created` entry of a single sign-on person's first sign-in, against ADR-0025 | P3 | Fixed: the hash is cleared and counted in the dry run. An entry another person wrote keeps that person's hash. Integration test; three mutations caught |
| The host gate gave one machine two keys (Unicode from the source, ASCII from the client for a redirect) | P3 | Fixed: the key is made from the ASCII form. Unit test; mutation caught |
| A worker's DNS pool could be tied up from personal feeds (suspected) | P3 | Recorded as F-62 |

**Operations and supply chain** (scripts, Compose, workflows, runbook): no P0 or P1.

| Finding | Severity | What was done |
| --- | --- | --- |
| A `PHAROS_*_IMAGE` exported in the shell wins over `deploy/.env`, so the images that run might not be the ones verified | P2 | Fixed and shown on Compose. The overlay says never to export them, and the runbook checks `pharos config --images` before the first start and on every upgrade |
| Throwaway stacks took settings from the calling shell | P3 | Fixed: `scripts/lib/throwaway-env.sh`, used by all six scripts, with a unit test and a static test |
| Throwaway stacks published on every address | P3 | Fixed: 127.0.0.1 in every generated env file; static test |
| Load test files were readable by other local users | P3 | Fixed: work directory 700, files 600, k6 runs as the caller; static test. Run as root only on the development host |
| Nothing compared the checkout with the release commit | P3 | Fixed: `verify-release.sh` fails when run from a checkout at another commit, and git variables cannot redirect it; four tests, two mutations caught |
| The "never overwrite a release" check fails open (suspected) | P3 | Recorded as F-63. It was not changed, because GHCR's answer for a package that was never pushed is not known, and a check that matches the wrong words could block the first release |
| Docs: a restore brings deleted people back; break-glass sign-in is outside MFA | P3 | Fixed in DEPLOYMENT and the runbook |

Both reviews also list what they checked and found sound. They looked at:

- the shared feed ranking's tenant boundary and how it validates what it reads back;
- the worker boundary;
- approved AI tools;
- the operator commands;
- OIDC timing and deadlines;
- backup and restore;
- the release chain's permissions and pins;
- the Compose hardening.

## What M5 does not prove

As the plan says: the pilot's usefulness ratio, onboarding completion, weekly use and time saved;
what people paste into AI tools; the real daily cost; Azerbaijani quality beyond the rated
professions; 30,000 users; stability over weeks; Entra ID outside the pilot's directory; several
organizations; screen readers (F-28); source terms of service; that the pilot host's firewall stays
as configured.

## Open work, by owner

- **The maintainer (gates and decisions):**
  - G1 to G11;
  - the release tag (M5-10, F-07);
  - D1 (accept the Proposed ADRs), then M5-03, M5-05 and M5-06;
  - ADR-0029 (a second Redis for the worker);
  - approvals for F-26 (an index migration), F-34 (a migration), F-43 (minimatch) and F-58 (a pinned shellcheck image);
  - the open Dependabot pull requests;
  - F-56 (a) and (c) (the tag ruleset; requiring the release dry run).
- **People:**
  - F-01 (native-speaker review);
  - F-36 (raters);
  - F-19 (an IPv6 host);
  - F-28 (screen readers).
- **The assistant, after decisions:**
  - M5-03, M5-05 and M5-06 once G1 passes;
  - F-62 and F-63;
  - the rest of the follow-ups in `TASKS.md` (P3, none blocking).
