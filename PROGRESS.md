# Progress

Management view, kept short. Task detail is in [TASKS.md](TASKS.md).

## Where the project is

- **Phase:** 1 (MVP). Milestones **M1** (skeleton and security baseline) and **M2** (sign-in, organization, onboarding) are complete (M2 on 2026-10-28). Next: **M3** (sources and content), which needs a plan approved by the maintainer first.
- **What exists:** a platform people can sign in to. Single sign-on with any OpenID Connect provider (Keycloak tested end to end, a Microsoft Entra ID preset tested with recorded shapes), local break-glass administrators, sessions, roles (administrator, editor, member) with people management for administrators, a four-question onboarding in four languages, an audit log, operator commands, and the Docker Compose stack with a development Keycloak. There is no content, no feed and no LLM yet.
- **Completion:** M1 10 of 10 tasks, M2 15 of 15 tasks (measured). Overall 2 of 5 milestones; that is a count, not an effort estimate, because the milestones differ a lot in size.

## Checks (state on `main` after the last merge)

| Check | Result |
| --- | --- |
| Pull-request pipeline (14 required checks: DCO, backend, integration, frontend, end-to-end, docs, Compose, secret scan, CodeQL x2, Trivy x2, Keycloak stack, stack end-to-end) | PASS |
| Backend: 541 unit, 383 integration (real PostgreSQL and Redis) | PASS |
| Frontend: 143 vitest, 250 Playwright against a mocked API (four languages, two themes, desktop and phone, axe WCAG 2.2 AA) | PASS |
| Real stack (Caddy over HTTPS, web app, API, PostgreSQL, Redis, development Keycloak): 46 Playwright tests incl. keyboard-only onboarding under two minutes and axe in four languages x two themes x two sizes | PASS |
| Compose stack: 72 checks (isolation, hardening, TLS, sign-in through the proxy, forged forwarded headers) | PASS |
| Keycloak stack: 35 checks (issuer, certificate trust, the whole sign-in as a browser does it, replay refused, sign-out) | PASS |
| Mutation checks (deliberately breaking security controls) | every new control caught; more than ten tests were too weak at first and were strengthened (see the session note) |
| Independent security reviews | M1: 3, M2: sessions, local accounts, single sign-on (2), people management and Entra (by the second session), onboarding, and the whole milestone. No P0 or P1; P2 fixed except F-14 (documented) |
| Coverage | NOT MEASURED |
| Load, performance, backup restore | NOT RUN |
| Real Entra ID tenant, any provider except a development Keycloak, a public domain with ACME | NOT TESTED |

## M2 acceptance criteria

| Criterion | Result |
| --- | --- |
| Sign-in end to end through Keycloak and through the Entra preset | Keycloak PASS (real stack); Entra PASS against recorded shapes only, **real Entra NOT TESTABLE** until M0-02 |
| User and department created on first sign-in; actions of the local admin in the audit log | PASS (integration and real-stack tests read the audit log) |
| RBAC in the service layer; a test fails for any route that is not covered, with a cross-tenant test | PASS (route-coverage gate, now also comparing the roles in the matrix with the roles enforced) |
| Onboarding keyboard-only, under two minutes, timestamps stored | PASS (mocked API and real stack) |
| All new text in four languages; axe on authenticated pages in 4 languages x 2 themes x 2 sizes | PASS (translations are the assistant's and unreviewed, F-01) |
| OIDC negative suite; independent review without open P0/P1; mutation checks | PASS |

## Risks

| Risk | State |
| --- | --- |
| R1 sensitive data pasted into an AI tool through "Try this" (P0) | Open by design; the card and admin tool list arrive with M4 |
| R2 pilot preparation (Entra registration, server, LLM budget) | Not started; blocks only the pilot (M0). Entra is the one provider path that has never met a real tenant |
| A compromised worker can write session records in Redis (F-14) | Open, documented; to be closed before the worker handles untrusted content (M3) |
| Egress by destination is not enforced by Compose | Open, documented (F-04) |
| Translations are unreviewed | Open (F-01) |
| Backup and restore undefined | Open (F-02) |
| Schedule: the plan assumes one developer with an assistant | M1 and M2 were built in about four calendar days including review rounds and a second parallel session; that is no basis for estimating M3-M5 |

## Next steps

1. Maintainer reviews ADR-0011 and the other ADRs marked Proposed ([TASKS.md](TASKS.md#decisions-waiting-for-the-maintainer)), and tries sign-in with the Keycloak guide (`make dev-sso`).
2. Start the M0 items that take calendar time: server and Entra ID test registration (M0-02, M0-03). Until then the Entra path is unproven.
3. Plan M3 and get the plan approved; close F-14 as part of it.
4. Have the az, tr and ru translations reviewed (F-01).
5. Decide the egress approach (F-04) before real data is stored.
