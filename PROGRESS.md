# Progress

Management view, kept short. Task detail is in [TASKS.md](TASKS.md).

## Where the project is

- **Phase:** 1 (MVP), milestone **M1 complete** on 2026-10-27. Next: M2 (sign-in, organization, onboarding), not planned yet.
- **What exists:** a runnable but empty platform. A placeholder home page in four languages, `/api/healthz` and `/api/readyz`, a worker with a heartbeat, a database with row-level security and one table, the Docker Compose stack, and the delivery pipeline. There is no sign-in, no content, no LLM.
- **Completion:** 10 of 10 M1 tasks done (measured). Overall, 1 of 5 milestones is complete; that is a count, not an effort estimate, because the milestones differ a lot in size.

## Checks (state on `main` after the last merge)

| Check | Result |
| --- | --- |
| Pull-request pipeline (12 required checks, since M1-09) | PASS on the pipeline pull request and on the Dependabot pull requests that were merged; earlier pull requests (#1-#9) predate it and were verified locally |
| Backend: 110 unit, 21 integration (real PostgreSQL and Redis) | PASS |
| Frontend: 31 vitest, 52 Playwright (four languages, desktop and phone, axe WCAG 2.2 AA) | PASS |
| Compose stack: 41 checks (isolation with positive controls, hardening, TLS, database login rules) | PASS |
| Script tests: 126 guard cases, 14 env generator, 10 DCO | PASS |
| Mutation checks (deliberately breaking security controls) | 21 of 21 caught (backend 9, frontend 4, Compose 8); two checks were weak at first (the pool leak test, the network probes) and were fixed |
| Independent security reviews | M1-04, M1-05, M1-08: no P0; all P1 and P2 findings fixed or documented |
| Coverage | NOT MEASURED |
| Load, performance, backup restore | NOT RUN |
| Real identity provider, real LLM, a public domain with ACME | NOT TESTED |

## Risks

| Risk | State |
| --- | --- |
| R1 sensitive data pasted into an AI tool through "Try this" (P0) | Open by design; the card and admin tool list arrive with M4 |
| R2 pilot preparation (Entra registration, server, LLM budget) | Not started; blocks only the pilot (M0) |
| Egress by destination is not enforced by Compose | Open, documented (F-04) |
| Translations are unreviewed | Open (F-01) |
| Backup and restore undefined | Open (F-02) |
| Schedule: the plan assumes one developer with an assistant | M1 was built in two calendar days including review rounds; that is no basis for estimating M2-M5 |

## Next steps

1. Maintainer reviews the ADRs marked Proposed and the ADR-0005 amendment ([TASKS.md](TASKS.md#decisions-waiting-for-the-maintainer)).
2. Plan M2 and get it approved: sign-in, sessions, RBAC, organization bootstrap, onboarding.
3. Start the M0 items that take calendar time: server and Entra ID registration (M0-02, M0-03).
4. Have the az, tr and ru translations reviewed (F-01).
5. Decide the egress approach (F-04) before real data is stored.
