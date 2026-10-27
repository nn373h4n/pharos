# API

Status: v0 (milestone M1). Only the health endpoints exist. The OpenAPI schema is the source of truth for request and response shapes: `/api/openapi.json`, with interactive docs at `/api/docs`. Both are disabled when `PHAROS_ENVIRONMENT=production`, which is also the default when the variable is not set.

Run the server with `uvicorn --factory pharos.main:create_app`. There is no module-level `app`, so importing the module has no side effects.

## Conventions

- All routes live under `/api`; Caddy sends `/api` to the backend and everything else to the web app.
- Every response carries an `X-Request-ID` header. A client may send its own (1-64 characters of `A-Z a-z 0-9 . _ -`); anything else is replaced with a generated id. The same id appears in the logs of that request. Behind Caddy the header should be overwritten at the edge, because a client can otherwise choose the id.
- The organization is never taken from the request. It comes from the authenticated session (ADR-0003, ADR-0005).
- Errors use one format, described in [Errors](#errors).
- Objects that belong to another organization answer `404 not_found`, never `403`: their existence is not revealed.

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
{"status": "ok", "checks": {"database": "ok", "database_role": "ok", "redis": "ok", "worker": "ok", "migrations": "ok", "organization": "ok"}}
```

| Check | Passes when |
| --- | --- |
| `database` | `SELECT 1` succeeds as the application role |
| `database_role` | the application role is not a superuser, has no `BYPASSRLS` and owns no table in `public` |
| `redis` | `PING` succeeds |
| `worker` | the worker heartbeat in Redis is at most 210 seconds old (three missed beats plus slack) |
| `migrations` | the revision in `alembic_version` equals the head of `backend/alembic/versions` |
| `organization` | an organization with the slug `PHAROS_ORG_SLUG` exists (looked up through the `pharos_org_id_for_slug` function) |

Each call opens database connections and pings Redis, and it is unauthenticated. Use it for the container healthcheck and the internal network; do not proxy it to the internet. A worker writes its first heartbeat as soon as it starts, but until one has, `worker` fails, so use `/healthz` for container restarts and `/readyz` for monitoring.

## Errors

Every error is an [RFC 9457](https://www.rfc-editor.org/rfc/rfc9457) document with the media type `application/problem+json`:

```json
{
  "type": "urn:pharos:problem:forbidden",
  "title": "Forbidden",
  "status": 403,
  "code": "forbidden",
  "request_id": "9f2c1a6e5b7d4c0e8a3b2d1f6e4c7a90"
}
```

`code` is the stable machine-readable identifier; the web app turns it into a translated message, so the API sends no user-facing text. `request_id` equals the `X-Request-ID` header and the id in the logs. Details never repeat request data (a rejected password must not come back in the response) or internal exception text.

| Code | Status | Meaning |
| --- | --- | --- |
| `not_authenticated` | 401 | No valid session, or the user is disabled or removed. The answer is the same for every cause. |
| `forbidden` | 403 | Signed in, but the role does not allow this. |
| `not_found` | 404 | No such route or object, or the object belongs to another organization. |
| `method_not_allowed` | 405 | The path exists but not for this method; `Allow` lists the methods. |
| `csrf_failed` | 403 | A state-changing request had no `Origin` equal to the public address, or a signed-in request had no valid `X-CSRF-Token`. |
| `onboarding_incomplete` | 409 | Onboarding was completed before all four answers existed; `missing` lists them. |
| `rate_limited` | 429 | Too many attempts. `Retry-After` gives the seconds to wait; waiting is enough, there is no lockout. |
| `validation_failed` | 422 | The body, query or path is not valid. `errors` lists `{loc, type}` for each problem, never the value. |
| `organization_not_configured` | 503 | The organization of this installation has not been created yet. |
| `service_unavailable` | 503 | A required dependency is down (for example Redis for sessions). Sign-in fails closed. |
| `internal_error` | 500 | Unexpected failure. The cause is in the logs under the `request_id`. |
| `http_error` | other | Any other HTTP error raised by the framework. |

More codes (sign-in failures) arrive with the endpoints that raise them and are added to this table in the same pull request.

## Authentication and authorization (building blocks)

Not endpoints yet; they are the parts every endpoint uses (ADR-0011):

- `current_org` resolves the organization from `PHAROS_ORG_SLUG` and never from the request.
- `UnitOfWorkDep` gives a transaction scoped to that organization. The service calls `commit()` before returning, so a failed commit becomes a 5xx and never a 200; anything not committed is rolled back.
- `CurrentPrincipal` returns the signed-in user. The role and status are read from the `users` row on every request, so a demotion or a disabled account applies on the next request. `require_role(...)` adds the role check.
- A route-coverage test fails when a route is neither on the public list nor in the role matrix with a cross-tenant test.

## Sessions and CSRF

- The session cookie is `__Host-pharos_session` (Secure, HttpOnly, SameSite=Lax, Path=/, no Domain, no expiry date). When `PHAROS_PUBLIC_URL` is plain http (local development only) the browser would reject that prefix, so the name is `pharos_session` and the cookie is not Secure.
- The cookie holds an opaque 256-bit id. The server keeps who it belongs to and a CSRF seed in Redis (database 1, own key prefix); role and status are read from the database on every request. The idle timeout is 8 hours and slides, the absolute limit is 24 hours (`PHAROS_SESSION_IDLE_SECONDS`, `PHAROS_SESSION_ABSOLUTE_SECONDS`). If Redis does not answer, requests fail with `503 service_unavailable`.
- Every `POST`, `PUT`, `PATCH` and `DELETE` must send an `Origin` header equal to `PHAROS_PUBLIC_URL`; a missing header or `null` is refused with `403 csrf_failed`. This also protects sign-in, which has no session yet.
- When the request carries a live session it must also send `X-CSRF-Token`, an HMAC bound to that session id and seed. A new token is issued whenever the session id is rotated; tokens of the old id stop working. The token reaches the browser in the response of the endpoint that returns the current user (M2-04).
- Forwarded headers (`X-Forwarded-For` and friends) are honoured only from Caddy's pinned address; see `docs/DEPLOYMENT.md`.

## Sign-in endpoints (local accounts)

| Method | Path | Auth | Purpose |
| --- | --- | --- | --- |
| GET | `/api/auth/config` | none | What the sign-in page needs: `{"local_login": "off" \| "break-glass" \| "on"}`. |
| POST | `/api/auth/local/login` | none | Sign in with a username and password. |
| POST | `/api/auth/logout` | cookie | End the caller's own session. Idempotent. `204`, the cookie is cleared. |
| GET | `/api/me` | session | The signed-in user and the CSRF token. |

`POST /api/auth/local/login` takes `{"username": "...", "password": "..."}` and answers `200 {"user": {...}, "csrf_token": "..."}` with the session cookie. Every failure (wrong password, unknown user, disabled account, not allowed in `break-glass` mode, password too long) is the same `401 invalid_credentials`; the reason is only in the audit log. After a few failures the answer becomes `429 rate_limited` with `Retry-After`, even for the right password; waiting is enough. With `PHAROS_LOCAL_LOGIN=off` the endpoint answers `404`. Sending the request while already signed in needs the `X-CSRF-Token` of that session, like any other state-changing request; the old session is ended and a new id is issued.

`GET /api/me` answers `{"user": {"id", "display_name", "email", "role", "locale", "department", "last_login_at"}, "csrf_token": "..."}`. The role is read from the database on every request, so a change applies at once; a disabled account gets `401`. The web app sends `csrf_token` back as `X-CSRF-Token` on every `POST`, `PUT`, `PATCH` and `DELETE`.

## Profile and onboarding

All of these need a session. Requests that change data also need `Origin` and `X-CSRF-Token` (see Sessions and CSRF).

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/api/me/profile` | The profile of the signed-in user. Empty fields are `null` or `[]`. |
| PUT | `/api/me/profile` | Set profile fields. Fields that are present are set (`null` clears), absent fields stay. |
| POST | `/api/me/onboarding/complete` | Mark onboarding finished. Idempotent. |
| GET | `/api/me/profile/options` | The fixed choices: `ai_levels`, `interests` (codes), `locales`. |
| GET | `/api/professions` | Public professions and the organization's own, with names in `en`, `az`, `tr`, `ru`. |
| GET | `/api/departments` | The organization's departments. |

`PUT /api/me/profile` accepts `profession_id`, `ai_level` (`beginner`, `intermediate`, `advanced`), `interests` (codes from the catalogue, no duplicates, at most 12), `department_id` and `locale` (`en`, `az`, `tr`, `ru`). Anything else, including `org_id`, `role` and the onboarding timestamps, is refused with `422`. Unknown choices answer `422 validation_failed` with the field in `errors`; a profession or department of another organization counts as unknown. A department chosen here is marked as the user's own, so a later sign-in through the identity provider does not overwrite it.

The profile response contains `locale` (the saved choice) and `effective_locale`: the saved choice, else the best supported language of `Accept-Language`, else `PHAROS_DEFAULT_LOCALE`. `onboarding_started_at` is set by the first save and never changes; `onboarding_completed_at` is set once by completing.

`POST /api/me/onboarding/complete` needs all four answers: a profession, at least one interest, a language and an AI level. Otherwise it answers `409 onboarding_incomplete` with `missing` listing the fields. `GET /api/me` also returns `onboarding_completed_at`, so the app can decide whether to show onboarding.
