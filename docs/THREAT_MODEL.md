# Threat model

Status: v0 (milestone M1). Every control below must gain an automated test before the milestone that ships it is accepted. Nothing here is claimed as proven until its test exists.

## Assets

- Credentials: LLM API keys, OIDC client secret, session cookies, the encryption master key.
- Organization data: user profiles, interaction history, audit log, and (phase 2) internal ideas and stories.
- Integrity of what employees read: summaries and "how to apply" text generated from external content.
- The organization's LLM budget and the availability of the service.

## Trust boundaries

1. Browser ↔ Caddy/web/api (untrusted client).
2. api/worker ↔ external networks: OIDC provider, content sources, LLM providers (untrusted responses).
3. External content and LLM output ↔ the rest of the system (always untrusted data).
4. Application role ↔ database (RLS applies; the role is not the table owner).
5. Employee ↔ third-party AI tool, when the user pastes a "Try this" prompt.

## Threats and controls

| Threat | Control | Test |
| --- | --- | --- |
| Identity spoofing | OIDC Authorization Code + PKCE, `state`/`nonce`, issuer and audience checks; backend BFF, no tokens in the browser; httpOnly, Secure, SameSite=Lax session cookie in Redis | Forged nonce, expired token, wrong audience |
| Tenant escape | `org_id` only from the session; mandatory filter in the repository layer; PostgreSQL RLS as the second line; tenant-aware cache keys | Two-organization fixture, cross-tenant test for every endpoint |
| Missing authorization (RBAC) | `admin` / `editor` / `member` checked in the service layer, not only in the UI | Role × endpoint matrix; the role is read from the database on every request, administrators changing people lock the administrator rows and are re-checked, and nobody changes their own account (M2-07) |
| Prompt injection, content poisoning | Product LLM has no tools; external text wrapped in delimiters and declared as data; JSON schema validation; link filter; `try_prompt` validation; low-quality or suspicious items go to an editor queue | Injection corpus regression test |
| Data leak through "Try this" (R1, P0) | Each card opens only the AI tools the admin has approved, shows a "do not paste sensitive data" notice, and uses placeholders instead of real data; click counts are aggregate only | UX review, user training material |
| SSRF | `SafeHttpClient` (`backend/src/pharos/net`): http/https on ports 80 and 443 only, no credentials in URLs, allow-list of global unicast addresses plus an explicit deny list (NAT64, 6to4, Teredo, mapped IPv6, 192.0.0.0/24), every resolved address checked at connect time and the checked address used (no rebinding window), at most 3 redirects each re-checked, own public host refused, bearer token only to its own host over HTTPS, 2 MiB cap and total deadline, no compression, no proxy variables, coarse errors; default-deny network egress. The test-only address allowance is refused by `Settings` outside `environment=test` and is never passed by Compose | 169.254.169.254, DNS rebinding, redirect chain, `127.1` and decimal forms |
| XSS | LLM output is plain text or restricted markdown; server-side sanitization with `nh3`; strict CSP; React escaping | Malicious HTML/markdown corpus |
| CSRF | Same origin behind Caddy, SameSite=Lax cookie, CSRF token on mutating requests | Cross-origin POST |
| Secret exposure | AES-256-GCM envelope encryption, `key_version` rotation; masking filter on logs; gitleaks pre-commit and CI; fake data only in the repository | Search logs for key patterns |
| Data leaving through a compromised container (SSRF, exfiltration) | `postgres`, `redis` and `web` have no route to the internet (Docker `internal: true` networks); `caddy` and `web` are not on the `data` network, so they cannot reach the database or Redis; only `api`, `worker` and `beat` join `egress`; no database or Redis port is published; the database superuser cannot log in over the network; all containers drop every capability, forbid privilege escalation and have a read-only root file system | `make test-compose` (isolation checks, each with a positive control; database login checks; hardening checks) |
| Code execution through the task queue | Celery accepts JSON only (no pickle); Redis is reachable from the internal network only and requires a password in production; a task payload is data, never code | `tests/workers/test_app.py` (serializer and `accept_content`) |
| DoS and cost abuse | Per-user and per-organization rate limits (Redis); LLM budget with 80% warning and 100% stop; queue backpressure | Limit-exceeded tests |
| Supply chain | In place: lockfiles installed with `--frozen`, Dependabot, CodeQL, gitleaks and Trivy as required CI checks, GitHub Actions pinned to commit SHAs, CI tools run as digest-pinned images, DCO sign-off, protected `main`. Planned (TASKS F-05, F-07): digest-pinned base images, SBOM and signed images by M5 | Required CI checks |
| Misuse of the development agent | PreToolUse guard hook, deny rules, gitleaks pre-commit; planned devcontainer | `tests/hooks/test_guard.sh` |

## Audit

Audited events: sign-in and sign-out, role changes, LLM settings and keys, source added or removed, exports, budget changes. Audit records never contain secrets or content.

How it is built (M2): events come from a fixed catalogue (`AuditAction` in `backend/src/pharos/core/audit.py`; a new event is added there in the pull request that raises it). The application role can only INSERT and SELECT audit rows. A successful action writes its row in the same transaction as the change; a failed attempt writes its row in a transaction of its own so it survives the rollback (best effort: a failing audit write is logged and does not turn into an outage). The client address is stored only as an HMAC with a key derived from `PHAROS_SECRET_KEY`. Detail fields are short scalars, and a key that looks like a secret (`password`, `token`, `cookie`, ...) is refused as a programming error.

Sign-in throttling (`backend/src/pharos/core/ratelimit.py`): counters live only in Redis, keyed by hashes of the account and address. Per address: 30 attempts a minute. Per account and address: three free failures, then waits of 2, 4, 8 ... seconds up to 5 minutes. Per account across addresses: ten free failures, then waits up to 1 minute. There is no hard lockout, so knowing someone's account name does not let an attacker lock them out; an operator can clear the counters. Unknown accounts are throttled exactly like known ones.

