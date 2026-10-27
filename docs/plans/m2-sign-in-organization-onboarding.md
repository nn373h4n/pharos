# M2 plan — sign-in, organization, onboarding

## Context

M1 is finished: the platform runs but nobody can sign in. M2 adds the identity layer that every later milestone (sources, feed, LLM) stands on: who the user is, which organization they belong to, what they may do, and their profile (profession, interests, language, AI level). It is the first code that handles credentials and sessions, so most of the effort goes into security and tests, not into screens.

Sources: `docs/ARCHITECTURE_PROPOSAL.md` sections 5, 7, 8, 12, 14; ADR-0003 (OIDC with the backend as BFF), ADR-0005 (one organization per installation, RLS), `docs/THREAT_MODEL.md`. An independent design review (Plan agent) found 15 flaws or gaps in the first draft; they are fixed below and marked **[R]**.

## Acceptance criteria for M2 (measurable)

1. Sign-in works end to end through Keycloak (Playwright through Caddy over HTTPS), and through the Entra ID preset against recorded/mocked Entra shapes. **Real Entra: NOT TESTABLE** until M0-02 exists; the criterion is "partial" until then.
2. A user and their department are created on first OIDC sign-in; the local admin's actions appear in the audit log (test reads it).
3. RBAC `admin` / `editor` / `member` is enforced in the service layer. A test enumerates **every registered route** and fails if one is neither on an explicit public allow-list nor covered by the role × endpoint matrix and a cross-tenant test. **[R]**
4. Onboarding (profession → interests → language → AI level) completes keyboard-only; a timed Playwright run finishes in under 2 minutes; `onboarding_started_at` and `completed_at` are stored so the pilot can measure it.
5. All new UI text exists in `en`, `az`, `tr`, `ru`; axe reports no violations on authenticated pages in 4 locales × 2 themes × 2 viewports.
6. Security: the OIDC negative suite (below) passes; an independent `security-reviewer` pass has no open P0/P1; the mutation checks for the new controls are caught.

## Design decisions (after the review)

| Topic | Decision | Why |
| --- | --- | --- |
| Session content | Redis record with only `user_id`, `org_id`, `auth_time`, `csrf_seed`; **role and status are read from `users` on every request** and the session org must equal the configured org. A per-user session index lets disabling a user, changing a role or "log out everywhere" delete all their sessions **[R]** | A role in the session stays valid after a demotion for up to 24 h |
| Cookie | `__Host-pharos_session`, Secure, HttpOnly, SameSite=Lax, Path=/; idle 8 h (sliding, Redis only), absolute 24 h, rotated on login and on any privilege change | Prefix needs HTTPS; E2E therefore goes through Caddy |
| CSRF | Token = HMAC(key, session id); server layout passes it to client components; every POST/PUT/PATCH/DELETE needs `X-CSRF-Token` **and** an `Origin` equal to `PHAROS_PUBLIC_URL` (never compared to `Host`); missing or `null` Origin is rejected; login and logout are covered too **[R]** | Proxy rewrites `Host`; login CSRF has no session yet |
| OIDC login CSRF | `state` bound to the browser by a short-lived `__Host-pharos_oidc` cookie; Redis key includes it; consumed with `GETDEL`; `response_mode=query`; PKCE S256; `next` and the verifier stay server-side **[R]** | Otherwise an attacker can sign a victim in as the attacker |
| Org before login | `SECURITY DEFINER` function `pharos_org_id_for_slug(text)` with `search_path = pg_catalog, pg_temp`, `REVOKE ... FROM PUBLIC`, `GRANT` to `pharos_app`; resolved lazily and cached; `/readyz` gains an `organization` check **[R]** | A view owned by the table owner bypasses RLS and becomes an enumeration oracle; `create-org` runs after the stack is up |
| Unit of work | One explicit transaction per request that the service commits **before** the response; dependencies use `scope="function"` (FastAPI 0.162 default `request` scope ends after the response is sent — verified in the installed source) ; DB first, then Redis; audit rows for failures use their own transaction **[R]** | A failed commit must never return 200 |
| Identity key | `(org_id, issuer, subject)` only. Never look up or link by email, verified or not; email is not unique; JIT is `INSERT … ON CONFLICT … RETURNING`; JIT never reactivates a `disabled` user; `department_source` (idp/user) so a sign-in does not overwrite the user's choice **[R]** | Account takeover through email, races |
| IdP HTTP | Small client that talks only to the endpoints in the validated discovery document (`issuer` must equal the configured value) plus explicit extras (Graph); https outside development; timeouts, size limit, no cross-host redirects; JWKS refetch on unknown `kid` (rate limited); algorithms ⊂ {RS256, PS256, ES256}; leeway ≤ 60 s; RFC 9207 `iss` checked when present. Recorded as an exception to "outbound HTTP only through SafeHttpClient" in ADR-0011 plus an import-linter contract (`httpx` only there) **[R]** | A private Keycloak must work; Google uses three hosts |
| Entra | Tenant-pinned issuer (`common`/`organizations` refused); identity `(tid, oid)`; Graph `/me?$select=department,jobTitle` for prefill; **no group → role mapping in M2** **[R]** | Group overflow and precedence need their own design |
| Local accounts | Operator CLI only (no self-registration); `PHAROS_LOCAL_LOGIN` set to `off`, `break-glass` or `on` (default `break-glass`: admins only); password 15–128 characters; argon2id in a thread behind a semaphore (2), dummy hash for unknown/SSO-only users, `needs_rehash`; rate limit **before** hashing; progressive delay per (account, IP) plus a soft per-account cap, no hard lockout, CLI `unlock`; counters only in Redis **[R]** | Lockout as denial of service; memory (64 MiB per hash vs 512 MB limit) |
| Proxy trust | Pin the `app` network subnet and Caddy's address; uvicorn `--proxy-headers --forwarded-allow-ips=<caddy ip>` (never `*`: `web` can reach `api:8000`); closes F-13 **[R]** | Per-IP limits and audit need the real client address |
| CLI | `create-org` runs with the owner role by reusing the `migrate` service environment (`docker compose run --rm migrate python -m pharos.cli …`), password via `getpass`, never argv; `create-admin` runs as `pharos_app` inside a tenant session so RLS applies. No new `tools` service **[R]** | App role may not create organizations, but it can create users |
| Secrets | `PHAROS_SECRET_KEY` (≥ 32 bytes) → HKDF subkeys (audit ip hash, CSRF, later AES); production validators extended: https `PHAROS_PUBLIC_URL`, real OIDC secret **[R]** | One key, separate purposes |
| Redis | Sessions and counters get a key prefix and their own database, `noeviction`; Redis down = sign-in and session checks fail closed (503/401), never open **[R]** | Shared with Celery, 256 MB |
| Errors | RFC 9457 `application/problem+json` with a stable `code` catalogue in `docs/API.md`; text only in the frontend. Objects of another organization return 404, never 403 | One error format (backend rule) |
| Time | One injectable `Clock` for sessions, lockout and token checks | Frozen time in tests |
| Frontend data | Server components fetch `http://api:8000` with `cache: "no-store"` and the forwarded cookie; no Server Actions for mutations; `proxy.ts` is never treated as authentication **[R]** | Next's data cache could serve one user's `/api/me` to another |

