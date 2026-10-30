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
| `own_account` | 409 | An administrator tried to change their own role or status. Ask another administrator. |
| `status_locked` | 409 | The user was removed by the identity provider (`deprovisioned`); the status is not changed here. |
| `source_conflict` | 409 | The same source (same type and configuration, in any spelling of the address) exists already. |
| `source_limit` | 409 | The organization has 200 sources, the most it may have. |
| `item_state` | 409 | The article is not in a state that allows this step (for example approving a hidden article). |
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

## People (administrators)

Administrators only; everyone else gets `403`, and a person of another organization answers `404 not_found`. Requests that change data also need `Origin` and `X-CSRF-Token`.

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/api/users` | The organization's people, by name. `limit` (1-100, default 50) and `offset`. Answers `{items, total}`. |
| PATCH | `/api/users/{id}/role` | Body `{"role": "admin" \| "editor" \| "member"}`. |
| PATCH | `/api/users/{id}/status` | Body `{"status": "active" \| "disabled"}`. `deprovisioned` is set only by the identity provider and is refused here. |

Each person has `id`, `display_name`, `email`, `role`, `status`, `department`, `last_login_at` and `sign_in` (`local` or `sso`). No credential data is ever included.

Rules: nobody changes their own role or status (`409 own_account`); the caller is checked again against the database after the administrator rows are locked, so two administrators changing each other at the same moment cannot leave the organization without one (the second gets `403`). A change ends every session of the person it concerns; a new role or status would apply on their next request anyway. A request that changes nothing answers `200` and writes nothing. Changes are recorded as `user.role_changed` and `user.status_changed` with the old and new value, plus `auth.sessions_revoked`; a refused change leaves a `failure` row with the reason. The role of a `deprovisioned` person cannot be changed either (`409 status_locked`).

## Sources (administrators and editors)

Administrators and editors read; administrators alone create, change and delete (a source makes the server contact a third party). Everyone else gets `403`, and a source of another organization answers `404 not_found`. A person's own feeds (M3-13) are never listed or changed here, not even for administrators. Requests that change data also need `Origin` and `X-CSRF-Token`.

| Method | Path | Who | Purpose |
| --- | --- | --- | --- |
| GET | `/api/sources` | admin, editor | The organization's sources, by name. `limit` (1-100, default 50), `offset`, `type` (`rss`, `github`, `hackernews`) and `enabled`. Answers `{items, total}`. |
| POST | `/api/sources` | admin | Create. Body `{"type", "name", "config", "interval_seconds"?, "enabled"?}`; answers `201` with the source. It starts disabled unless `enabled` is `true`. |
| GET | `/api/sources/{id}` | admin, editor | One source. |
| PATCH | `/api/sources/{id}` | admin | Change `name`, `config`, `interval_seconds` or `enabled` (at least one, never `null`). The type never changes. |
| DELETE | `/api/sources/{id}` | admin | Delete the source with its runs, raw entries and links (`204`). Its items stay; retention removes them in time. |
| GET | `/api/sources/{id}/runs` | admin, editor | The latest fetch runs, newest first. `limit` (1-50, default 20). |
| POST | `/api/sources/{id}/test-fetch` | admin | One trial fetch by the worker, to see whether a source works before enabling it. Answers `202 {test_id, retry_after_seconds}`. |
| GET | `/api/sources/{id}/test-fetch/{test_id}` | admin | The answer of that trial. |

`config` depends on the type: `{"url"}` for `rss`, `{"repo": "owner/name"}` for `github`, `{"query", "min_points"?}` for `hackernews` (see [DATA_MODEL.md](DATA_MODEL.md)). `interval_seconds` is 300 to 604800 (default 3600). `name` is plain text of 1 to 200 characters; text that changes when cleaned (markup, control or invisible characters) is refused, not repaired.

A source shows `enabled`, `interval_seconds`, `next_fetch_at`, `last_fetched_at`, `last_success_at`, `consecutive_failures`, `last_error_code` and who created it. It never shows the lease, the validators or the key used to recognise duplicates.

Validation: a bad configuration answers `422 validation_failed` with `{loc: ["body", "config"], type: CODE}` where CODE is one of `url_required`, `url_invalid`, `repo_required`, `repo_invalid`, `query_required`, `query_invalid`, `min_points_invalid`, `unknown_setting` or `unknown_type`. An address that can only be an internal machine answers `url_not_allowed`: an IP address in any spelling a browser accepts (`127.1`, `2130706433`, `[::1]`, or followed by an ideographic full stop), `localhost`, a name without a dot (`redis`, `api`), a name ending in `.local`, `.internal`, `.lan` and similar, and the address of this installation. The refusal is audited, and the address is checked again when a source is switched on. This check only saves the administrator a wasted fetch; `SafeHttpClient` still decides when a connection is opened, after the name has been resolved.

**Trial fetch** (ADR-0013). The API has no route to the internet: it asks the worker, which fetches once through the same connector and `SafeHttpClient` as a scheduled fetch and writes **nothing to the database** (no run, no entry, no item, no validator, no failure count). The request is allowed for a source that is not enabled yet, checks the stored address again (`422 url_not_allowed`, audited), and may be repeated for one source every 30 seconds, and at most ten times a minute for the whole organization (`429 rate_limited` with `Retry-After` otherwise; another source has its own cooldown). If the queue or Redis does not answer, `503 service_unavailable`: the marker is removed, but the cooldown stays, because the task may have reached the broker, so wait it out before trying again. Poll the answer with the `test_id`: `status` is `pending` until the worker has answered, then `done` with `outcome` (`ok`, or a fixed word such as `bad_status`, `timeout`, `blocked`, `bad_content`, `host_busy`, `source_missing`), `error_code`, `http_status`, `entries_seen`, `entries_skipped`, `bytes_fetched` and at most five `sample` articles (`title`, `url`, `published_at`; no excerpt; an article whose text is not plain is left out of the sample). The answer is kept for two minutes and the test id for five; after that, or for an id that was not started for this source in this organization, the answer is `404`. What the worker leaves in Redis is treated as untrusted: if it is not exactly what the protocol allows (size, fields, exact types, outcomes, plain text, links, ids), the answer is `unavailable` and nothing of it is shown. Titles are text from a third party and must be shown as text.

Rules: the same source twice answers `409 source_conflict`; the 201st answers `409 source_limit`; two administrators changing one source queue up on its row lock and both changes are kept; a change that changes nothing is neither saved nor audited. Changing a source never touches what the worker keeps about it (failures, validators, lease). Audit events: `source.created`, `source.updated` (with the names of the changed fields), `source.enabled`, `source.disabled`, `source.deleted`, `source.test_fetch_requested`; entries never contain the configuration.

## Personal feeds (everyone, their own only)

Every signed-in person, whatever their role, can follow up to five RSS or Atom feeds of their own (M3-13). Only they see them and their articles: another person, an administrator, another organization and the shared article list all get `404 not_found`, the same answer as for an id that does not exist. If the operator switched the feature off (`PHAROS_PERSONAL_FEEDS=off`), every endpoint below answers `404`. Requests that change data also need `Origin` and `X-CSRF-Token`.

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/api/me/feeds` | The person's feeds, oldest first. `limit` (1-50, default 20), `offset`. Answers `{items, total}`. |
| POST | `/api/me/feeds` | Add one: `name` (plain text, up to 200 characters), `url`, `interval_seconds` (3600 to 604800, default 3600), `enabled` (default `true`). Unknown fields, a `type` and a `config` are refused. |
| GET | `/api/me/feeds/{id}` | One feed: `id`, `name`, `url`, `enabled`, `interval_seconds`, `next_fetch_at`, `last_fetched_at`, `last_success_at`, `consecutive_failures`, `last_error_code`, `created_at`, `updated_at`. |
| PATCH | `/api/me/feeds/{id}` | Change `name`, `url`, `interval_seconds` or `enabled`. |
| DELETE | `/api/me/feeds/{id}` | Delete the feed with its fetch history. Its articles stay in the table, reachable by nobody, until retention removes them. |
| GET | `/api/me/feeds/{id}/runs` | The latest fetches (`limit` 1-50, default 20), newest first. |
| GET | `/api/me/feeds/{id}/items` | The articles of this feed, newest first (`limit` 1-100, default 20, `offset`), with `status` and `hold_reason`. Nobody reviews a personal article: a flag only tells the owner that a rule matched. |

