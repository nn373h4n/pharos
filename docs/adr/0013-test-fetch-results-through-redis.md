# 0013. Test fetch: results from the worker reach the API through Redis

- Status: Proposed
- Date: 2026-10-30

## Context

An administrator who adds a source wants to know, before enabling it, whether it works: is the
address reachable, does it parse, what would the first articles look like. Only the worker may
contact the outside world (ADR-0012): the API has no route to the internet by design, a strict import
contract keeps the sources API from loading any HTTP client, and the worker is the process that is
allowed to handle hostile content.

So the check has to run in the worker and its answer has to travel back to the API and from there
to the administrator's browser. That is a new channel from the least trusted process to the most
trusted one, and the answer is built from what a third-party server said.

Forces:

- The worker's Redis user may touch only the Celery queue and `pharos:worker:*`; the API's user may
  read everything. The worker's database role may read sources but cannot write anything about them
  except through the ingestion paths.
- A task message must not let anyone who can write to the broker choose a URL (ADR-0012: tasks carry
  identifiers only).
- The answer comes from a process that parses hostile input. If that process is compromised it can
  write any value under its own keys.
- Redis is small (200 MB, no eviction) and is shared with sessions and the queue.
- A test fetch is an outbound request caused by a person: it must be limited, politely spaced and
  audited like the scheduled ones.

## Options considered

1. Run the fetch in the API through `SafeHttpClient` — simple, but it gives the API a route to the
   internet and loads the HTTP client into the process that holds sessions and the root key. It
   undoes the main decision of ADR-0012.
2. Celery result backend — the framework's own answer, but it needs a second kind of Redis access
   for both users, stores results under keys neither ACL is shaped for, and returns whatever the
   worker pickled or serialized to a reader that does not validate it.
3. The worker writes the answer into the database — it has no write grant on anything an
   administrator reads, and widening that grant for a transient answer is the wrong trade.
4. The worker writes a small validated answer under its own Redis prefix with a short expiry, and
   the API treats it as untrusted data when it reads it — chosen.

## Decision

Option 4.

- **Request.** `POST /api/sources/{id}/test-fetch` (administrators only) checks the source and its
  stored address again, takes a per-source cooldown (30 seconds) that also counts against a limit of ten trials a minute for the whole organization, records a marker
  `pharos:api:testfetch:<test_id>` (organization, source, person; expiry 5 minutes) in the API's own
  namespace, audits `source.test_fetch_requested`, and sends the Celery task
  `pharos.ingest.test_fetch` with two identifiers: the source and the test id. The message carries no
  address, no configuration and an expiry of one minute.
- **Work.** The task re-reads the source from the database as the worker role, only for an
  organization source (`owner_user_id IS NULL`), passes the host politeness gate, fetches once through
  the same connector and `SafeHttpClient` as a scheduled fetch, **writes nothing to the database**,
  and stores a result under `pharos:worker:testfetch:<test_id>` with a two-minute expiry. The result
  holds the outcome, a fixed error word, the HTTP status, counts, and at most five samples (a cleaned
  title, an `http(s)` link, a date). No excerpt, no body, no header.
- **Answer.** `GET /api/sources/{id}/test-fetch/{test_id}` (administrators only) finds the marker
  (another organization's, another source's or an expired id is a `404`), reads the worker's key and
  **validates it as untrusted input**: a size limit before parsing, a strict schema (exact types, no
  unknown fields), a fixed list of outcomes and a short lowercase word for the error code, at most five
  samples, the identifiers must equal the marker's, a title must already be what text cleaning leaves
  (the reader refuses text, it does not repair it) and a link must pass the link check again. Anything else is reported as `unavailable`,
  never passed on. Until a result exists the answer is `pending`.
- **Boundaries.** The API never writes `pharos:worker:*` and the worker cannot read or write
  `pharos:api:*` (its ACL does not cover them), so the worker cannot fake a marker, lift a cooldown or
  read another test's request. The sources API still imports no HTTP client, fetcher, connector or
  worker module; it sends the task by name through a small queue client in `pharos.core` that publishes through the
  anonymous exchange to the one list the worker listens to, so the broker's table of bindings (which the
  worker's Redis user may write) is never read, and the API's all-keys Redis user is never steered to
  another key. The send runs in a thread pool of its own under a deadline.
- **Failure.** If the queue or Redis is down the request answers `503 service_unavailable` and the
  marker is removed. The cooldown stays: the task may have reached the broker even though the answer
  did not reach the API, and a retry at once could fetch the source twice.

## Consequences

Positive:

- The API stays without a route to the internet; the check uses the very code path that a scheduled
  fetch uses, so a green test means the real fetch will behave the same way.
- A compromised worker can only put a bounded, schema-checked, plain-text answer in front of an
  administrator, for a test that administrator started. It gains no way to read sessions, rate limits
  or markers.
- Nothing is stored in PostgreSQL, so no migration and no retention question.

Negative:

- A second, small protocol between two processes that must be kept in step (`pharos.ingest.testfetch`
  holds the shared constants and the schema, with no network in it).
- The answer is asynchronous: the browser polls. Two minutes after the fetch the result is gone.
- Titles in a sample are text from a third party shown to an administrator; the front end must show
  them as text, never as markup (ADR-0009).
- Any administrator of the organization who has the test id can read its answer; the person who
  started it is recorded in the marker and in the audit log, not checked on reading.
- A compromised worker could fill the shared Redis (200 MB, no eviction) through keys under its own
  prefix; this is older than the test fetch and is left to the Redis hardening follow-up.
- A test fetch that cannot get the host gate (another fetch from that host a moment ago) reports
  `host_busy` instead of waiting.

Follow-up:

- Tests: shared schema fuzzing, the API against a forged or malformed answer, the worker's task with a
  scripted network and the worker database role (nothing written), the Redis ACL for both users, the
  cooldown, the audit entry and the roles (done in the same change).
- Real-stack check with a fake feed host inside the test network: M3-14.
- The front end of the sources screen (M3-11) polls the answer and shows it as text.

## References

- ADR-0006 (connectors, `SafeHttpClient`), ADR-0009 (output sanitization), ADR-0012 (worker identity
  and ingestion boundaries).
- `docs/plans/m3-sources-and-ingestion.md`, task M3-10 (test fetch as a worker task).
- `deploy/redis/start.sh` (the two Redis users).
