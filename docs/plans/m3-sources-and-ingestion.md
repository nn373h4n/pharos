# M3 plan — sources and ingestion

## Context

M1 (skeleton) and M2 (sign-in, organization, onboarding) are merged. People can sign in, but there is nothing to show them. M3 builds the supply side: Pharos fetches content from outside sources on a schedule, cleans and de-duplicates it, and stores it so that M4 (LLM enrichment, translation, personal feed, "Try this") has material to work with.

M3 is the first milestone that processes **untrusted content from the internet**, so most of the effort goes into containing it: a safe outbound HTTP client, a worker that can hurt very little if it is taken over, hostile-feed handling, and tests that attack all of it. There is **no LLM, no embeddings and no ranking** in M3.

The docs define M3 in one line only ("Sources and ingestion: RSS, GitHub, HN, Reddit · SafeHttpClient · dedup", `docs/ARCHITECTURE_PROPOSAL.md:338`) plus the MVP criterion "connectors work; an admin adds/disables a source; a user's personal RSS passes the SSRF check". This plan fills the gap. It was checked by an independent design review (Plan agent); its findings are marked **[R]**.

Inputs: `docs/adr/0006` (connectors, SafeHttpClient), `0009` (sanitization), `0005` (tenancy), `docs/THREAT_MODEL.md`, follow-ups F-14 (worker can forge sessions; **must close in M3**), F-03 (CSP), F-04 (egress by host).

## Decisions already taken by the maintainer (this session)

1. **Personal RSS is in M3**, small version: 5 feeds per user, interval at least 60 minutes, visible only to its owner.
2. **No intranet or private-address feeds** in M3, for anyone. (An operator CIDR allowlist would need its own ADR later.)
3. **Existing demo installs: recreate the volume** (no upgrade script) for the new database role and Redis ACL. Documented, with a clear start-up error for an old volume.

## Decisions I made (listed so the maintainer can overrule them)

| Topic | Decision | Why |
| --- | --- | --- |
| Content plane | M3 tables are **organization-scoped** with ordinary tenant RLS (one org per installation). No `public` rows, no ingestion "public writer" role. The starter catalogue is imported into the org, **disabled** until an admin enables each source | Removes the public-writer trap; the maintainer approves every source (Q12) |
| Worker identity **[R]** | New PostgreSQL role `pharos_worker` with column-level grants on the M3 tables only (cannot touch `users`, `local_credentials`, sessions, audit); Redis **ACL user** for the worker limited to Celery keys and `pharos:worker:*`. An HMAC on session records alone does not close F-14 (the worker could still reset rate limits or edit OIDC state) | Real isolation for the process that handles hostile input |
| Task arguments **[R]** | Celery tasks carry IDs only; the task re-reads URL and config from the database | Anyone who can write to the broker must not be able to choose a URL |
| Worker DB access **[R]** | One event loop and one asyncpg engine per worker process (created in `worker_process_init`), `run_until_complete` per task; **no DB transaction is held during HTTP** | No new dependency (psycopg); asyncpg connections belong to a loop |
| Scheduling **[R]** | One beat entry runs a dispatcher every minute. It selects due sources (`FOR UPDATE SKIP LOCKED`), takes a **lease** (`lease_until`, `lease_token`) and enqueues at most N per tick. `task_time_limit` and `task_soft_time_limit` are set. Beat only needs Redis, so it leaves the `egress` network | Late acks + 1 h visibility timeout means a task can run twice; the lease makes that harmless; a flood of enqueues would fill the 200 MB Redis and make sign-in fail closed |
| Parsing **[R]** | `feedparser` on **bytes only** (never a string: it would fetch or read a file), after a DOCTYPE/entity check with `defusedxml`; caps: 2 MiB raw, gzip refused or hard-capped, 200 entries, title 300 and excerpt 500 characters | Billion-laughs, XXE, feed bombs |
| Stored text **[R]** | `nh3` to plain text, NFKC, strip control, bidi and zero-width characters; links only `http(s)` (checked on store and on render), shown with their host and `rel="noopener noreferrer nofollow"`. Only title, excerpt, canonical URL, date are stored; **article pages are never fetched** | ADR-0006/0009; stored XSS; cost |
| Dedup | Exact: canonical URL (strip `utm_*`, `fbclid`, `gclid`, fragment; lowercase host; drop default port; **keep other query parameters**, Hacker News needs `?id=`). `UNIQUE (org_id, owner_user_id, url_hash) NULLS NOT DISTINCT`, plus `item_sources` so one article seen in several sources is one item. **Near-duplicate by title shingles is deferred to M4** (embeddings; it would merge "v1.2.3" and "v1.2.4") **[R]** | Correctness first |
| Personal items **[R]** | `owner_user_id` set: never merged into or created as an org-visible item (that would leak what someone follows). Visibility filter in the repository **and** a cross-user test | Privacy |
| Secrets | GitHub token only as `PHAROS_GITHUB_TOKEN` in the worker environment (fine-grained, no scopes), bound to `api.github.com`, masked in logs. Nothing encrypted in the database (the worker has no root key) | Consistent with the worker split |
| Reddit | Not a connector. Possible later as an ordinary RSS source after the legal check | Open legal question |
| Hacker News | One Algolia query per run, not Firebase plus hundreds of item fetches | Cost, politeness |
| Politeness **[R]** | Conditional GET (ETag / Last-Modified), honour 429 and `Retry-After`, per-host minimum gap and concurrency (Redis), identifying User-Agent | Hostile or fragile hosts, being a good citizen |
| Retention | `raw_items` kept 14 days (truncated fields only); `fetch_runs` 30 days; `items` 180 days unless held for review | Proposed; Q10 stays open for M5 |
| Test-only host allowance **[R]** | Every Docker address is private, so the fake feed host in tests needs an allowance. It is read only when `PHAROS_ENVIRONMENT=test`, `Settings` refuses it otherwise, it is never a constructor flag, and it has its own mutation test | Otherwise tests cannot reach a fake feed, or production gets a backdoor |

