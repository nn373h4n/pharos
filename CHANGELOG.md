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
- Project files: Apache-2.0 license, contribution guide with DCO sign-off, code of conduct, security policy, governance, issue and pull request templates.
- Architecture decision records 0001-0010, architecture, data model, threat model, API and deployment documents.
