# Single sign-on with another OpenID Connect provider

Any provider that speaks OpenID Connect (Google Workspace, Okta, Authentik, Auth0, ...) works with
the generic setup. Pharos uses the authorization code flow with PKCE, so the provider must support
**PKCE with S256** and publish a discovery document at `<issuer>/.well-known/openid-configuration`.

## What to create at the provider

A **web application / confidential client** with:

- **Redirect URI:** `<PHAROS_PUBLIC_URL>/api/auth/oidc/callback` (exactly this, https).
- **Post-logout redirect URI** (if the provider asks for one): `<PHAROS_PUBLIC_URL>/login`.
- **Grant type:** authorization code. Nothing else is needed.
- **Scopes:** `openid profile email` (the default; change with `PHAROS_OIDC_SCOPES`).
- A **client secret**. Production requires one.

## Configure Pharos

```bash
PHAROS_OIDC_ISSUER=<the issuer, exactly as the provider writes it in "iss">
PHAROS_OIDC_CLIENT_ID=<client id>
PHAROS_OIDC_CLIENT_SECRET=<client secret>
PHAROS_OIDC_PROVIDER_NAME=<the name on the button>
# PHAROS_OIDC_DEPARTMENT_CLAIM=department
```

The issuer is a URL such as `https://accounts.google.com` or `https://company.okta.com`. The
discovery document must name **the same** issuer; if it does not, Pharos refuses the provider (this
stops a provider from being swapped for another one).

## What Pharos expects

- `sub` identifies the person. It must be stable for a person and unique within the issuer. If your
  provider issues a new `sub` for the same person (some do when an application is re-registered), the
  person gets a second account.
- `name` (or `preferred_username`) and `email` are used for display only.
- The department comes from the claim named by `PHAROS_OIDC_DEPARTMENT_CLAIM` (default `department`)
  if the ID token contains one. Providers differ here; if yours uses another name, set it. If there is
  no such claim, the person picks the department during onboarding.
- Roles are never read from the provider: first sign-in gives a plain member.
- Multi-factor authentication is the provider's job.

## Checking it

1. Open Pharos in a private window and press the provider's button.
2. Sign in at the provider; you should land on the app.
3. As an administrator open the audit log (or ask your operator). A failed sign-in always says why
   (`state_mismatch`, `token:bad_claims`, `code_rejected`, `discovery_failed`, ...); the person only
   sees a generic message.

If the provider is unreachable, the sign-in page says so and the break-glass administrator
(`PHAROS_LOCAL_LOGIN=break-glass`) can still sign in with a password.
