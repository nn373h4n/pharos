# Tasks

Source of truth for tasks. Status: Todo / In Progress / Blocked / Done. Priority: P0 (blocks everything) to P3 (nice to have).
Milestone status is in [PROGRESS.md](PROGRESS.md); decisions are in [docs/adr/](docs/adr/).

## M1 — Skeleton and security baseline (complete)

All ten tasks were merged through pull requests. The pipeline arrived with M1-09, so PRs #1-#9 were verified locally and the later ones also by the pipeline. "Evidence" names the checks that proved the acceptance criterion.

| ID | P | Status | Task | Acceptance criterion | Evidence |
| --- | --- | --- | --- | --- | --- |
| M1-01 | P0 | Done | Repo skeleton, `.gitignore`, Makefile | `make help` lists every target | PR #1 |
| M1-04 | P0 | Done | Claude Code guard hooks and gitleaks pre-commit | Bypass forms of the deny list are blocked, normal commands pass | 126 guard cases PASS; a staged fake token is rejected; PR #2, #5 |
| M1-02 | P1 | Done | ADR 0001-0010, architecture, data model, threat model v0 | ADR template, statuses as decided | markdownlint PASS; PR #3 (0010: PR #9) |
| M1-03 | P1 | Done | Open-source files | README, DCO, Code of Conduct, issue and PR templates | link check PASS; `LICENSE` identical to the Apache-2.0 text; PR #4 |
| M1-05 | P0 | Done | Backend skeleton, JSON log masking, Alembic baseline with RLS | `/healthz`, `/readyz`; secrets masked; cross-organization RLS test | 110 unit + 21 integration PASS; 9 mutation checks; independent security review; PR #6 |
| M1-06 | P1 | Done | Celery worker, beat, heartbeat | Heartbeat every minute, visible in `/readyz` | integration with a real worker PASS; measured 59.96 s between beats; PR #7 |
| M1-07 | P1 | Done | Frontend skeleton in four languages | Missing key stops the build; axe: no violations | 31 vitest + 52 Playwright PASS; build fails on a missing key; PR #8 |
| M1-08 | P0 | Done | Docker Compose, networks, non-root, `.env.example` | Stack opens over HTTPS; database and Redis cannot reach the internet | `make test-compose` 41 checks PASS; 8 mutation checks; independent security review; PR #9 |
| M1-09 | P1 | Done | CI, security scans, Dependabot, branch protection | A failing check blocks the merge | A deliberately failing PR was blocked (Backend and DCO failed, merge refused); PR #10 |
| M1-10 | P2 | Done | TASKS, PROGRESS, CHANGELOG, session note | Statuses match reality; percentages only from measurable tasks | this pull request |

## M0 — Pilot readiness (owner: maintainer, parallel, blocks the pilot only)

| ID | P | Status | Task | Blocker or note |
| --- | --- | --- | --- | --- |
| M0-01 | P1 | Todo | Internal approval for the pilot and the pilot teams | Maintainer |
| M0-02 | P1 | Todo | Entra ID app registration in the pilot organization | Needed by M5; development uses Keycloak |
| M0-03 | P1 | Todo | Pilot server, plus a server and domain for the public demo | Compose needs Docker Engine 26+; see [DEPLOYMENT.md](docs/DEPLOYMENT.md) |
| M0-04 | P1 | Todo | LLM test key for development; pilot key, monthly budget, approved AI tool | Needed before M4 can use a real provider |
| M0-05 | P2 | Todo | Pilot professions, first source list, check of the name "Pharos" | Open questions 8, 12 and risk R15 |

## Follow-ups from reviews (none block the next milestone)

