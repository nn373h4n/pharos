# 0011. Sessions, CSRF, identity-provider trust and organization resolution

- Status: Proposed
- Date: 2026-10-27

## Context

[ADR-0003](0003-authentication-oidc-bff.md) decided that the backend is the BFF and keeps sessions
in Redis, and [ADR-0005](0005-tenancy-and-rls.md) decided that one installation serves one
organization, protected by row-level security. Milestone M2 builds both. Several details were left
open, and an independent review of the first M2 design found that the obvious choices are unsafe:

- A role stored in the session stays valid after a user is demoted or disabled.
- An OIDC `state` that lives only in Redis does not bind the login to the browser that started it,
  so an attacker can sign a victim in as the attacker.
- Before sign-in the application does not know the organization, but RLS hides every row until
  `app.org_id` is set.
- FastAPI runs the exit code of a `yield` dependency after the response is sent by default, so a
  commit placed there can fail while the client already received 200.
- The project rule "outbound HTTP only through `SafeHttpClient`" (ADR-0006) blocks private
  addresses, but a corporate identity provider or a Keycloak container is private.

## Options considered

1. **Roles and status in the session** — pros: no database read per request. Cons: stale rights,
   no revocation. Rejected.
2. **JWT sessions in the cookie** — pros: stateless. Cons: no revocation or rotation without a
   server-side list, tokens would reach the browser. Rejected (ADR-0003).
3. **Server-side session with identity only; role and status loaded on every request** — pros:
   changes apply immediately; disabling a user can delete their sessions. Cons: one primary-key
   lookup per request. Chosen.
4. **Org resolution through a view** — a view owned by the table owner is not subject to RLS (the
   owner bypasses it) and exposes every organization's id and slug. Rejected.
5. **Org resolution through a `SECURITY DEFINER` function** with a fixed `search_path`, execute
   rights only for the application role, returning one id for one slug. Chosen.
6. **Authlib for OIDC** — pros: complete. Cons: a large surface with its own session handling that
   we would replace. A small explicit flow with `joserfc` and `httpx` is easier to audit and test.

## Decision

- **Session.** An opaque 256-bit id in the cookie `__Host-pharos_session` (Secure, HttpOnly,
  SameSite=Lax, Path=/). The Redis record holds `user_id`, `org_id`, `auth_time`, a CSRF seed and
  the sign-in `method` (`local` or `oidc`) only. Idle timeout 8 hours (sliding, in Redis), absolute 24 hours. The id is rotated at sign-in
  and on any privilege change. A per-user index of session ids lets "disable user", "change role"
  and "sign out everywhere" delete them. On every request the user row is read inside the tenant
  transaction; the request is refused unless the user is `active` and the session's organization is
  the configured one. Sessions and counters use their own Redis database and key prefix, and Redis
  being down means sign-in and session checks fail closed.
- **CSRF.** The token is an HMAC of the session id with a key derived from `PHAROS_SECRET_KEY`.
  Every `POST`, `PUT`, `PATCH` and `DELETE` needs an `Origin` header equal to `PHAROS_PUBLIC_URL`
  (never compared with `Host`, which the proxy rewrites); a missing or `null` `Origin` is rejected.
  A request that carries a live session also needs the token in `X-CSRF-Token`. Before a session
  exists (sign-in) the exact `Origin` match is the protection against login CSRF; a request whose
  cookie names no live session is treated the same way. WebSocket handshakes get the same `Origin`
  check. Session creation, rotation and revocation run as single Redis scripts, so "sign out
  everywhere" cannot fall between the steps of a sign-in.
- **OIDC.** Authorization code flow with PKCE (S256), `nonce` and `response_mode=query`. The
  `state` is bound to the browser by a short-lived `__Host-pharos_oidc` cookie, stored in Redis under
  a key that includes it, and consumed once with `GETDEL`; the PKCE verifier and the post-login
  target stay server-side, and the target must be a relative path. The ID token is checked for
  signature (algorithms RS256, PS256 or ES256, JWKS refetched once on an unknown `kid`), `iss`,
  `aud`, `azp`, `exp`, `iat` (leeway at most 60 seconds) and `nonce`; an RFC 9207 `iss` response
  parameter is checked when present.
- **Identity.** A user is identified by `(organization, issuer, subject)` and nothing else. E-mail
  is never used to find or link accounts and is not unique. First sign-in creates the user with an
  `INSERT … ON CONFLICT` and never re-activates a disabled one. For Entra ID the issuer is pinned
  to one tenant (`common` and `organizations` are refused) and the subject is the object id.
- **Identity-provider HTTP client.** A small dedicated client may talk only to the endpoints listed
  in the validated discovery document (whose `issuer` must equal the configured value) and to
  explicitly configured extras such as Microsoft Graph. It requires https outside development,
  sets timeouts and a response size limit and refuses redirects to other hosts. It is the only
  module allowed to import `httpx`, enforced by an import-linter contract. This is the one
  exception to the `SafeHttpClient` rule: that client is for untrusted public sources, and the
  identity provider is a configured, trusted endpoint that may be on a private network.
- **Organization before sign-in.** `pharos_org_id_for_slug(text)` is a `SECURITY DEFINER` function
  with `search_path = pg_catalog, pg_temp`, `EXECUTE` revoked from `PUBLIC` and granted to the
  application role. The organization is resolved lazily from `PHAROS_ORG_SLUG` and cached, and
  `/api/readyz` reports whether it exists. After that every query runs in `tenant_session`.
- **Unit of work.** One explicit transaction per request, committed by the service before the
  response is built. Dependencies that own it use `scope="function"`. The database is written
  first, then Redis. Audit rows for failed attempts use their own transaction so a rollback does
  not remove them.
- **Proxy trust.** The API trusts `X-Forwarded-*` only from Caddy's pinned address on a pinned
  subnet, never from the whole network (the web container can reach the API too).

## Consequences

- Positive: demotion, disabling and sign-out take effect on the next request; login CSRF, session
  fixation, e-mail takeover and a failed commit returning 200 each have a test; the trust boundary
  to the identity provider is one small module.
- Negative: one database read per authenticated request; more moving parts than a library would
  hide; the pinned subnet can clash with an existing network on the host, so it is configurable.
- Follow-up: the M2 tasks and tests in `docs/plans/m2-sign-in-organization-onboarding.md`; update
  `docs/API.md`, `docs/DATA_MODEL.md`, `docs/THREAT_MODEL.md`; a back-channel logout and a
  group-to-role mapping need their own decisions later.
- Dependencies introduced: `joserfc` (brings `cryptography`), `argon2-cffi`, and `httpx` moves from
  the development group to the runtime dependencies. Image for the `dev` profile:
  `quay.io/keycloak/keycloak`.

## References

- [0003 Authentication](0003-authentication-oidc-bff.md),
  [0005 Tenancy and RLS](0005-tenancy-and-rls.md),
  [0006 Connectors and SafeHttpClient](0006-connector-plugins-safehttpclient.md),
  [0010 Compose topology](0010-compose-network-topology-and-container-hardening.md)
- RFC 7636 (PKCE), RFC 9207 (issuer identification), RFC 9457 (problem details)
- `docs/plans/m2-sign-in-organization-onboarding.md`