## SafeHttpClient (the centre of M3) **[R]**

`backend/src/pharos/net/safe_http.py`, own import-linter exception (only it and the identity-provider client import `httpx`); connectors may import only this client; API modules may **not** import it (`test-fetch` runs in the worker, because the API is also on `egress`).

- httpx with a custom httpcore network backend: resolve DNS and validate **every** returned address **at connect time**, connect to the validated IP, keep Host and SNI. One resolution per connection (no rebinding window). Reject the hostname if any address is bad.
- Allow only `ip.is_global` and not multicast (needs Python 3.12.4+); also deny explicitly: 0/8, 100.64/10, 192.0.0/24, 198.18/15, 240/4, NAT64 (64:ff9b::/96, 64:ff9b:1::/48), 6to4 and Teredo (check the embedded IPv4), fec0::/10, IPv4-mapped IPv6; the host of `PHAROS_PUBLIC_URL`. Reject userinfo, non-http(s) schemes, ports other than 80/443.
- Redirects handled by hand: at most 3, every hop re-validated, `Authorization` stripped on a cross-origin hop. `trust_env=False`, no HTTP/2, identity encoding.
- Limits: 2 MiB on the raw stream, **total wall-clock deadline** (not only read timeouts: slowloris), connect timeout.
- Errors are coarse categories only (`blocked`, `timeout`, `too_large`, `bad_status`, `bad_content`), so "blocked" and NXDOMAIN do not reveal internal names.

## Data model (migration 0003, tenant RLS on every table)

`sources` (type `rss|github|hackernews`, config validated per type, `enabled`, `interval_seconds`, `owner_user_id` null for admin sources, `next_fetch_at`, lease, `consecutive_failures`, `last_error_code`), `fetch_runs` (bounded history for the admin), `raw_items` (truncated fields, unique `(source_id, external_id)`), `items` (canonical URL, `url_hash`, title, excerpt, `lang`, `published_at`, `status new|held|hidden`, `hold_reason`, provenance `content_sha256`, `fetched_at`), `item_sources`. Grants: the worker role gets only what the pipeline needs; the app role gets reads and the admin/editor writes. Privilege tests: as `pharos_worker`, `UPDATE users` and `SELECT local_credentials` fail.

## Tasks (one pull request each)

