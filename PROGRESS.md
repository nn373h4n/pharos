# Progress

Management view, kept short. Task detail is in [TASKS.md](TASKS.md).

## Where the project is

- **Phase:** 1 (MVP). Milestones **M1** (skeleton and security baseline) and **M2** (sign-in, organization, onboarding) are complete. **M3** (sources and ingestion) is complete (2026-10-31): all 17 tasks are Done, including **M3-14** (real-stack end-to-end tests, CI job, whole-milestone review, documentation; merged as PR #53). **M4** (the LLM layer, enrichment, the ranked feed, "Try this", the local model profile) has all 11 tasks Done (M4-05 was cut on 2026-11-01, un-cut by the maintainer on 2026-11-03 and built; it is Done with the limits in its TASKS.md row, and `make test` had one unrelated flaky front-end failure, F-41). M4-10 (local model) is Done, on a pull request not yet merged. M4-09 delivers the evaluation harness only: the first real evaluation run needs native-speaking raters and a provider key (F-36), so the Azerbaijani quality is unmeasured. Next: **M5** (pilot readiness: backup and restore, release, the pilot itself).
- **What exists:** a platform people can sign in to (single sign-on with any OpenID Connect provider, local break-glass administrators, roles, onboarding in four languages, audit log, Docker Compose stack). On top of it, M3 adds:
  - **Sources:** administrators and editors manage RSS and Atom feeds, GitHub release feeds and Hacker News searches; a starter catalogue of 25 sources approved by the maintainer is imported switched off; a trial fetch shows what a source would bring before it is switched on.
  - **Ingestion:** a dispatcher runs every minute and leases due sources; the worker fetches only through `SafeHttpClient` (addresses checked at connect time, redirects checked by hand, size and time limits, no internal or own-host addresses); hostile feed input is refused or cut; the pipeline removes duplicates and holds suspicious items for review.
  - **Articles:** members see current articles; administrators and editors hold, approve, hide and restore them. Feed text is shown as text only.
  - **Personal feeds:** each person may add up to 5 RSS feeds, at most one fetch an hour, visible to that person only; they stop being fetched when the person is disabled.
  - **Worker isolation:** the worker has its own database role and its own Redis user, so it cannot read or write sessions (F-14 closed).
  - **Model layer (M4):** every call goes through one gateway on the LiteLLM SDK; only public article text is sent, masked, between random delimiters, with no tools and a schema-checked answer; the key lives in the worker only; the day's spending is capped (80% warning, 100% stop, insights limited to half of the day so reading goes on). Near-duplicate articles are shown as one card per story when an embedding provider is configured (off by default); the duplicate rate is measured only on a synthetic corpus with a demo embedder. The worker reads new articles (summary, category, quality, fit to each profession), people see a ranked Home feed with Save, Useful and Not useful, and **Try this** shows how an article applies to their profession with a prompt to copy and a data note. An evaluation harness (`python -m pharos.evaluation`) is ready for the native-speaker run.
- **What does not exist yet:** a duplicate rate measured on a real model or a real corpus (M4-05 has only the synthetic check), any measurement of the Azerbaijani output quality or of the local model's answers (F-36), the approved AI tool list and the "Open in" link (F-35), and Reddit or commercial news sources (the legal question is open).
- **Completion (counted from TASKS.md):** M1 10 of 10 tasks, M2 15 of 15, M3 17 of 17, M4 11 of 11 (M4-10 and M4-05 are on pull requests not yet merged). Overall 4 of 5 milestones complete. These are counts, not effort estimates; the milestones differ a lot in size.
- **Schedule (estimate):** M3 was built in about three calendar days (2026-10-28 to 2026-10-31), including review rounds and a parallel session. M4 took about two calendar days (2026-10-31 to 2026-11-02) with review rounds. These are observations, not a basis for estimating M5.

## Checks (state of the M4-11 branch, 2026-11-02; the pull request is pending)

| Check | Result |
| --- | --- |
| `make lint` (ruff, mypy strict, import contracts, eslint, tsc) | PASS |
| Backend: 2182 unit tests | PASS |
| Backend: 993 integration tests (real PostgreSQL and Redis, worker role, Redis ACL users) | PASS |
| Frontend: 703 vitest tests | PASS |
| Real stack, `llm-feed.spec.ts` (7 tests: the worker reads articles with the test provider, cards, Try this, a stored save, spending page, 80% warning, 100% stop with cached cards still shown, the key canary absent from responses, tables and logs) | PASS |
| Whole real-stack suite (`make test-stack-e2e`), `make test-compose`, the Playwright suite against a mocked API | NOT RUN in this task; run by the pull-request pipeline |
| Mutation checks (deliberately breaking each new control) | every control of M4-09 was caught by a named test (one equivalent check removed); in M4-11 two were broken in the real stack and caught: removing the 100% stop, and logging the key |
| Independent whole-milestone security review (M4) | no open P0 or P1; its P2 (one member could use up the day's model spending) is fixed (F-38); F-33 stays P2 and open; P3s in F-39 and F-40 |
| `make test-sso-dev` (Keycloak stack, 35 checks) | NOT RUN in M3 or M4; last PASS on 2026-10-28 |
| Coverage | NOT MEASURED |
| A real model provider; native-speaker rating of the Azerbaijani output (F-36); load beyond 3,000 articles; backup and restore | NOT RUN |
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

## M4 acceptance criteria

The ten criteria of [the M4 plan](docs/plans/m4-llm-layer-and-personal-feed.md#acceptance-criteria-measurable). PASS means the named tests run and pass; PARTLY means only part is shown; NOT RUN means nobody has done it yet.

| # | Criterion | Result |
| --- | --- | --- |
| 1 | Lint, unit, integration, Compose and stack tests green locally and in CI | PARTLY: lint, unit, integration and the new real-stack spec are green locally; the whole stack suite and Compose run in the pull-request pipeline |
| 2 | Each mutation of the list makes a named test fail | PARTLY: every control was broken in its own task (counts in the pull requests); two more were broken in the real stack in M4-11; the whole list was not re-run as one set |
| 3 | Budget: 80% warns, 100% stops new generation, cached cards still show (real stack, test provider) | PASS |
| 4 | At least 40 hostile articles give only schema-valid, link-limited, text-only output | PASS (M4-04) |
| 5 | No secret, prompt or answer in logs or the audit log | PARTLY: unit tests read logs for secrets and prompts; in the real stack the key was searched for in responses, five tables and every log; prompts and answers were not searched in the real stack's logs |
| 6 | First sign-in shows at least 20 cards; feed p95 under 400 ms; duplicate rate under 5% | PARTLY: p95 about 58 ms on 3,000 synthetic articles; twenty cards a page; the duplicate rate is measured only on a synthetic corpus with a demo embedder (M4-05); no real model or language is measured |
| 7 | Azerbaijani insight quality: mean 4 of 5 from native raters, 30 to 50 items for each of three professions | NOT RUN: the harness is built (M4-09); the run needs raters and a key (F-36). Not claimed |
| 8 | "Try this": copy and open in an approved tool; a data note on every card; clicks as aggregates only | PARTLY: copy, the data note and the aggregates are built; "open in" waits for the approved tool list (F-35) |
| 9 | All text in four languages; axe clean in 4 x 2 x 2; keyboard use | PASS for presence, axe and keyboard flows; az, tr and ru wording unreviewed (F-01) |
| 10 | Independent whole-milestone review with no open P0 or P1 | PASS (one P2 fixed; F-33 P2 open and recorded) |

## Risks

| Risk | State |
| --- | --- |
| R1 sensitive data pasted into an AI tool through "Try this" (P0) | Reduced: every card shows a data note and the copy flow asks for harmless text; the approved tool list is still missing (F-35), and a person can paste anything into any tool |
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

1. Merge PR #76 (M4-10), then the M4-05 pull request (stacked on it) (F-41, a flaky front-end test, is tracked apart). Then M4 is merged. Run the NOT RUN checks of M4-05 (stack end-to-end, `make test-compose`, a real embedding provider) before calling its quality shown.
2. Maintainer: accept or change the ADRs marked `Proposed` (see [TASKS.md](TASKS.md#decisions-waiting-for-the-maintainer)); they now include ADR-0015 to ADR-0018 (what may reach a model, budget, enrichment, the feed and interactions) besides the older ones. Decide F-31 (first fetch of a new personal feed).
3. The Azerbaijani quality run (F-36): find native-speaking raters (decision D6), give the worker a provider key and the candidate models, and follow [docs/guides/evaluation.md](docs/guides/evaluation.md). Until then the default models are only the settings' values and the quality is unmeasured. Also: the approved AI tool list (F-35, decision D5).
4. Have the az, tr and ru text reviewed (F-01) and decide the egress approach (F-04) before real data is stored.
5. Run the checks that are NOT RUN: load and performance, backup and restore (F-02), a worker killed in a real container, peak memory of the 10,000-entry feed in the stack, a recorded corpus for the duplicate rate, and the Entra ID path (M0-02).
6. M5 (pilot readiness): plan it, get the plan approved, then build. The maintainer decides ADR-0007, ADR-0020 and ADR-0021 (all `Proposed`); the quality of the embeddings and of the local model waits for raters (F-36).
