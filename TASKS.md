# Tasks

Source of truth for tasks. Status: Todo / In Progress / Blocked / Done. Priority: P0 (blocks everything) to P3 (nice to have).
Milestone status is in [PROGRESS.md](PROGRESS.md); decisions are in [docs/adr/](docs/adr/).

## M1 — Skeleton and security baseline (complete)

All ten tasks were merged through pull requests. The pipeline arrived with M1-09, so PRs #1-#9 were verified locally and the later ones also by the pipeline. "Evidence" names the checks that proved the acceptance criterion.

| ID | P | Status | Task | Acceptance criterion | Evidence |
| --- | --- | --- | --- | --- | --- |
| M1-01 | P0 | Done | Repo skeleton, `.gitignore`, Makefile | `make help` lists every target | PR #1 |
| M1-04 | P0 | Done | Claude Code guard hooks and gitleaks pre-commit | Bypass forms of the deny list are blocked, normal commands pass | 126 guard cases PASS; a staged fake token is rejected; PR #2, #5 |
| M1-02 | P1 | Done | ADR 0001-0010, architecture, data model, threat model v0 | ADR template, statuses as decided | markdownlint PASS; PR #3 (0010: PR #9) |
| M1-03 | P1 | Done | Open-source files | README, DCO, Code of Conduct, issue and PR templates | link check PASS; `LICENSE` identical to the Apache-2.0 text; PR #4 |
| M1-05 | P0 | Done | Backend skeleton, JSON log masking, Alembic baseline with RLS | `/healthz`, `/readyz`; secrets masked; cross-organization RLS test | 110 unit + 21 integration PASS; 9 mutation checks; independent security review; PR #6 |
| M1-06 | P1 | Done | Celery worker, beat, heartbeat | Heartbeat every minute, visible in `/readyz` | integration with a real worker PASS; measured 59.96 s between beats; PR #7 |
| M1-07 | P1 | Done | Frontend skeleton in four languages | Missing key stops the build; axe: no violations | 31 vitest + 52 Playwright PASS; build fails on a missing key; PR #8 |
| M1-08 | P0 | Done | Docker Compose, networks, non-root, `.env.example` | Stack opens over HTTPS; database and Redis cannot reach the internet | `make test-compose` 41 checks PASS; 8 mutation checks; independent security review; PR #9 |
| M1-09 | P1 | Done | CI, security scans, Dependabot, branch protection | A failing check blocks the merge | A deliberately failing PR was blocked (Backend and DCO failed, merge refused); PR #10 |
| M1-10 | P2 | Done | TASKS, PROGRESS, CHANGELOG, session note | Statuses match reality; percentages only from measurable tasks | this pull request |

## M0 — Pilot readiness (owner: maintainer, parallel, blocks the pilot only)

| ID | P | Status | Task | Blocker or note |
| --- | --- | --- | --- | --- |
| M0-01 | P1 | Todo | Internal approval for the pilot and the pilot teams | Maintainer |
| M0-02 | P1 | Todo | Entra ID app registration in the pilot organization | Needed by M5; development uses Keycloak |
| M0-03 | P1 | Todo | Pilot server, plus a server and domain for the public demo | Compose needs Docker Engine 26+; see [DEPLOYMENT.md](docs/DEPLOYMENT.md) |
| M0-04 | P1 | Todo | LLM test key for development; pilot key, monthly budget, approved AI tool | Needed before M4 can use a real provider |
| M0-05 | P2 | Todo | Pilot professions, first source list, check of the name "Pharos" | Open questions 8, 12 and risk R15 |

## Follow-ups from reviews (none block the next milestone)

| ID | P | Status | Task | Origin |
| --- | --- | --- | --- | --- |
| F-01 | P1 | Todo | Native-speaker review of the az, tr and ru translations | Written by the assistant in M1-07; needed before the pilot |
| F-02 | P1 | Todo | Back up and restore procedure, rehearsed | Open in the architecture proposal (question 10); needed by M5 |
| F-03 | P1 | Todo | Strict Content-Security-Policy, with the sanitization work | ADR-0009; THREAT_MODEL residual risk |
| F-04 | P1 | Todo | Restrict which hosts `api`, `worker` and `beat` can reach (egress proxy profile or firewall guide) | ADR-0010; main gap against "default-deny egress" |
| F-05 | P2 | Todo | Pin base images and tools by digest; keep Dependabot updating them | Review of M1-08 |
| F-06 | P2 | Todo | Host firewall rule for the Docker bridge gateway: test it, then document the exact rule | ADR-0010; reasoned, not tested |
| F-07 | P2 | Todo | Add SBOM generation and image signing to releases | Proposal section 7, by M5 |
| F-08 | P3 | Todo | Run `caddy` as a non-root user | ADR-0010 |
| F-09 | P3 | Todo | Reject passwords with URL-unsafe characters at start-up | Review of M1-08 |
| F-10 | P3 | Todo | Cache or rate limit `/api/readyz` (it is hidden from the internet already) | Review of M1-05 |
| F-11 | P3 | Todo | Move to ESLint 10 and TypeScript 7 when `eslint-config-next` and `typescript-eslint` support them | M1-07; Dependabot ignores these majors |
| F-12 | P3 | Todo | English version of `PHAROS_MASTER_PROMPT.md` before the public launch (G3) | Proposal decision 7 |
| F-13 | P3 | Done | Trust `X-Forwarded-*` headers from the proxy in the API | Done in M2-03b: trusted from Caddy's pinned address only |
| F-14 | P2 | Todo | Keep the worker out of session records in Redis (ACL users or a MAC on the record) | Before the worker handles untrusted content (M3); see THREAT_MODEL residual risks |
| F-15 | P3 | Partly done | Tie a session to how the user signed in (local or SSO) and end local sessions when `PHAROS_LOCAL_LOGIN` is switched off or an admin loses break-glass rights | Review of M2-04 (P3-3); decide with the OIDC work (M2-05) | Done in M2-05: the session records the method and local sessions end when password sign-in is switched off; still open: a demoted break-glass admin keeps a local session, and under `break-glass` members' password sessions last until they expire (up to 24 hours).
| F-16 | P3 | Todo | Verification time of hashes made with older parameters differs from the dummy hash until they are rehashed | Review of M2-04 (P3-2); small timing signal, only for accounts not yet upgraded |
| F-17 | P3 | Todo | OIDC client hardening: refetch the signing keys on a TTL (a withdrawn key stays trusted until restart) and put one overall deadline on each provider call (the 10 s timeout is per chunk) | Security review of M2-05 (P3-2, P3-4) |
| F-18 | P3 | Todo | Negative test for the Entra `common` tenant issuer | Security review of M2-05 (P3-7); belongs to M2-06 |