| ID | P | Status | Task | Origin |
| --- | --- | --- | --- | --- |
| F-01 | P1 | Todo | Native-speaker review of the az, tr and ru translations | Written by the assistant in M1-07; needed before the pilot |
| F-02 | P1 | Todo | Back up and restore procedure, rehearsed | Open in the architecture proposal (question 10); needed by M5 |
| F-03 | P1 | Todo | Strict Content-Security-Policy, with the sanitization work | ADR-0009; THREAT_MODEL residual risk |
| F-04 | P1 | Todo | Restrict which hosts `api`, `worker` and `beat` can reach (egress proxy profile or firewall guide) | ADR-0010; main gap against "default-deny egress" |
| F-05 | P2 | Todo | Pin base images and tools by digest; keep Dependabot updating them | Review of M1-08 |
| F-06 | P2 | Todo | Host firewall rule for the Docker bridge gateway: test it, then document the exact rule | ADR-0010; reasoned, not tested |
| F-07 | P2 | Todo | Add SBOM generation and image signing to releases | Proposal section 7, by M5 |
| F-08 | P3 | Todo | Run `caddy` as a non-root user | ADR-0010 |
| F-09 | P3 | Todo | Reject passwords with URL-unsafe characters at start-up | Review of M1-08 |
| F-10 | P3 | Todo | Cache or rate limit `/api/readyz` (it is hidden from the internet already) | Review of M1-05 |
| F-11 | P3 | Todo | Move to ESLint 10 and TypeScript 7 when `eslint-config-next` and `typescript-eslint` support them | M1-07; Dependabot ignores these majors |
| F-12 | P3 | Todo | English version of `PHAROS_MASTER_PROMPT.md` before the public launch (G3) | Proposal decision 7 |
| F-13 | P3 | Done | Trust `X-Forwarded-*` headers from the proxy in the API | Done in M2-03b: trusted from Caddy's pinned address only |
| F-14 | P2 | Done | Keep the worker out of session records in Redis (ACL users or a MAC on the record) | Closed by M3-02: Redis ACL users, see THREAT_MODEL |
| F-15 | P3 | Done | Tie a session to how the user signed in (local or SSO) and end local sessions when `PHAROS_LOCAL_LOGIN` is switched off or an admin loses break-glass rights | Review of M2-04 (P3-3). The session records the method (M2-05); local sessions end when password sign-in is switched off (M2-05); demoting or disabling a person ends their sessions (M2-07); and under `break-glass` every request refuses a password session of a non-administrator, so a sign-in racing a demotion or a tightened mode ends at the next request (whole-milestone review) |
| F-16 | P3 | Todo | Verification time of hashes made with older parameters differs from the dummy hash until they are rehashed | Review of M2-04 (P3-2); small timing signal, only for accounts not yet upgraded |
| F-17 | P3 | Todo | OIDC client hardening: refetch the signing keys on a TTL (a withdrawn key stays trusted until restart) and put one overall deadline on each provider call (the 10 s timeout is per chunk) | Security review of M2-05 (P3-2, P3-4) |
| F-18 | P3 | Done | Negative test for the Entra `common` tenant issuer | Security review of M2-05 (P3-7); done in M2-06: config tests refuse `common` and an issuer that disagrees with the tenant |
| F-24 | P3 | Todo | Pipeline leftovers: items cannot be re-held when a source edits flagged text (it is dropped silently; the app needs a `content_changed` signal, which is a data-model change and an ADR); equal links in different spellings stay separate items (parameter order, Unicode versus punycode host, `%7E`); hold phrases are English-only and ignore look-alike letters (az, tr, ru phrases); the `item_sources` lookup was not checked with `EXPLAIN` on a large organization; the lock-order deadlock between two concurrent fetches was fixed by ordering but not reproduced | Security review of M3-08 |
| F-23 | P3 | Todo | Scheduler: the in-flight count and the lease are not one atomic step (two dispatchers can exceed the cap of 40 by up to a batch); entries that never change keep their old `fetched_at`, so after 14 days they are stored again as new raw items (the M3-08 pipeline must treat an unchanged hash as a no-op); the host gate checks the configured host only, not a host reached by redirect | Security review of M3-07 |
| F-22 | P3 | Todo | `RssConnector.dedupe_key` hosts use the standard library's IDNA 2003 codec, so two spellings of one international host (for example `ß` and `xn--zca`) could register as two sources; compare with the name `httpx` really connects to | Security review of M3-06 |
| F-21 | P3 | Todo | The scheduler (`beat`) only needs Redis but still receives the worker's database URL because `Settings` demands one in production: add a process type for it that needs none | Security review of M3-03 |
| F-20 | P3 | Todo | SafeHttpClient: also refuse the addresses that `PHAROS_PUBLIC_URL` resolves to (today only the name is refused); run DNS lookups in a bounded executor so a name server that never answers cannot tie up the default thread pool; make `Settings` immutable after start-up (defence in depth for the test-only allowance) | Security review of M3-04 (all P3, closing in M3-14 at the latest) |
| F-19 | P3 | Todo | Check that Caddy sees real IPv6 client addresses behind Docker's port publishing; if it does not, all IPv6 users share one rate-limit bucket | Whole-milestone review of M2 (not verifiable without a host with IPv6) |

## Decisions waiting for the maintainer

- Accept or change ADR-0012 (worker identity and ingestion boundaries, M3), ADR-0011 (sessions, CSRF, identity-provider trust, organization resolution; implemented and reviewed in M2), ADR-0006 (connector plugins), ADR-0007 (embeddings; needs the Azerbaijani evaluation), ADR-0009 (output sanitization, adds `nh3`) and ADR-0010 (network topology). All are `Proposed`.
- ADR-0005 was amended during the M1-05 review (separate read and write policies on content tables); confirm the amendment.
- Questions 7-12 in `docs/ARCHITECTURE_PROPOSAL.md` section 16 (team size, pilot professions, card format, retention, fully local mode, source list).