The address is checked with the rules of the sources (`pharos.ingest.sourceconfig`): `http` or `https` only, no credentials, nothing inside the network (`localhost`, private and link-local addresses, odd spellings such as `127.1`, `2130706433`, `0x7f.1`, `[::ffff:7f00:1]`, and the address of this installation). A refused address answers `422 validation_failed` with `errors: [{"loc": ["body", "url"], "type": "url_not_allowed"}]` and leaves an audit entry (`source.created` or `source.updated`, outcome `failure`, reason `url_not_allowed`, scope `personal`; the address itself is not recorded). Ten new addresses an hour per person (a feed added, or the address of one changed) are allowed; more answers `429 rate_limited` with `Retry-After`. The sixth feed answers `409 source_limit`; the same feed twice (whatever its spelling) answers `409 source_conflict`. A feed is fetched at most once an hour. There is no trial fetch for a personal feed.

## Items (articles)

Every signed-in person reads; administrators and editors also review. An article is a title, a short plain-text excerpt and a link to the original; the page behind the link was never fetched. An article of another organization answers `404 not_found`. A person's own articles (M3-13) are never part of this list or of the review. Requests that change data also need `Origin` and `X-CSRF-Token`.

| Method | Path | Who | Purpose |
| --- | --- | --- | --- |
| GET | `/api/items` | everyone | Articles, newest first (publication time, else the time they were fetched). `status` (`new`, `held`, `hidden`), `source_id`, `limit` (1-100, default 50), `offset`. Answers `{items, total}`. |
| GET | `/api/items/{id}` | everyone | One article. |
| POST | `/api/items/{id}/hold` | admin, editor | `new` becomes `held` (reason `editor_hold`). |
| POST | `/api/items/{id}/approve` | admin, editor | `held` becomes `new`; the flag and its reason are cleared. |
| POST | `/api/items/{id}/hide` | admin, editor | `new` or `held` becomes `hidden`: out of the list for everyone but reviewers. |
| POST | `/api/items/{id}/restore` | admin, editor | `hidden` becomes `new`. |

