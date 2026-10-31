# Progress

Management view, kept short. Task detail is in [TASKS.md](TASKS.md).

## Where the project is

- **Phase:** 1 (MVP). Milestones **M1** (skeleton and security baseline) and **M2** (sign-in, organization, onboarding) are complete. **M3** (sources and ingestion) is complete (2026-10-31): all 17 tasks are Done, including **M3-14** (real-stack end-to-end tests, CI job, whole-milestone review, documentation; merged as PR #53). Next: **M4** (the LLM layer, enrichment, the ranked feed, "Try this"): the plan is approved (2026-10-31) and M4-01, the two ADRs, is in review.
- **What exists:** a platform people can sign in to (single sign-on with any OpenID Connect provider, local break-glass administrators, roles, onboarding in four languages, audit log, Docker Compose stack). On top of it, M3 adds:
  - **Sources:** administrators and editors manage RSS and Atom feeds, GitHub release feeds and Hacker News searches; a starter catalogue of 25 sources approved by the maintainer is imported switched off; a trial fetch shows what a source would bring before it is switched on.
  - **Ingestion:** a dispatcher runs every minute and leases due sources; the worker fetches only through `SafeHttpClient` (addresses checked at connect time, redirects checked by hand, size and time limits, no internal or own-host addresses); hostile feed input is refused or cut; the pipeline removes duplicates and holds suspicious items for review.
  - **Articles:** members see current articles; administrators and editors hold, approve, hide and restore them. Feed text is shown as text only.
  - **Personal feeds:** each person may add up to 5 RSS feeds, at most one fetch an hour, visible to that person only; they stop being fetched when the person is disabled.
  - **Worker isolation:** the worker has its own database role and its own Redis user, so it cannot read or write sessions (F-14 closed).
- **What does not exist yet:** no LLM, no embeddings, no ranking, no "Try this" and no Reddit or commercial news source (the legal question is open).
- **Completion (counted from TASKS.md):** M1 10 of 10 tasks, M2 15 of 15, M3 17 of 17. Overall 3 of 5 milestones complete. These are counts, not effort estimates; the milestones differ a lot in size.
- **Schedule (estimate):** M3 was built in about three calendar days (2026-10-28 to 2026-10-31), including review rounds and a parallel session. That is an observation, not a basis for estimating M4 and M5.

## Checks (state on `main` after PR #52; the M3-14 pull request is pending)

| Check | Result |
| --- | --- |
| `make lint` (ruff, mypy strict, import contracts, eslint, tsc) | PASS |
| Backend: 1650+ unit tests | PASS |
| Backend: 817 integration tests (real PostgreSQL and Redis, worker role, Redis ACL users) | PASS |
| Frontend: 593 vitest tests | PASS |
| Frontend: whole Playwright suite against a mocked API (about 550 tests; four languages, two themes, desktop and phone, axe WCAG 2.2 AA) | PASS |
| `make test-compose` (Compose stack: isolation, hardening, TLS, forged forwarded headers) | PASS |
| `make test-stack-e2e` (real stack: Caddy over HTTPS, web, API, worker, beat, PostgreSQL, Redis, fake feed host): 104 tests, incl. ingestion, personal feeds and axe on `/items`, `/sources`, `/my-feeds` in 4 languages x 2 themes x 2 sizes | PASS |
| Pull-request pipeline (14 required checks) | PASS on `main` through PR #52; the M3-14 pull request is pending |
| Mutation checks (deliberately breaking each new control) | every new control caught by a named test; counts per task are in the TASKS.md rows and the pull-request descriptions |
| Independent whole-milestone security review | no open P0 or P1; its P2 findings fixed (an EBCDIC feed hid a document type declaration from the check; the SSRF stack targets mostly proved the port rule; conflicting specs) |
| `make test-sso-dev` (Keycloak stack, 35 checks) | NOT RUN in M3; last PASS on 2026-10-28 |
| Coverage | NOT MEASURED |
| Load, performance, backup and restore | NOT RUN |
| Real Entra ID tenant; a real internet feed in CI; a real GitHub or Hacker News API call through the connectors | NOT TESTED |

## M3 acceptance criteria

The ten criteria of [the M3 plan](docs/plans/m3-sources-and-ingestion.md). PASS means the named tests run and pass; PARTLY means the criterion is only partly shown; the detail is in the [plan's result section](docs/plans/m3-sources-and-ingestion.md#result).

| # | Criterion | Result |
| --- | --- | --- |
| 1 | Lint, unit, integration, Compose and stack tests green locally and in CI | PARTLY: green locally; CI for the M3-14 pull request pending |
| 2 | Each of the 12 listed mutations makes a named test fail | PARTLY: every control was broken in its task; the per-mutation list is in the pull requests and was not re-checked here |
| 3 | SSRF suite of at least 40 cases; real-stack targets blocked | PARTLY: 226 tests in `backend/tests/net`; the stack test asserts `blocked` and zero stored items, it does not count connections |
| 4 | As the worker role, `UPDATE users` and reading `local_credentials` fail; Redis `GET pharos:session:x` refused; F-14 closed | PASS |
| 5 | `http://127.1/` and `http://[::ffff:7f00:1]/` refused and audited | PASS |
| 6 | Two dispatchers start at most one fetch per source; killed worker leaves no duplicates; second run adds 0 | PARTLY: proven with a controlled clock and in the stack; a worker killed in a real container is not evidenced |
| 7 | 10,000 entries give 200 items, under 300 MB and under 20 s | PARTLY: count and time PASS; peak memory measured only as Python allocations in a unit test |
| 8 | Duplicate rate under 5% on a recorded corpus | PARTLY: the corpus is synthetic, not recorded |
| 9 | UI text in en, az, tr, ru; axe clean in 4 x 2 x 2 | PASS for presence and axe; az, tr and ru wording unreviewed (F-01) |
| 10 | Independent whole-milestone review with no open P0 or P1 | PASS |

## Risks

| Risk | State |
| --- | --- |
| R1 sensitive data pasted into an AI tool through "Try this" (P0) | Open by design; the card and admin tool list arrive with M4 |
| R2 pilot preparation (Entra registration, server, LLM budget) | Not started; blocks only the pilot (M0). Entra is the one provider path that has never met a real tenant |
| Worker takes hostile input from the internet | Contained by SafeHttpClient, the worker role and the Redis user; not proven against a real internet feed or a killed container |
| Egress by destination is not enforced by Compose | Open (F-04); M3 proves the address policy, not network egress |
| Strict CSP | Open (F-03); new pages render plain text only |
| Personal feed first fetch is due at once | Open (F-31): bounded by 10 new addresses an hour |
| Translations are unreviewed | Open (F-01), including the M3 text in az, tr and ru |
| Backup and restore undefined | Open (F-02); restore NOT RUN |
| Starter sources can move or die | Open (F-30): the Sources page shows failures; a periodic check is planned |
| Schedule: the plan assumes one developer with an assistant | M1 and M2 took about four calendar days and M3 about three (estimates, with review rounds and a parallel session); that is no basis for estimating M4 and M5 |

## Next steps

1. Merge the M3-14 pull request once CI is green, then mark M3-14 Done in TASKS.md.
2. Maintainer reviews the ADRs marked Proposed (0005 amendment, 0006, 0007, 0009, 0010, 0011, 0012, 0013, 0014; see [TASKS.md](TASKS.md#decisions-waiting-for-the-maintainer)), and decides F-31 (first fetch of a new personal feed).
3. Have the az, tr and ru text reviewed (F-01); decide the CSP (F-03) and the egress approach (F-04) before real data is stored.
4. Run the checks that are NOT RUN: load and performance, backup and restore (F-02), a worker killed in a real container, peak memory of the 10,000-entry feed in the stack, a recorded corpus for the duplicate rate, and the Entra ID path (M0-02).
5. M4 (the LLM layer, enrichment, the ranked feed, "Try this"): the plan is approved (2026-10-31, [docs/plans/m4-llm-layer-and-personal-feed.md](docs/plans/m4-llm-layer-and-personal-feed.md)) and M4-01 (ADR-0015 and ADR-0016) is written; next M4-02, the gateway. Still needed from the maintainer: pilot professions and the card format, the approved tool list, the Azerbaijani raters, the dependency `litellm`.
