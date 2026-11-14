# 0029. The worker gets its own Redis, so it cannot fill the API's

- Status: Proposed
- Date: 2026-11-14

## Context

One Redis serves the whole installation (`deploy/redis/start.sh`). Two users share it.

- The API's user owns the session records, the rate-limit counters, the sign-in state, the shared
  ranking of the Home feed and the trial-fetch markers.
- The worker's user may touch only what Celery needs (the queue and kombu's bookkeeping) and its
  own keys under `pharos:worker:*`. It cannot read or write the API's keys, list names or run an
  administrative command (F-14, ADR-0012).

Memory is shared. Redis runs with `--maxmemory 200mb --maxmemory-policy noeviction`, so sessions
and counters are never dropped to make room. When the limit is reached, Redis refuses writes
instead, and sign-in then fails closed.

The worker handles hostile content, so it has to be assumed that it can be taken over. Redis has
no memory limit per user and no limit on the number of keys an ACL may create. A compromised
worker can therefore write keys with no expiry under its own prefix, or into the queue, until the
200 MB are used up. Every write of the API then fails:

- nobody can sign in;
- rate limits cannot count;
- the feed cannot be stored.

That is a denial of service against the trusted side by the untrusted side. It is older than M3-10c
(F-27). Nothing is read or changed that the ACLs protect; only the shared capacity is taken.

## Options considered

1. **Two Redis instances.** `redis` keeps the API's data. `broker` holds the Celery queue and the
   worker's keys.
   - The API also connects to `broker`, as its own user there: it enqueues trial fetches, reads
     their answers and sees the worker's heartbeat.
   - The worker and the scheduler connect only to `broker`.
   - A worker that fills `broker` can stop ingestion, trials and its own heartbeat. It cannot touch
     sessions, rate limits or the feed.
   - Cons: one more container (about 30 MB of memory at rest, a `mem_limit` of 256m) and a second
     URL in the settings. Backups need nothing new, because everything in Redis is either
     short-lived or rebuilt.
2. **A watchdog.** The API or the scheduler reads `INFO memory`, warns and deletes the worker's keys
   when usage is high. This detects the attack but does not prevent it. It also needs a user that
   may run `INFO` and delete the worker's keys, which is the API's user, the one this ADR is
   meant to protect. A race against a fast writer is lost.
3. **Expiry on every worker key, enforced.** Redis cannot enforce it. A compromised worker simply
   does not set one.
4. **Keep it and document it.** This is the state today. Sign-in depends on the worker behaving.

## Decision

Proposed: option 1.

- **Compose.** A `broker` service gets the same image and digest as `redis` and its own
  `start-broker.sh`:
  - `--maxmemory 100mb --maxmemory-policy noeviction`;
  - users `pharos_broker_api` and `pharos_worker` with the patterns that exist today;
  - the default user off.

  The `redis` service drops the `pharos_worker` user and the Celery patterns. The worker and the
  scheduler are no longer on the network of `redis`.
- **Settings.** `PHAROS_BROKER_URL` is new. It is required in production and gets a password and
  the same character rules as the Redis URLs (F-09). `PHAROS_REDIS_URL` stays the API's.
  `.env.example` and `make env` make a third password.
- **Code.** The Celery app, `CeleryQueue`, the trial-fetch answer store, the host gate, the
  heartbeat and the LLM budget keys of the worker use `broker_url`. The API's sessions, rate
  limits, sign-in state, feed cache and trial markers stay on `redis_url`.
  - Trial markers stay on `redis` on purpose: the worker must still be unable to forge or read
    them.
  - The readiness check covers both instances.
- **Upgrade.** Nothing in Redis has to be carried over. Queued tasks at the moment of the upgrade
  are lost, and the next dispatcher run leases the sources again, as after any restart of Redis.
  The release notes say to add the new password before `up`.
- **Tests.** `scripts/test-infra.sh` starts both instances. Two tests are added:
  - the worker's user on `broker` cannot reach `redis` at all;
  - filling `broker` up to its limit leaves sign-in working. This is a smoke step: it fills
    `broker` as the worker's user and signs in.

## Consequences

- The untrusted side can only exhaust its own Redis, which is a change of the security boundary.
  THREAT_MODEL and ADR-0012 get an amendment, and DEPLOYMENT gets the new service.
- One more container and one more secret for the operator. The status script and the backup docs
  mention `broker`.
- A full `broker` still stops ingestion and trials until it is emptied. That is visible through the
  heartbeat and readiness. Fixing it means restarting `broker`, whose contents are disposable.
- It needs the maintainer's approval as a deployment change. It is not needed for the pilot if the
  pilot accepts the risk (one trusted installation, the worker's ACLs unchanged). It should be
  decided before the first public release.

## References

- [0012 Worker identity and ingestion boundaries](0012-worker-identity-and-ingestion-boundaries.md),
  [0013 The trial fetch](0013-test-fetch-results-through-redis.md)
- `deploy/redis/start.sh`, F-14 and F-27 in `TASKS.md`
