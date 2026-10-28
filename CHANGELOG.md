# Changelog

All notable changes are listed here, newest first. The format follows [Keep a Changelog](https://keepachangelog.com/) and the project uses [Semantic Versioning](https://semver.org/). Nothing has been released yet.

## [Unreleased]

### Fixed

- Caddy listens on the same port numbers the host publishes, and the HTTP-to-HTTPS redirect now names the configured domain and HTTPS port. The automatic redirect reflected the request's Host header, an open redirect; it is disabled and `make test-compose` checks it.

### Added

- Platform skeleton (milestone M1):
  - Backend: FastAPI application with request ids, JSON logs that mask secrets, `/api/healthz` and `/api/readyz`, and an Alembic baseline that creates `organizations` under PostgreSQL row-level security.
  - Worker: Celery worker and scheduler with a heartbeat that `/api/readyz` checks.
  - Web app: Next.js pages in English, Azerbaijani, Turkish and Russian, light and dark themes, WCAG 2.2 AA checks in the test suite.
  - Deployment: Docker Compose stack with Caddy, isolated networks, hardened containers, PostgreSQL with pgvector and Redis (`make env`, `make dev`).
  - Delivery: pull-request checks, security scans (gitleaks, CodeQL, Trivy), weekly Dependabot updates, a protected `main` branch.
- Development identity provider: `deploy/docker-compose.sso-dev.yml` adds Keycloak (fictional users) behind Caddy on `auth.localhost`; the API trusts only Caddy's public root certificate. `make test-sso-dev` checks the setup.
- Request plumbing for the identity work (milestone M2): one error format (`application/problem+json` with stable codes), the injectable clock, the principal and organization scoping of every request, one transaction per request that the service commits before the response, `PHAROS_PUBLIC_URL`, `PHAROS_ORG_SLUG` and `PHAROS_SECRET_KEY` (with derived keys), an `organization` readiness check and a test that fails when a route is neither public nor covered by the role matrix.
- Sessions and CSRF protection (milestone M2): server-side sessions in Redis (`__Host-pharos_session` cookie, idle and absolute limits, per-user revocation, rotation), an Origin check and a per-session CSRF token on every state-changing request, and forwarded headers trusted from Caddy's pinned address only.
- Rate limiting and the audit log service (milestone M2): atomic per-address and per-account sign-in throttling with progressive delay and no lockout, and an append-only audit service with a fixed event catalogue, a separate transaction for failed attempts and keyed hashes instead of client addresses.
- Local accounts (milestone M2): password sign-in with Argon2id (`PHAROS_LOCAL_LOGIN`: `break-glass` by default, `on`, `off`), sign-out, `GET /api/me`, and operator commands `create-org`, `create-admin`, `set-password` and `unlock` (`python -m pharos.cli`). Failures all look alike, are throttled with growing waits and never lock an account out.
- Profile and onboarding API (milestone M2): profession, interests, language, AI level and department choices from fixed catalogues, completion with start and end timestamps, language precedence (own choice, browser, installation default) and the professions and departments lists.
- Single sign-on (milestone M2): authorization code flow with PKCE for any OpenID Connect provider, users found or created by issuer and subject (never by e-mail), department from a configurable claim, sign-out that also ends the provider session, and a Keycloak-based check of the whole flow (`make test-sso-dev`). New dependencies: `joserfc` and `httpx` (now a runtime dependency, used only by the identity-provider client).
- Onboarding (milestone M2): a four-step questionnaire in four languages (profession and optional department, interests, language, experience with AI) that saves each answer, can be completed with the keyboard alone in well under two minutes, continues where the person stopped and sends people who have not finished it there before anything else. The new texts in Azerbaijani, Turkish and Russian are unreviewed by native speakers (F-01).
- Sign-in pages and app shell (milestone M2): a login page in four languages with single sign-on and password forms (shown according to `GET /api/auth/config`), errors for wrong credentials, too many attempts and failed single sign-on, an account menu with sign-out, and a gate that sends signed-out visitors to sign in and back to the page they asked for. The new texts in Azerbaijani, Turkish and Russian are unreviewed by native speakers (F-01).
- Microsoft Entra ID preset (milestone M2): `PHAROS_OIDC_PRESET=entra` with `PHAROS_ENTRA_TENANT_ID` pins sign-in to one directory (the `common`, `organizations` and `consumers` aliases and domain names are refused), identifies people by object id, requires a version 2.0 token from that directory, refuses guests and personal accounts by default, and fills the department from Microsoft Graph without ever depending on it. Tested against Entra-shaped mocks only; real Entra ID is not tested.
- People management (milestone M2): `GET /api/users` and `PATCH /api/users/{id}/role|status` for administrators. Nobody changes their own account, two administrators cannot remove each other at the same moment (row locks plus a re-check of the caller), a change ends the person's sessions, and every change is audited.
- Single sign-on guides for Keycloak, Microsoft Entra ID and any other OpenID Connect provider (`docs/guides/`), `make dev-sso` for a development Keycloak, `make org` and `make admin` for the first steps after the stack is up, a section on private certificate authorities, and a test that fails when a setting of the API cannot be set through Compose or is missing from `deploy/.env.example`.
- Browser tests against the real stack (`make test-stack-e2e`): Caddy over HTTPS, the web app, the API, PostgreSQL, Redis and a development Keycloak. They cover sign-in at the provider, the session cookie, sign-out ending the provider session, break-glass password sign-in, refusals, a disabled account, and the audit log. CI runs them and the Keycloak stack check as two new jobs.
- Project files: Apache-2.0 license, contribution guide with DCO sign-off, code of conduct, security policy, governance, issue and pull request templates.
- Architecture decision records 0001-0010, architecture, data model, threat model, API and deployment documents.