## Data model (Alembic 0002, RLS on every table)

`departments` (unique `(org_id, normalized_name)`), `professions` (columns `scope`/`owner_org_id` so `content_policy_sql` is reused; global rows seeded with 4-language names), `users` (`status` active/disabled/deprovisioned, `role`, `issuer`, `subject`, `locale`, `department_id`, `department_source`; app role may not UPDATE `org_id`, `issuer`, `subject`), `local_credentials` (hash only; counters live in Redis), `user_profiles` (`profession_id`, `ai_level`, `interests` as fixed codes, `onboarding_started_at`, `completed_at`), `audit_logs` (append-only: INSERT and SELECT only; `actor_type` user/local_admin/operator_cli; `ip_hash` = HMAC). **Composite foreign keys** `(org_id, x_id) → x(org_id, id)` so a row cannot point into another organization; a trigger checks `profession_id` scope. Seed content is generic (about ten professions, twelve interest codes); no pilot data.

## Endpoints (documented in `docs/API.md` with the error codes)

`GET /api/auth/config`, `POST /api/auth/local/login`, `POST /api/auth/logout`, `GET /api/auth/oidc/login`, `GET /api/auth/oidc/callback`, `GET /api/me`, `GET|PUT /api/me/profile`, `POST /api/me/onboarding/complete`, `GET /api/professions`, `GET /api/departments`, `GET /api/users`, `PATCH /api/users/{id}/role`, `PATCH /api/users/{id}/status`. Admin-only audit read: `GET /api/admin/audit`. The role endpoint refuses removing the last admin and self-demotion.

## Tasks (one pull request each; order and parallelism)

