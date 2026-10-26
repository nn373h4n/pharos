---
name: security-checklist
description: Pharos security checklist for a change - tenancy, auth, SSRF, LLM boundary, XSS/CSRF, secrets, cost limits, supply chain. Use before committing security-relevant code or when asked for a security review.
---

# Pharos security checklist

Check only the areas the change touches. Mark each item OK, ISSUE (with file:line) or N/A.

## Tenancy and authorization
- `org_id` comes from the authenticated session (`current_org` dependency), never from body, query or headers.
- Every tenant-scoped read, write, background job, cache key, export and file path is scoped to the org.
- New tenant tables have an RLS policy; the app role does not own tables.
- RBAC (admin / editor / member) is enforced in the service layer, with a test per role.

## Authentication (OIDC)
- Authorization Code + PKCE; `state` and `nonce` verified; issuer, audience, signature and expiry checked.
- Tokens never reach the browser; the session cookie is httpOnly, Secure, SameSite=Lax.
- Login errors do not reveal whether an account exists.

## Outbound HTTP (SSRF)
- All requests go through `SafeHttpClient`.
- Scheme allowlist; DNS resolved and private, loopback, link-local and metadata ranges blocked; re-checked on every redirect; timeout and response-size limit set.

## LLM boundary
- External text is passed as delimited data; the system prompt says it is not instructions.
- The model gets no tools; output is schema-validated with a safe fallback.
- Output links are limited to the source URL; `try_prompt` has no links, no credential requests and no real data (placeholders only).
- Company-internal content goes to a cloud model only if the org policy allows it, and only after masking.
- The budget is checked before the call; usage is recorded without content.

## Web
- LLM and user content is rendered as text or sanitized markdown (`nh3` server-side); no `dangerouslySetInnerHTML` without sanitizing.
- State-changing requests carry a CSRF token; CSP and security headers are unchanged or tighter.
- Parameterized SQL only.

## Secrets and data
- No secrets, tokens, real emails or company data in code, tests, fixtures, logs or docs.
- New config has a fake value in `.env.example`.
- Logs mask secrets; audit events contain no content.

## Cost and abuse
- Rate limits and the LLM budget cover new endpoints and jobs; retries have backoff and a cap.

## Supply chain
- A new dependency is Apache-2.0 compatible, maintained, pinned in the lockfile and justified in the PR.

## Claims
- No privacy claim ("anonymous", "admins can't see", "data never leaves") without a test that proves it.
