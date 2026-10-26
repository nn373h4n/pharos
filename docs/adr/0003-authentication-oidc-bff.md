# 0003. Authentication: generic OIDC with the backend as BFF

- Status: Accepted
- Date: 2026-10-26

## Context

Pharos is installed by many organizations with different identity providers (Entra ID, Google
Workspace, Okta, Keycloak and others). Login must work with any of them, development and E2E
tests must not depend on an external IdP, and a demo install must work with no IdP at all.
Tokens must not reach the browser, and `org_id` must come only from the authenticated session.
Earlier planning documents disagreed on the fallback (e-mail, LDAP, Google, local accounts).

## Options considered

1. **Generic OIDC with the backend as BFF, local accounts as fallback** — pros: one standard
   covers most enterprise IdPs; tokens stay server-side; a single auth implementation.
   Cons: per-IdP claim differences (department, job title) need configurable mapping.
2. **NextAuth in the frontend** — pros: quick setup and many providers. Cons: a second auth
   logic next to the backend's RBAC and session handling; tokens and session state in the
   Next.js layer; harder to keep `org_id` derivation in one place.
3. **LDAP first** — pros: fits on-premises directories. Cons: the backend handles passwords;
   no MFA or SSO through the IdP; does not cover cloud-only directories.
4. **SAML first** — pros: common in enterprises. Cons: heavier protocol and libraries, more
   XML-signature pitfalls; most target IdPs also offer OIDC.

## Decision

Option 1 (accepted decision 3).

- OIDC Authorization Code flow with PKCE, `state` and `nonce`, and strict issuer and audience
  validation, for any compliant provider.
- The FastAPI backend is the BFF: it performs the code exchange and keeps tokens server-side.
  Sessions are stored in Redis; the browser gets only an httpOnly, Secure, SameSite=Lax cookie.
  Every state-changing request also requires a CSRF token. Next.js holds no tokens.
- Entra ID is the first preset; it reads department and job title from Microsoft Graph to
  prefill onboarding. Claim mapping is configurable per installation.
- Keycloak runs in the compose `dev` profile for development and E2E tests.
- Local accounts are the fallback (demo mode, break-glass admin); local admin actions are audited.
- LDAP and SAML are Phase 2.

## Consequences

- Positive: provider-agnostic login; tokens never reach the browser; one place derives `org_id`.
- Negative: the API needs egress to the IdP (and Graph for the Entra preset); the backend owns
  session lifecycle, revocation and CSRF.
- Follow-up tests: forged nonce, expired token, wrong audience/issuer, missing PKCE verifier,
  cross-origin POST without CSRF token; E2E login against Keycloak and the Entra preset.
- Follow-up docs: IdP setup guide and claim mapping in the deployment docs; threat entries in
  `docs/THREAT_MODEL.md`.

## References

- `docs/ARCHITECTURE_PROPOSAL.md` accepted decision 3, section 2 (row 3), 7, 8, 9, 11 (R10), 15
- [0005 Tenancy and RLS](0005-tenancy-and-rls.md)