## Company-internal content (phase 2)

Not sent to a cloud LLM by default. If an admin enables it, personal and sensitive data is masked first (email, phone, IBAN, national ID, personal names) and the setting change is audited. Privacy claims such as "anonymous" or "never leaves the network" are written only when the architecture and tests prove them.

## Residual risks and open items

- R1 depends on user behaviour; the controls reduce but do not remove it.
- Row-level security does not defend against code that executes arbitrary SQL as the application role, because that role can change `app.org_id` itself. Mitigations: parameterized queries only, LLM output and external text never reach SQL, the pool resets `app.org_id` on check-in, and `/readyz` verifies the role has no `BYPASSRLS`, is not a superuser and owns no table. See [DATA_MODEL.md](DATA_MODEL.md#row-level-security).
- The web app sends `X-Content-Type-Options`, `X-Frame-Options`, `Referrer-Policy` and `Permissions-Policy`, but not yet a strict Content-Security-Policy. The CSP is planned with the output sanitization work (ADR-0009); until then, XSS protection relies on React escaping and on rendering no untrusted content.
- Docker Compose cannot restrict *which* internet hosts the `egress` services reach. Until the operator adds a host firewall rule or an outbound proxy, a compromised `api`, `worker` or `beat` can reach any host. This is the main gap between the "default-deny egress" principle and what ships. See [DEPLOYMENT.md](DEPLOYMENT.md).
- Containers on an `internal: true` network can still reach services that listen on the host's bridge gateway address, because that traffic does not pass through the forwarding rules. A host firewall rule for the Docker subnets closes this (see [DEPLOYMENT.md](DEPLOYMENT.md)); it was reasoned about from Docker's documentation, not tested.
- `caddy` runs as root inside its container (all capabilities dropped except `NET_BIND_SERVICE`). Database and Redis passwords are environment variables readable by anyone who can run `docker inspect`.
- Session records in Redis are not signed, and the Celery worker uses the same Redis credentials as the API. A compromised worker could write a session record for any user, including an administrator. The sessions are in their own database and key prefix, which is separation by convention, not an access control. Closing this needs Redis ACL users (the worker without access to `pharos:session:*`) or a MAC on the record with a key the worker does not hold; tracked as follow-up F-14 and to be done before the worker handles untrusted content (M3).
- Single sign-on trusts the configured provider completely for who a person is: whoever controls an account at the provider can sign in as its subject. The controls are around the protocol, not the provider: state and nonce, PKCE, a one-time binding cookie, exact issuer and audience, a fixed signature algorithm list, identity by subject only (never e-mail; with Entra the object id inside one pinned directory, so a guest or another directory cannot collide), no role from the provider, and a disabled account that stays disabled. The provider's own multi-factor authentication is the provider's job. The API reaches the provider with a dedicated client (the one exception to `SafeHttpClient`) that follows no redirects, limits sizes and calls only endpoints from the validated discovery document (plus, with the Entra preset, one fixed Microsoft Graph address with a short timeout); a malicious discovery document can still point the API at an internal address, which is why the issuer is configuration, never user input. Tested against a fake provider (negative suite) and a real Keycloak; Entra ID only with recorded shapes (M2-06).
- The single sign-on start and callback are limited to 120 requests a minute per source address, and refusals that anyone can cause without having started a flow (no state, unknown state, throttled) are logged but not written to the audit log one by one. People behind one shared egress address share that allowance; it is meant to stop a loop, not a crowd. A flood from many addresses is a job for the edge (firewall or proxy), not for this counter.
- Local sign-in commits the database (last login, audit row) before it creates the Redis session, as ADR-0011 says. If Redis fails in between, the audit log records a successful sign-in for which no session exists; the user sees a 503 and tries again. A local session also stays valid after `PHAROS_LOCAL_LOGIN` is switched off until it expires (follow-up F-15), and a stored hash made with older parameters verifies in slightly different time than the dummy hash used for unknown users (F-16).
- The worker and the scheduler are started without the root key, the identity-provider secret and the sign-in mode (`PHAROS_PROCESS=worker`; Settings refuses to start a worker that is given them), so a compromised worker cannot read them from its environment. It still holds the Redis and database credentials, which is why F-14 stays open.
- Redis has a memory limit (`--maxmemory 200mb`, below the container's 256 MB) with `noeviction`: when it is full it refuses writes and sign-in fails closed, instead of the kernel killing it together with every session. Unfinished single sign-in records (10 minutes each) are bounded per address (an IPv6 /64 counts as one) and for the whole installation (600 a minute).
- The password path takes no database connection while it waits for the hashing queue, and a request takes none until it uses the database, so a burst of sign-ins cannot use the connection pool up. The single sign-on callback calls the provider before it touches the database for the same reason.
- The API trusts `X-Forwarded-For` from Caddy's pinned address (`PHAROS_CADDY_IP`). Another container could only use that address by taking it over, which Docker prevents inside a network while Caddy runs; `NET_ADMIN` and `NET_RAW` are dropped everywhere, so address spoofing from the web container is not possible. Not tested beyond the forged-header checks in `make test-compose`.
- Application logs pass through a regex masker. It covers common key names, headers, URLs and token formats, not every possible secret, so secrets must still not be logged on purpose.
- Azerbaijani output quality and local-model quality are unmeasured.
- The development agent guard is a guard rail, not a sandbox; its known limits are listed in `.claude/hooks/pre_bash_guard.py`.
- Backup, restore and retention defaults are undefined.
- External source terms of service must be checked per connector before it is enabled.