A member sees only `new` articles: asking for another status, or for one `source_id`, answers `403`, and a held or hidden article answers `404` exactly like an id that does not exist. `sources` (which organization sources reported the article), `hold_reason` and `updated_at` (a change in it would say that an article was reviewed) are for administrators and editors; a member gets an empty `sources` and `null` for the other two. A held article is one that a rule flagged during ingestion (`link_ip_host`, `link_odd_port`, `instruction_phrase`) or that an editor held; the flag is a prompt to look, not a verdict.

Each step is one move of a small state machine. Asking for the state an article is in already (hiding a hidden one, approving a `new` one) changes nothing and is not recorded; a step that does not exist (restoring a held article, holding a hidden one) answers `409 item_state`. Two editors acting on one article queue up on its row lock, and the second decides on what the first left behind. The worker cannot undo a decision: it has no right to change `status` or `hold_reason`, and a hidden article is not created again by a later fetch of the same link (a feed that publishes it under another address makes a new article: near-duplicates are for M4). Restoring a hidden article is an explicit decision to show it, even if a rule had flagged it before it was hidden; the audit entry of the hide keeps the reason that was cleared (`was`). Audit events: `item.held`, `item.approved`, `item.hidden`, `item.restored`, each with the state it came from (`from`) and the reason that was cleared (`was`); entries never contain article text.

## Single sign-on (OpenID Connect)

Off unless `PHAROS_OIDC_ISSUER` and `PHAROS_OIDC_CLIENT_ID` are set; then `GET /api/auth/config` returns `"oidc": {"provider_name": "...", "login_url": "/api/auth/oidc/login"}` (otherwise `null`) and the routes below exist (without a provider they answer `404`). Register `{PHAROS_PUBLIC_URL}/api/auth/oidc/callback` as the redirect URI at the provider.

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/api/auth/oidc/login?next=/page` | Start: `303` to the provider's authorization endpoint (authorization code flow, PKCE S256, `state`, `nonce`, `response_mode=query`). Sets the one-time cookie `__Host-pharos_oidc` (10 minutes) that ties the flow to this browser. |
| GET | `/api/auth/oidc/callback` | Finish: `303` to `next` (a path on this site; anything else becomes `/`) with the session cookie, or to `/login?error=CODE`. |

Both answer with redirects and `Cache-Control: no-store`, because a browser navigates to them. Error codes in `/login?error=`: `sso_failed` (anything wrong with the response: state, code, token), `sso_denied` (the person or the provider said no), `sso_unavailable` (provider or Redis not reachable), `account_disabled` (the account is disabled; it is not re-activated), `rate_limited`. The precise reason is only in the audit log.

How a person is identified: by `(organization, issuer, subject)` from the validated ID token, never by e-mail (e-mail is not unique and two accounts with the same address stay separate). The first sign-in creates the user as a plain `member`; roles are set by administrators. Name and e-mail are refreshed at each sign-in (a claim that is missing does not erase what is stored). The department claim (`PHAROS_OIDC_DEPARTMENT_CLAIM`, default `department`) creates or selects the department, unless the person chose their own.

**Microsoft Entra ID** (`PHAROS_OIDC_PRESET=entra`, see [DEPLOYMENT.md](DEPLOYMENT.md)): the issuer is pinned to one directory (`https://login.microsoftonline.com/<tenant>/v2.0`). The ID token must also carry that directory in `tid`, be version `2.0` and have an object id; the person is `(issuer, oid)`, not `sub`, which differs per application. The department is read once per sign-in from Microsoft Graph (`GET /v1.0/me?$select=department`, scope `User.Read`) on every sign-in (this preset does not use a department claim) and cleaned like any claim; a Graph failure or an odd value never stops the sign-in. The access token is not stored. There is no group-to-role mapping: roles are set by administrators. Failure reasons in the audit log add `token:bad_tenant`, `token:bad_version` and `token:guest_not_allowed`.

What is checked on the ID token: signature (RS256, PS256 or ES256 only, key chosen by `kid` from the provider's key set, one refetch for an unknown key), `iss` equal to the configured issuer, `aud` containing the client id, `azp`, `exp`, `iat` (60 seconds leeway), `nonce` and `sub`. The optional RFC 9207 `iss` response parameter must match too. A callback works once, only in the browser that started the flow, and only for ten minutes.

`POST /api/auth/logout` answers `200 {"end_session_url": "..."}` instead of `204` when the session was created by single sign-on: the web app sends the person to the provider's own logout (`client_id` and `post_logout_redirect_uri={PHAROS_PUBLIC_URL}/login`) so the provider session ends too. The URL is `null` if the provider has no end-session endpoint.
