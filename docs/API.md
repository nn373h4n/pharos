# API

Status: v0 (milestone M1). Only the health endpoints exist. The OpenAPI schema is the source of truth for request and response shapes: `/api/openapi.json`, with interactive docs at `/api/docs`. Both are disabled when `PHAROS_ENVIRONMENT=production`, which is also the default when the variable is not set.

Run the server with `uvicorn --factory pharos.main:create_app`. There is no module-level `app`, so importing the module has no side effects.

## Conventions

- All routes live under `/api`; Caddy sends `/api` to the backend and everything else to the web app.
- Every response carries an `X-Request-ID` header. A client may send its own (1-64 characters of `A-Z a-z 0-9 . _ -`); anything else is replaced with a generated id. The same id appears in the logs of that request. Behind Caddy the header should be overwritten at the edge, because a client can otherwise choose the id.
- The organization is never taken from the request. It comes from the authenticated session (ADR-0003, ADR-0005).
- Errors use one format; it is defined together with the first authenticated endpoint (M2).

## Endpoints

| Method | Path | Auth | Purpose |
| --- | --- | --- | --- |
| GET | `/api/healthz` | none | Liveness: the process is up. Touches no dependency. |
| GET | `/api/readyz` | none | Readiness: database, application role, Redis, worker heartbeat and migration revision. |

### `GET /api/healthz`

`200 {"status": "ok"}`

### `GET /api/readyz`

`200` when every check passes, `503` otherwise. The body names the failed checks and nothing else; the reason is written to the log.

```json
{"status": "ok", "checks": {"database": "ok", "database_role": "ok", "redis": "ok", "worker": "ok", "migrations": "ok"}}
```

| Check | Passes when |
| --- | --- |
| `database` | `SELECT 1` succeeds as the application role |
| `database_role` | the application role is not a superuser, has no `BYPASSRLS` and owns no table in `public` |
| `redis` | `PING` succeeds |
| `worker` | the worker heartbeat in Redis is at most 210 seconds old (three missed beats plus slack) |
| `migrations` | the revision in `alembic_version` equals the head of `backend/alembic/versions` |

Each call opens database connections and pings Redis, and it is unauthenticated. Use it for the container healthcheck and the internal network; do not proxy it to the internet. A worker writes its first heartbeat as soon as it starts, but until one has, `worker` fails, so use `/healthz` for container restarts and `/readyz` for monitoring.