| ID | Task | Depends on | Notes |
| --- | --- | --- | --- |
| M2-01 | ADR-0011 (sessions, CSRF, IdP trust boundary, org resolution, unit of work); approval of dependencies | — | `Proposed`; gates 03 and 05 |
| M2-02 | Migration 0002: schema, definer function, grants, composite FKs, seeds | 01 | RLS test per table, cross-org FK test, append-only audit, column grants, downgrade round trip |
| M2-10a | **Keycloak spike** (parallel with 02): dev profile behind Caddy on a dev host name, network alias on `caddy`, Caddy's internal CA trusted by `api` | 01 | Riskiest integration; decides the issuer setup early |
| M2-03a | Errors, `Clock`, `Principal`, unit of work, `current_user`, settings and validators, lazy org resolution, **route-coverage gate** | 02 | Gate protects every later PR |
| M2-03b | Session store, CSRF, Origin check, cookies, pinned proxy trust | 03a | Includes the compose smoke check that a forged `X-Forwarded-For` is ignored |
| M2-03c | Rate limiter, audit service and event catalogue | 03b | Concurrency tests |
| M2-04 | Local accounts: login, logout, `/me`, CLI (`create-org`, `create-admin`, `unlock`, `set-password`) | 03c | Shortest path to unblock the frontend |
| M2-08 | Profile and onboarding API, professions, interest codes, locale precedence | 03a | Parallel with 05 |
| M2-05 | Generic OIDC: client, flow, JIT, RP-initiated logout | 03c | Fake-IdP suite |
| M2-06 | Entra preset | 05 | Mock/recorded shapes only |
| M2-07 | RBAC dependency, role/status endpoints, `GET /users`, matrix rows | 04 | Session revocation tests |
| M2-09a | Frontend: login page, app shell, user menu, 401 handling | 04 | Can start earlier against the OpenAPI schema |
| M2-09b | Frontend: onboarding stepper | 08, 09a | Keyboard-only |
| M2-10b | Compose wiring: new env, `make env` additions, IdP setup guides (Keycloak, Entra, generic), `DEPLOYMENT.md` | 10a, 05 | |
| M2-11 | Stack E2E + CI job, security review, docs (API, DATA_MODEL, THREAT_MODEL, ARCHITECTURE), TASKS/PROGRESS/CHANGELOG/session | all | Execution order: 01, 02, 10a, 03a-c, 04, 08, 05, 06, 07, 09a, 09b, 10b, 11 |

Each PR keeps the M1 discipline: tests first-class, `make lint` and `make test`, mutation checks for each new control, `security-reviewer` before committing the auth-critical ones (03b, 04, 05), and the required CI checks.

## Dependencies and images to approve (this approval covers them)

Python runtime: `joserfc` (JOSE; brings `cryptography`), `argon2-cffi`, `httpx` (moves from the dev group to runtime). Image: `quay.io/keycloak/keycloak` (dev profile only, pinned tag). Frontend: none. Not added: Authlib (a small explicit OIDC flow is easier to audit), `cryptography` directly, any Redis client library beyond the existing one.

## Test strategy

- **Fake IdP** (in-test ASGI app: discovery, JWKS, token endpoint, keys generated per test) covers the negatives deterministically: state mismatch, forged nonce, replayed code, expired token, wrong audience, wrong issuer, missing PKCE verifier, `alg=none` and HS256 confusion, JWKS rotation, callback from a second browser context, RFC 9207 mismatch, `common` tenant.
- **Keycloak** only for the Playwright stack test (interoperability, cookies, redirects): login → onboarding (timed, keyboard-only) → app shell → logout, through Caddy over HTTPS (`ignoreHTTPSErrors`), plus authenticated axe in 4 locales × 2 themes × 2 viewports.
- Real PostgreSQL and Redis integration tests for: RLS per table, parallel first sign-ins, parallel failed logins, revocation (disable user → next request 401; role change → next request reflects it), failing commit not returning 200, Redis down failing closed, forged `X-Forwarded-For`, Origin cases, open-redirect corpus for `next`, log masking of cookies, `code`, `state`, ID tokens.
- Cross-tenant tests need a second organization: forged session for org B against an app configured for org A, plus a second app instance. Designed in M2-03a.
- **Not proven by M2:** real Entra, Google Workspace, Okta; behaviour behind a real public domain with ACME; load; password-hash timing beyond a coarse bound (the dummy-hash path is asserted instead of wall-clock equality).

## Moved out of M2

Admin panel and DB-stored encrypted provider configuration, group → role mapping, TOTP for the local admin, back-channel logout, self-registration and password reset by e-mail, LDAP/SAML, user import. Native-speaker review of the translations (F-01) stays a follow-up; the new strings are written by the assistant again and flagged.

## Risks

Keycloak setup in CI is heavy (about 1 GB; the dev profile only); OIDC provider differences (claims, issuers) appear only with real providers; the route-coverage gate may need exceptions for framework routes; the review rounds make M2 the largest milestone so far (13 pull requests); Redis outage means no sign-in by design.

## Verification at the end of M2

`make lint`, `make test`, `make test-integration`, `make test-e2e`, `make test-compose` and the new stack E2E all PASS locally and as required CI checks; mutation script for the auth controls catches every deliberate regression; manual run: `make env`, `make dev` with the dev profile, create the organization and an admin with the CLI, sign in locally, sign in through Keycloak, finish onboarding by keyboard, sign out; independent security review has no open P0/P1; `TASKS.md`, `PROGRESS.md` and the session note state what was not tested.