## Decisions waiting for the maintainer

- Accept or change ADR-0006 (connector plugins), ADR-0007 (embeddings; needs the Azerbaijani evaluation), ADR-0009 (output sanitization, adds `nh3`) and ADR-0010 (network topology). All are `Proposed`.
- ADR-0005 was amended during the M1-05 review (separate read and write policies on content tables); confirm the amendment.
- Questions 7-12 in `docs/ARCHITECTURE_PROPOSAL.md` section 16 (team size, pilot professions, card format, retention, fully local mode, source list).

## M2 — Sign-in, organization, onboarding (plan approved 2026-10-27)

Plan: [docs/plans/m2-sign-in-organization-onboarding.md](docs/plans/m2-sign-in-organization-onboarding.md). Decisions: [ADR-0011](docs/adr/0011-sessions-csrf-idp-trust-and-organization-resolution.md) (`Proposed`). One pull request per task; execution order is the order of the rows.

| ID | P | Status | Task | Depends on | Acceptance criterion |
| --- | --- | --- | --- | --- | --- |
| M2-01 | P0 | Done | ADR-0011, plan copied into the repository, dependency approval | — | ADR in the template; plan and tasks recorded |
| M2-02 | P0 | Done | Migration 0002: departments, professions, users, local credentials, profiles, audit log, org lookup function | 01 | RLS test per table; cross-organization foreign keys rejected; audit log append-only; column grants; downgrade round trip |
| M2-10a | P1 | Done | Keycloak spike: dev profile behind Caddy, issuer hostname, CA trust | 01 | The API validates a Keycloak token through the proxy hostname |
| M2-03a | P0 | Done | Error format, clock, principal, unit of work, `current_user`, settings, lazy organization, route-coverage gate | 02 | A failing commit never returns 200; the gate fails on an uncovered route |
| M2-03b | P0 | Done | Session store, CSRF, Origin check, cookies, pinned proxy trust | 03a | Revocation, fixation, Origin and forged forwarded-header tests |
| M2-03c | P1 | Done | Rate limiter, audit service, event catalogue | 03b | Concurrent attempts counted exactly; audit entries without secrets |
| M2-04 | P0 | Done | Local accounts: login, logout, `/me`, operator CLI | 03c | Break-glass admin can sign in; no user enumeration; no hard lockout |
| M2-08 | P1 | Done | Profile and onboarding API, professions, interest codes | 03a | Profile validated against its organization; timestamps stored |
| M2-05 | P0 | Done | Generic OIDC with a fake-IdP negative suite, user creation on first sign-in | 03c | Every negative case in the plan is rejected; the first sign-in upsert never writes `role` or `status`; display names are truncated to 200 characters |
| M2-06 | P1 | Todo | Entra ID preset (tenant-pinned issuer, Graph prefill) | 05 | Tested against recorded or mocked Entra shapes; real Entra NOT TESTED |
| M2-07 | P0 | Todo | RBAC dependency, role and status endpoints, user list | 04 | Role × endpoint matrix; role change and disabling apply on the next request; two admins cannot demote each other concurrently (row lock) |
| M2-09a | P1 | Todo | Frontend: login page, app shell, 401 handling | 04 | Keyboard-only sign-in; axe clean in 4 languages |
| M2-09b | P1 | Todo | Frontend: onboarding stepper | 08, 09a | Keyboard-only; completes in under 2 minutes in a timed run |
| M2-10b | P1 | Todo | Compose wiring, `make env` additions, identity-provider guides | 10a, 05 | `make test-compose` still passes with the new settings |
| M2-11 | P0 | Todo | Stack E2E through Keycloak, CI job, security review, documentation | all | All M2 acceptance criteria in the plan |
