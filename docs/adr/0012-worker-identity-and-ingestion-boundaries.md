# 0012. Worker identity and ingestion boundaries

- Status: Proposed
- Date: 2026-10-28

## Context

Milestone M3 makes the worker fetch content from the internet on a schedule. That content, and the
servers it comes from, are untrusted: a hostile feed can try to exhaust resources, exploit a parser,
smuggle markup, or steer the fetcher to internal addresses. Today the worker is as powerful as the
API where it matters most:

- It logs in to PostgreSQL as `pharos_app`, the role of the API, which may change `users.role` and
  `users.status`. A taken-over worker could make anybody an administrator.
- It uses the same Redis password as the API, so it can write session records (it can become any
  user), reset rate limits and edit the single sign-on state (follow-up F-14, which was due to close
  in M3).
- Celery tasks may carry arbitrary arguments; whoever can write to the broker could choose a URL.
- Outbound HTTP rules ([ADR-0006](0006-connector-plugins-safehttpclient.md)) exist on paper only:
  there is no `SafeHttpClient`.

## Options considered

1. Keep one database role and one Redis user — simplest, and the worker can do anything the API can.
   Rejected: the process that handles hostile input must be the least powerful one.
2. Sign the session records in Redis with a key the worker lacks — closes session forgery, but the
   worker could still reset rate limits, edit the OIDC state and delete sessions. Not enough alone.
3. A separate PostgreSQL role and a separate Redis ACL user for the worker, tasks that carry only
   identifiers — real isolation with little machinery. Chosen.
4. Run fetchers in their own container image with no access to the database, passing results through
   a queue — the strongest isolation, a lot more moving parts. Kept as a later step if connectors from
   third parties are ever allowed.

## Decision

- **Database.** A role `pharos_worker` (no login privileges beyond what is listed) is created with the
  other roles in `deploy/postgres/init/01-roles.sh`. Its grants are column-level and limited to the
  ingestion tables (`sources`, `fetch_runs`, `raw_items`, `items`, `item_sources`) plus
  `EXECUTE` on `pharos_org_id_for_slug`. It cannot read or write `users`, `local_credentials`,
  `user_profiles`, `audit_logs` or anything else. Row-level security still applies to it.
- **Redis.** Two ACL users replace the single password: the API user may use everything it uses today;
  the worker user may use only the Celery/kombu keys and channels and keys under `pharos:worker:`,
  without the `@dangerous` commands. The default user is switched off. A test proves the worker user
  gets `NOPERM` on `pharos:session:*`, `pharos:rl:*` and `pharos:oidc:*`.
- **Tasks.** Celery tasks carry identifiers only. The URL and the configuration are read from the
  database inside the task. A fetch task takes a lease on its source before it starts, so a
  redelivered or duplicated task is harmless.
- **Process.** The worker keeps one event loop and one asyncpg engine per child process, created after
  the fork; no database transaction is held while waiting for HTTP.
- **Outbound HTTP.** All fetching goes through `SafeHttpClient` (design in the M3 plan): addresses are
  validated at connect time and the connection goes to the validated address, redirects are followed
  by hand with every hop re-checked, sizes and the total time are capped. The API process does not
  import it; the "test fetch" of a source runs as a worker task.
- **Content.** Only title, excerpt, canonical link and date are stored; article pages are never
  fetched. Text is reduced to plain text with `nh3` (ADR-0009), links are `http(s)` only.
- **Content plane.** M3 tables belong to the organization (ordinary tenant row-level security). There
  are no `public` rows and no ingestion role that writes across organizations.

Existing demo installations must recreate their volumes: the role and the ACL are created at first
start, and the services refuse to run against a database or Redis without them, with a message that
says so. There is no upgrade script before 1.0.

## Consequences

- Positive: a taken-over worker can no longer promote users, read credentials, forge sessions or
  tamper with sign-in state; a hostile broker message cannot choose a URL; the SSRF defence is
  concentrated in one client with its own test suite.
- Negative: two more secrets to generate (`make env` does it), a second database connection setup
  for the worker, and old volumes must be recreated.
- Follow-up: F-14 is closed by this ADR's implementation (M3-02); F-04 (egress by host) and F-03
  (strict CSP) stay open and are documented as not proven by M3. The worker's database role and Redis
  user are listed in `docs/THREAT_MODEL.md` and `docs/DEPLOYMENT.md`.
- New dependencies: `feedparser`, `nh3`, `defusedxml` (approved with the M3 plan).

## References

- [0005 Tenancy and RLS](0005-tenancy-and-rls.md),
  [0006 Connector plugins and SafeHttpClient](0006-connector-plugins-safehttpclient.md),
  [0009 Output sanitization](0009-output-sanitization-nh3-csp.md),
  [0011 Sessions and CSRF](0011-sessions-csrf-idp-trust-and-organization-resolution.md)
- `docs/plans/m3-sources-and-ingestion.md`, follow-ups F-03, F-04, F-14 in `TASKS.md`
