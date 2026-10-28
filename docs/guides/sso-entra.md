# Single sign-on with Microsoft Entra ID

Pharos has a preset for Entra ID. It pins the sign-in to **one** directory (tenant), identifies a
person by their object id, and reads the department from Microsoft Graph so onboarding can prefill it.

> Status: tested with recorded Entra token and Graph shapes and with a fake provider. It has **not**
> been tried against a real Entra tenant yet (task M0-02). Follow this guide on a test tenant first.

## 1. Register the application

In the Entra admin center: **App registrations → New registration**.

- **Name:** Pharos.
- **Supported account types:** *Accounts in this organizational directory only* (single tenant).
  Multi-tenant and personal accounts are refused by Pharos on purpose.
- **Redirect URI** (platform **Web**): `<PHAROS_PUBLIC_URL>/api/auth/oidc/callback`, for example
  `https://pharos.example.org/api/auth/oidc/callback`.

After creating it, copy the **Application (client) ID** and the **Directory (tenant) ID** from the
overview page.

## 2. Create a client secret

**Certificates & secrets → New client secret.** Copy the secret **value** (not the secret id) now; it
is shown once. Note the expiry date: sign-in stops when it expires, so put the renewal in a calendar.

## 3. Permissions

**API permissions** needs only **Microsoft Graph → Delegated → `User.Read`** (it is added by default).
That is what lets Pharos read the signed-in person's department. No admin consent is needed for it
in most tenants; if yours requires it, press *Grant admin consent*.

Pharos asks for the scopes `openid profile email User.Read` and never stores the Graph token.

## 4. Optional: only assigned people

**Enterprise applications → Pharos → Properties → Assignment required = Yes**, then assign users or
groups under **Users and groups**. Everyone else is refused by Entra before Pharos sees them.

## 5. Configure Pharos

In `deploy/.env`:

```bash
PHAROS_OIDC_PRESET=entra
PHAROS_ENTRA_TENANT_ID=<Directory (tenant) ID, a GUID>
PHAROS_OIDC_CLIENT_ID=<Application (client) ID>
PHAROS_OIDC_CLIENT_SECRET=<the secret value>
PHAROS_OIDC_PROVIDER_NAME=Microsoft
```

Leave `PHAROS_OIDC_ISSUER` empty: the preset builds it (`https://login.microsoftonline.com/<tenant>/v2.0`).
`common`, `organizations`, `consumers` and domain names are refused; use the GUID.

Guests invited from another directory are refused unless you set `PHAROS_ENTRA_ALLOW_GUESTS=true`.
Pharos recognizes a guest in two ways: the optional claims of the ID token (`acct`, `idp`) and the
`userType` that Microsoft Graph reports at every sign-in. If neither is available (Graph refused and
the token carries no such claim) a guest looks like a member, so for a guarantee use
**Assignment required** (step 4) and assign only the people who should come in.

Restart the API (`docker compose up -d api`). The sign-in page now shows the button.

## What Pharos does with an Entra sign-in

- The person is `(issuer, oid)`: the object id, which is the same in every application of the tenant.
  The e-mail address is information only and is never used to find or merge accounts.
- The token must come from your tenant (`tid`), be a version 2.0 token and carry an object id.
- A new person becomes a plain **member**. There is **no** group-to-role mapping yet: an administrator
  sets roles in Pharos.
- The department is read from Graph at every sign-in; if Graph is slow or refuses, the person still
  signs in, only without the department.

## Troubleshooting

| Symptom | Likely cause |
| --- | --- |
| Back on the sign-in page with "sign-in failed" | The redirect URI in the registration is not exactly `<PHAROS_PUBLIC_URL>/api/auth/oidc/callback`; or the secret expired. The audit log names the reason (`token:bad_tenant`, `code_rejected`, ...). |
| The API does not start: `entra_tenant_id must be the directory (tenant) id` | A domain name or `common` was used; use the GUID. |
| Sign-in works but the department is empty | Graph refused (`User.Read` not consented) or the person has no department in Entra. |
| A colleague from a partner company is refused | They are a guest; set `PHAROS_ENTRA_ALLOW_GUESTS=true` if that is what you want. |
