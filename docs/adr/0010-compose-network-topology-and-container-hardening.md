# 0010. Compose network topology and container hardening

- Status: Proposed
- Date: 2026-10-26

## Context

`docs/ARCHITECTURE_PROPOSAL.md` section 9 lists three networks (edge, internal, egress) and says
the database and Redis never reach the internet. It places `web` and `api` on `edge`. A normal
Docker bridge network gives its containers outbound internet access, so with `web` and `api` on
`edge`, the web app could open connections to the internet. Principle 5 (default-deny egress) says
only the API, the worker and the scheduler may call out, and only for specific reasons: the
identity provider, content sources and LLM providers.

The images are also built once and run in many installations, usually by people who are not
security specialists, so the defaults must already be safe.

## Options considered

1. **Follow the proposal table literally** (`web` and `api` on `edge`, `internal` as a plain
   network) — pros: matches the document. Cons: `web` has internet access it does not need; an
   `internal` network without `internal: true` does not isolate anything.
2. **Only `caddy` on `edge`; everything else on an `internal: true` network; `egress` only for the
   services that must call out** — pros: Docker itself prevents `postgres`, `redis` and `web` from
   opening outbound connections; one rule is easy to verify. Cons: `caddy` and the backend reach
   each other over the internal network, so there is one more network to understand.
3. **A host firewall or egress proxy per destination only** — pros: real default-deny by
   destination. Cons: not portable, depends on the operator, cannot ship in the compose file.

## Decision

Option 2, with option 3 documented as the next step.

- `edge` carries the published ports and only `caddy`. Two networks are `internal: true`: `app`
  (`caddy`, `web`, `api`) and `data` (`api`, `worker`, `beat`, `migrate`, `postgres`, `redis`). The
  proxy and the web app, which handle requests from users, therefore cannot open a connection to
  the database or to Redis. `egress` is a normal network joined by `api`, `worker` and `beat` only.
- Every container runs with `cap_drop: [ALL]`, `no-new-privileges` and a read-only root file
  system; writable paths are tmpfs or named volumes. `api`, `worker`, `beat`, `web`, `postgres` and
  `redis` run as non-root users. `caddy` runs as root inside its container with every capability
  dropped except `NET_BIND_SERVICE`, because it must bind ports 80 and 443 and write its volumes.
- Compose cannot say which destinations an egress service may reach. Restricting `api`, `worker`
  and `beat` to the identity provider, content sources and LLM hosts is left to the host firewall
  or an outbound proxy, and `docs/DEPLOYMENT.md` says so.
- `postgres` and `redis` publish no ports. The roles `pharos_owner` and `pharos_app` are created
  by an init script on first start ([ADR-0005](0005-tenancy-and-rls.md)), which also forbids the
  database superuser to log in over TCP, so a compromised service on the `data` network cannot use
  it even with the password. Passwords reach `psql` through its environment, not its arguments.
- Containers also get a process limit (`pids_limit: 256`).
- `/api/readyz` is not served through the proxy, and the proxy replaces the client's
  `X-Request-ID`.
- The migration runs as a one-shot `migrate` service. `PHAROS_MIGRATE_ON_START=true` applies it
  before the API starts (first install); production sets it to `false` and runs the migration by
  hand after review.

## Consequences

- Positive: a reviewer can check the isolation with one script (`make test-compose`), including a
  positive control that the egress network works; the defaults are safe without extra steps.
- Negative: the egress services still reach any internet host until the operator adds a firewall
  rule, so default-deny egress by destination is a documented gap, not a feature. `caddy` is the
  one root process; running it as a non-root user needs its data volumes to be owned by that user
  and a sysctl for ports below 1024, and is left for later. Images are pinned by tag, not by
  digest. Containers on an `internal` network can still reach host services on the bridge gateway
  address; a host firewall rule is needed (documented, not tested).
- Follow-up: digest pinning of the base images (image scanning runs in CI since M1-09); an optional egress proxy profile; a
  non-root `caddy`; HSTS once there is a real domain; a check that passwords are URL-safe; backup
  and restore.
- Tests: `scripts/compose-smoke.sh` checks health, TLS, the hidden `readyz` under several path
  spellings, request-id replacement, network isolation with controls (including that the proxy and
  the web app cannot reach the database), database login rules, non-root users, read-only roots,
  dropped capabilities and the process limit. Deliberately breaking each of them makes it fail.
  It runs in its own project with its own image tag, so it never replaces the images of a real
  installation.

## References

- `docs/ARCHITECTURE_PROPOSAL.md` sections 3 (principles 4 and 5), 7, 9, 13 (M1-08)
- [0001 Modular monolith](0001-modular-monolith-platform.md),
  [0004 Background jobs](0004-background-jobs-celery.md),
  [0005 Tenancy and RLS](0005-tenancy-and-rls.md)