## M2 — Sign-in, organization, onboarding (plan approved 2026-10-27)

Plan: [docs/plans/m2-sign-in-organization-onboarding.md](docs/plans/m2-sign-in-organization-onboarding.md). Decisions: [ADR-0011](docs/adr/0011-sessions-csrf-idp-trust-and-organization-resolution.md) (`Proposed`). One pull request per task; execution order is the order of the rows.

| ID | P | Status | Task | Depends on | Acceptance criterion |
| --- | --- | --- | --- | --- | --- |
| M2-01 | P0 | Done | ADR-0011, plan copied into the repository, dependency approval | — | ADR in the template; plan and tasks recorded |
| M2-02 | P0 | Done | Migration 0002: departments, professions, users, local credentials, profiles, audit log, org lookup function | 01 | RLS test per table; cross-organization foreign keys rejected; audit log append-only; column grants; downgrade round trip |
| M2-10a | P1 | Done | Keycloak spike: dev profile behind Caddy, issuer hostname, CA trust | 01 | The API validates a Keycloak token through the proxy hostname |
| M2-03a | P0 | Done | Error format, clock, principal, unit of work, `current_user`, settings, lazy organization, route-coverage gate | 02 | A failing commit never returns 200; the gate fails on an uncovered route |
| M2-03b | P0 | Done | Session store, CSRF, Origin check, cookies, pinned proxy trust | 03a | Revocation, fixation, Origin and forged forwarded-header tests |
| M2-03c | P1 | Done | Rate limiter, audit service, event catalogue | 03b | Concurrent attempts counted exactly; audit entries without secrets |
| M2-04 | P0 | Done | Local accounts: login, logout, `/me`, operator CLI | 03c | Break-glass admin can sign in; no user enumeration; no hard lockout |
| M2-08 | P1 | Done | Profile and onboarding API, professions, interest codes | 03a | Profile validated against its organization; timestamps stored |
| M2-05 | P0 | Done | Generic OIDC with a fake-IdP negative suite, user creation on first sign-in | 03c | Every negative case in the plan is rejected; the first sign-in upsert never writes `role` or `status`; display names are truncated to 200 characters |
| M2-06 | P1 | Done | Entra ID preset (tenant-pinned issuer, Graph prefill) | 05 | Tested against recorded or mocked Entra shapes; real Entra NOT TESTED |
| M2-07 | P0 | Done | RBAC dependency, role and status endpoints, user list | 04 | Role × endpoint matrix; role change and disabling apply on the next request; two admins cannot demote each other concurrently (row lock) |
| M2-09a | P1 | Done | Frontend: login page, app shell, 401 handling | 04 | Keyboard-only sign-in; axe clean in 4 languages |
| M2-09b | P1 | Done | Frontend: onboarding stepper | 08, 09a | Keyboard-only; completes in under 2 minutes in a timed run |
| M2-10b | P1 | Done | Compose wiring, `make env` additions, identity-provider guides | 10a, 05 | `make test-compose` still passes with the new settings |
| M2-11 | P0 | Done | Stack E2E through Keycloak, CI job, security review, documentation | all | All M2 acceptance criteria in the plan |

## M3 — Sources and ingestion (plan approved 2026-10-28)

Plan: [docs/plans/m3-sources-and-ingestion.md](docs/plans/m3-sources-and-ingestion.md). Decisions: [ADR-0012](docs/adr/0012-worker-identity-and-ingestion-boundaries.md) (`Proposed`), ADR-0006 and ADR-0009 (`Proposed`, to be accepted by the maintainer). New dependencies approved with the plan: `feedparser`, `nh3`, `defusedxml`.