| ID | Task | Depends on | Notes |
| --- | --- | --- | --- |
| M3-01 | ADRs: accept 0006 and 0009 (maintainer), new ADR-0012 for worker identity (DB role, Redis ACL, ID-only tasks) and ingestion; approve dependencies `feedparser`, `nh3`, `defusedxml` | — | `Proposed` until the maintainer accepts |
| M3-02 | **Close F-14**: Redis ACL users (API full, worker Celery + `pharos:worker:*`, `-@dangerous`); worker credentials in Compose; integration test that the worker user gets NOPERM on `pharos:session:*`, `pharos:rl:*`, `pharos:oidc:*` | 01 | Mutation: remove an ACL rule |
| M3-03 | `pharos_worker` DB role (init script, grants, start-up check), the per-process loop/engine pattern, task time limits, beat off `egress` | 01 | Old volumes get a clear error |
| M3-04 | **SafeHttpClient + SSRF suite** | 01 | No DB dependency; parallel with 02/03. At least 40 parametrized cases (ranges, IPv6 forms, decimal/octal/short IPv4, redirects at hops 1-3 and a 4th, auth strip, rebinding with an injected `Resolver`, 2 MiB+1, 10 MB gzip bomb, 1 byte/s drip killed in 30 s) |
| M3-05 | Migration 0003 + RLS + grants + privilege and cross-tenant tests | 03 | Uniqueness, NULLS NOT DISTINCT, personal-item isolation |
| M3-06 | Connector protocol + RSS/Atom connector + parser hardening | 04, 05 | bytes-only, DOCTYPE refusal, caps, conditional GET; fake feed server |
| M3-07 | Dispatcher, lease, retry/backoff, circuit breaker, cleanup, politeness | 06 | `Clock` fake; duplicate-run test; two dispatchers |
| M3-08 | Pipeline: normalize, exact dedup, hold rules (flags only, as metadata for M4), `item_sources` | 07 | 10,000-entry feed stores 200 items, < 300 MB, < 20 s |
| M3-09 | GitHub (releases) and Hacker News (Algolia) connectors | 06 | Token binding, rate-limit headers |
| M3-10 | Admin/editor API: sources CRUD, enable/disable, `test-fetch` as a worker task, fetch status, items list, hold/approve/hide; audit events; route matrix + cross-tenant tests | 08 | Coarse errors only |
| M3-11 | Frontend: sources admin, items review, plain "latest items" list for members; four languages; axe | 10 | Editor review UI can shrink to the API plus a minimal page |
| M3-12 | Starter catalogue (5-8 well-known sources, **disabled**) and `pharos.cli import-sources`; maintainer approves the list before it ships | 10 | Run as the app role, not as owner |
| M3-13 | Personal RSS: 5 per user, 60-minute minimum, owner-only visibility, SSRF-checked at add time and at every fetch | 10 | Cuttable if the milestone runs long |
| M3-14 | Real-stack E2E (a fake feed host inside the test network; worker fetches through SafeHttpClient; item appears; second run adds nothing; killing the worker leaves no duplicates; attempts against `redis:6379`, `postgres:5432`, `api:8000`, `169.254.169.254` and the public URL are blocked), CI job, whole-milestone security review, docs (API, DATA_MODEL, THREAT_MODEL, ARCHITECTURE, DEPLOYMENT, guides), PROGRESS, session note | all | |

Order of work: 01, then 02, 03 and 04 in parallel, then 05 to 10 in sequence (09 beside 08), 11 to 13, 14. Each pull request keeps the M1/M2 discipline: tests first-class, `make lint`, `make test`, mutation checks for every new control, a `security-reviewer` pass before committing the risky ones (02, 03, 04, 06, 10, 13), required CI checks green.

## Acceptance criteria (measurable)

1. `make lint`, `make test`, `make test-integration`, `make test-compose`, `make test-stack-e2e` green locally and in CI.
2. Every mutation in the list below makes at least one named test fail (recorded in the pull request): remove the IP check; remove the per-hop re-check; remove the size cap; remove the deadline; remove the auth strip on redirect; remove `nh3`; remove the link-scheme check; remove the lease; remove the bytes-only parse; widen the worker grant; remove the test-allowance environment guard; remove a Redis ACL rule.
3. SSRF suite of at least 40 cases passes (list in M3-04). Real stack: fetches towards `redis:6379`, `postgres:5432`, `api:8000`, `169.254.169.254` and the public URL open 0 connections and end as `blocked` runs.
4. As `pharos_worker`, `UPDATE users` and `SELECT local_credentials` fail; with the worker's Redis user, `GET pharos:session:x` fails with NOPERM. F-14 is closed.
5. A user who adds `http://127.1/` or `http://[::ffff:7f00:1]/` is refused and the refusal is audited.
6. Two dispatchers running at once start at most one fetch per source per interval; a worker killed mid-task leaves no duplicate items; the second run of the same feed adds 0 items.
7. A feed of 10,000 entries stores 200 items, peak memory stays under 300 MB, the run takes under 20 s.
8. Duplicate rate on a recorded corpus of overlapping feeds is under 5%.
9. All new UI text in `en`, `az`, `tr`, `ru`; axe clean on the new pages (4 locales x 2 themes x 2 sizes).
10. An independent whole-milestone security review has no open P0/P1.

## What M3 will explicitly NOT prove

Egress control by destination host (F-04); strict CSP (F-03, the new pages render only plain text, but the policy itself stays open); resistance to malicious connector plugins (only built-in connectors exist); any LLM or prompt-injection resistance (M4); proxy-based egress; the terms of service of any real source; feed quality, relevance or the Azerbaijani quality; behaviour with several organizations; load beyond the 10,000-entry feed.

## Risks

SSRF has many corner cases (mitigated by the address allowlist, connect-time validation and the large suite); a hostile or broken feed can waste resources (caps, deadline, backoff, circuit breaker, per-host limits); the 200 MB Redis is shared by broker and sessions (enqueue caps per tick); the milestone is large (14 pull requests), so M3-13 and the review UI are the first things to cut; real feeds will behave differently from the fake ones (the catalogue starts small and disabled).

## Verification at the end of M3

`make lint`, `make test`, `make test-integration`, `make test-compose`, `make test-sso-dev`, `make test-stack-e2e` all PASS; the mutation list above is recorded; manual run: `make env`, `make dev`, create the organization and an administrator, import the catalogue, enable one source, watch an item appear, try to add an internal address as a personal feed and see it refused, kill the worker and see no duplicates; the whole-milestone review has no open P0/P1; `TASKS.md`, `PROGRESS.md` and the session note say what was not tested.
