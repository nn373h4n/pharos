# Single sign-on with Keycloak

Two uses: a throwaway Keycloak on your machine to try single sign-on (development), or your own
Keycloak in production.

## Try it on your machine (development)

The repository ships a development Keycloak with two fictional people. It is **not for production**:
throwaway passwords, Keycloak in development mode.

```bash
make env                      # creates deploy/.env with generated passwords
```

Add to `deploy/.env` (the ports are the defaults of a development machine; change them if 80 or 443
are taken):

```bash
PHAROS_PUBLIC_URL=https://localhost
PHAROS_KEYCLOAK_PUBLIC_URL=https://auth.localhost
PHAROS_OIDC_ISSUER=https://auth.localhost/realms/pharos
PHAROS_OIDC_CLIENT_ID=pharos
PHAROS_OIDC_CLIENT_SECRET=dev-only-client-secret
PHAROS_OIDC_PROVIDER_NAME=Keycloak (development)
```

`deploy/.env` also needs `PHAROS_KEYCLOAK_ADMIN_PASSWORD`, the password of Keycloak's `admin`;
`make env` fills it in with a random value (a `.env` made before that needs the line added, for
example with `openssl rand -hex 24`). To keep the development stack off the network altogether, add
`PHAROS_BIND_ADDRESS=127.0.0.1`.

If you use other ports, put the HTTPS port in both URLs (`https://localhost:8443`,
`https://auth.localhost:8443`). Then:

```bash
make dev-sso                  # the normal stack plus Keycloak, behind the same Caddy
make org                      # creates the organization (once)
make admin                    # creates a local administrator (asks for a password)
```

The development Keycloak answers only clients on loopback or private networks, and its
administrator password is random per installation, so publishing the port does not open its admin
console to whoever reads this guide. Still do not run it anywhere that matters. The test and load
scripts (`make test-sso-dev`, `LOAD_SCENARIO=sso make loadtest`) make their own random password and
publish their ports on 127.0.0.1 only.

Open `https://localhost`, accept the certificate warning (Caddy signs with its own CA) and choose
**Keycloak (development)**. Sign in as `alice` (department Finance) or `bob` (Human Resources); the
password is `dev-only-password`. Keycloak's own admin console is at `https://auth.localhost`
(`admin` and the `PHAROS_KEYCLOAK_ADMIN_PASSWORD` from `deploy/.env`).

Signing out of Pharos also ends the Keycloak session. Pharos does not keep ID tokens, so it cannot
send an `id_token_hint`, and Keycloak therefore shows a short "Do you want to sign out?" page before it
sends the person back to the sign-in page. That is Keycloak following the specification, not an error.

Why a host name and not `localhost:8080`: the browser and the API container must see the **same**
issuer address. Caddy serves Keycloak on `auth.localhost` (browsers resolve every `*.localhost` name
to your machine), the same name is an alias of Caddy inside the stack, and the API trusts Caddy's
root certificate. `make test-sso-dev` checks all of that in a throwaway copy of the stack.

## Your own Keycloak (production)

1. In your realm create a client: **Client ID** `pharos`, **Client authentication** on, **Standard
   flow** on, every other flow (implicit, direct access grants, service accounts) off.
2. **Valid redirect URIs:** `<PHAROS_PUBLIC_URL>/api/auth/oidc/callback`. **Valid post logout redirect
   URIs:** `<PHAROS_PUBLIC_URL>/*`.
3. Under **Advanced** set **Proof Key for Code Exchange Code Challenge Method** to `S256`.
4. Copy the client secret from **Credentials**.
5. Optional: to prefill the department, add a **User Attribute** mapper that puts the attribute into
   the ID token under the claim name `department` (or set `PHAROS_OIDC_DEPARTMENT_CLAIM`).
6. In `deploy/.env`:

   ```bash
   PHAROS_OIDC_ISSUER=https://keycloak.example.org/realms/your-realm
   PHAROS_OIDC_CLIENT_ID=pharos
   PHAROS_OIDC_CLIENT_SECRET=<the secret>
   PHAROS_OIDC_PROVIDER_NAME=Company login
   ```

The issuer must be exactly what Keycloak puts in `iss` (with the `/realms/<name>` part, no trailing
slash). If Keycloak is behind a proxy, its hostname setting (`KC_HOSTNAME`) must produce that same
address. The API must be able to reach it over https; see "Private certificate authorities" in
[DEPLOYMENT.md](../DEPLOYMENT.md) if your Keycloak uses an internal CA.

Roles are not taken from Keycloak: a person who signs in for the first time is a plain member, and an
administrator changes roles in Pharos.