| ID | P | Status | Task | Depends on | Acceptance criterion |
| --- | --- | --- | --- | --- | --- |
| M3-01 | P0 | Done | Plan copied into the repository, ADR-0012, tasks recorded | — | ADR in the template; plan and tasks recorded |
| M3-02 | P0 | Done | Close F-14: Redis ACL users for the API and the worker | 01 | `deploy/redis/start.sh`; `test_redis_acl.py` (NOPERM on sessions, rate limits, sign-in state, dangerous commands, SCAN) with positive controls; the real Celery worker runs as the limited user; 14 rule mutations caught; compose smoke checks a live session record |
| M3-03 | P0 | Done | `pharos_worker` database role, per-process loop and engine, task time limits, beat off `egress` | 01 | Role from the init script with no table privilege until M3-05 (allow-list test), start-up role check with an upgrade hint, lazy fork-safe loop and engine, time limits and recycled children, beat on `data` only; 17 mutations caught, two review rounds fixed (cancelled tasks, wider role check) |
| M3-04 | P0 | Done | SafeHttpClient and the SSRF suite | 01 | 226 tests in `backend/tests/net` (address ranges, URL rules, scripted network, real sockets); 20 mutations caught, recorded in the PR; security review done, its P2 findings fixed and tested (F-20 keeps the P3s) |
| M3-05 | P0 | Done | Migration 0003: sources, fetch runs, raw items, items, item sources; RLS and grants | 03 | Tenant RLS on all five tables, personal rows hidden from others by a restrictive policy (`app.user_id`), owner trigger, column grants for both roles, about 130 tests incl. exact privilege sets for both roles; 28 mutations caught (the `WITH CHECK` removal is equivalent: PostgreSQL reuses `USING`); two review rounds fixed (hourly limit and five-feed limit in the database, attribution, hidden items, wider link check, policies for every role) |
| M3-06 | P0 | Done | Connector protocol, RSS/Atom connector, parser hardening | 04, 05 | `pharos.ingest` (connector protocol, RSS connector, feed parser, text and link cleaning); about 280 tests including a hostile-feed corpus; 37 mutations caught (equivalent: the `parse_feed(str)` guard, and the UTF-32 scan, since feedparser cannot read such feeds); review found a P0 (feedparser opened a body such as `/dev/zero` as a file) and fixed it; a 10,000-entry feed is cut to 200 in about 10 s under tracemalloc |
| M3-07 | P0 | Done | Dispatcher, lease, retry/backoff, circuit breaker, cleanup, politeness | 06 | `pharos.ingest.{dispatcher,fetcher,backoff,hostgate,repository}`, tasks `pharos.ingest.{dispatch,fetch,cleanup}` and their beat entries; about 90 tests (33 against PostgreSQL as the worker role: two dispatchers, duplicate delivery, lost lease, dead worker, no open transaction during the fetch, retention boundaries); 40 mutations caught; security review found a P1 (a database error while saving left the source untouched, so backoff was bypassed) and three P2s (duplicate task delivery fetched twice, a late start could outlive the lease, personal feeds could crowd out organization feeds), all fixed with tests (equivalent mutants: `SKIP LOCKED` alone, `ORDER BY` with the index, the breaker constant and two overlapping lease checks) |
| M3-08 | P1 | Review | Pipeline: normalize, exact dedup, hold rules, item sources | 07 | `pharos.ingest.pipeline` run inside the transaction that stores the raw entries (a lost lease discards both); canonical link, exact dedup by `(org, owner, url_hash)`, three hold rules, item links; 61 unit and 15 PostgreSQL tests (as the worker role) (second run adds 0; raw copy stored again after cleanup changes nothing, F-23; personal items never merged; a decision on an item survives; 200 entries stored quickly; five overlapping feeds leave 0 duplicates); 26 mutations caught by named tests (15 unit, 11 database); security review found 2 P1 (one long non-ASCII link made `store_failed` for the whole feed on every fetch; any source could rewrite another source's item unflagged), 2 P2 (an edit could add a flagged phrase to a clean item; IPv4 written as `2130706433`, `127.1`, `0x7f000001` or with a trailing dot was not flagged) and several P3, all P1/P2 fixed with tests; the rest in F-24. One existing test changed on purpose: `items_new` now counts new items, not changed raw rows |
| M3-09 | P1 | Todo | GitHub (releases) and Hacker News (Algolia) connectors | 06 | Token bound to `api.github.com`; rate limits honoured |
| M3-10 | P0 | Todo | Admin and editor API: sources, test fetch, status, items, hold/approve/hide; audit; route matrix | 08 | Cross-tenant tests; coarse errors only |
| M3-11 | P1 | Todo | Frontend: sources admin, items review, latest items list | 10 | Four languages; axe clean in 4 x 2 x 2 |
| M3-12 | P1 | Todo | Starter catalogue (disabled) and `pharos.cli import-sources`; the maintainer approves the list | 10 | Imported as the app role; every source disabled |
| M3-13 | P1 | Todo | Personal RSS: 5 per user, 60-minute minimum, owner-only | 10 | `http://127.1/` and `http://[::ffff:7f00:1]/` refused and audited; cross-user test |
| M3-14 | P0 | Todo | Real-stack E2E with a fake feed host, CI job, whole-milestone security review, documentation | all | All M3 acceptance criteria; no open P0/P1 |
