# 0014. The worker reads the state of an account, and nothing else about people

- Status: Proposed
- Date: 2026-10-30

## Context

ADR-0012 gives the worker role grants on the ingestion tables only: it "cannot read or write `users`".
That keeps the process that handles hostile content away from people and sign-in data.

Personal feeds (M3-13) add one need. A personal feed belongs to one person, and when the person is
disabled or deprovisioned the feed must stop being fetched. Administrators cannot do it for them:
row-level security hides a person's feeds from everybody else, administrators included. Only the
worker decides what is fetched, so only the worker can skip the feeds of a person who is no longer
active. The security review of M3-13 found that without this the feeds of a deprovisioned person are
fetched for ever (F-32).

## Options considered

1. Switch the feeds off when an administrator disables a person. — Misses every other path that
   changes the state (the identity provider deprovisions people on sign-in), and puts one module's
   cleanup into another's.
2. A separate table the API fills with "feeds to skip". — One more thing to keep in step with
   `users.status`; a bug leaves feeds fetched.
3. Let the worker read the state of the account, by column. — One extra fact, no personal data.
4. Let the operator switch all personal feeds off (`PHAROS_PERSONAL_FEEDS=off`). — Exists, but is
   all or nothing.

## Decision

Option 3. The migration `0004` grants `SELECT (org_id, id, status)` on `users` to `pharos_worker`
and nothing else. The lease query takes a personal feed only if its owner is `active`
(`EXISTS`, so only `sources` rows are locked), and the fetch task checks again before it fetches, so
a lease taken before the account was disabled is dropped.

This replaces the sentence in ADR-0012 that the worker cannot read `users` for these three columns
only. The e-mail, name, role, department, credentials, profiles and the audit log stay unreadable;
row-level security still limits the worker to the organization it entered. Tests pin the exact
columns and prove that reading any other column is refused.

## Consequences

- A disabled or deprovisioned person's feeds are no longer fetched; making the person active again
  resumes them.
- The worker can list the identifiers and states of every account of its organization (not only
  of feed owners), so it can tell who is `deprovisioned`. Together with an identifier that is
  pseudonymous personal data. The worker holds no name, e-mail or role to join it with, and the
  process is the one we already trust with the organization's feeds, but the exposure is real.
  A `SECURITY DEFINER` function answering for one identifier would remove it; it was left out to
  keep the grant simple and visible to the exact-set tests. Revisit if the worker's reach grows.
- A fetch that has started is not stopped when the owner is disabled meanwhile (the lease and
  `start_run` look just before the network call): at most one more fetch, whose result only the
  owner can see. The same holds for the `enabled` flag.
- The worker's privileges grow by one column-level grant; the exact-set tests now pin it. Any other
  grant on `users` fails the build.
- ADR-0012 gets a pointer to this decision.

## References

- ADR-0012, M3-13, F-32, `backend/alembic/versions/0004_worker_reads_owner_status.py`
