# Deployment

Pharos runs as one Docker Compose stack on one Linux host: one installation serves one organization
([ADR-0005](adr/0005-tenancy-and-rls.md)). Network and container choices are explained in
[ADR-0010](adr/0010-compose-network-topology-and-container-hardening.md).

Status: M1. The stack starts and passes `make test-compose`. Identity provider setup, backups and
the production checklist are not written yet.

## Requirements

- Linux with Docker Engine 26 or newer and the Compose plugin (v2). Older engines have known
  weaknesses in how internal networks forward DNS.
- Free ports 80 and 443, or other ports set in `deploy/.env`.
- Memory: the demo stack ran in the checks on a host with 2 vCPUs and 8 GB, next to other
  workloads. The sizing for a pilot or for 30,000 users in `docs/ARCHITECTURE_PROPOSAL.md` section 9
  is an estimate, not a measurement.

## First start

```bash
git clone https://github.com/nn373h4n/pharos.git && cd pharos
make env   # creates deploy/.env with generated passwords
make dev   # builds the images and starts everything
```

Open `https://localhost`. With the default `PHAROS_TLS=internal`, Caddy signs the certificate with
its own CA, so the browser warns until you trust that CA. The first build takes several minutes.

`make down` stops the stack and keeps the data. `make dev` again starts it with the same data.

### Create the organization and the first administrator

The stack starts without an organization, and `/api/readyz` reports `organization: fail` until one exists. Create it once, then the first administrator (the password is read from a hidden prompt, never from the command line; the minimum is 15 characters):

```bash
docker compose --env-file deploy/.env -f deploy/docker-compose.yml run --rm migrate \
  python -m pharos.cli create-org --slug default --name "Your organization"
docker compose --env-file deploy/.env -f deploy/docker-compose.yml exec api \
  python -m pharos.cli create-admin --username admin
```

The slug must equal `PHAROS_ORG_SLUG`. For scripts, add `--password-stdin` (and `-T` to `exec`) and pipe the password in. `set-password` changes a password and `unlock --username NAME` clears the sign-in delays of an account.

`PHAROS_LOCAL_LOGIN` controls password sign-in: `break-glass` (default) lets administrators sign in with a password so you can always get in when the identity provider is down, `on` allows every local account, `off` disables it. After repeated wrong passwords an account and address are slowed down with growing waits (never locked out); `unlock` removes the waits at once.

## Configuration (`deploy/.env`)

`deploy/.env.example` documents every variable. `make env` fills in the secrets. The file is
git-ignored and written with mode 600.

| Variable | Meaning |
| --- | --- |
| `POSTGRES_PASSWORD` | Database superuser. Used by PostgreSQL itself and the first-start script only. |
| `PHAROS_OWNER_PASSWORD` | Role `pharos_owner`: owns the database and runs migrations. |
| `PHAROS_APP_PASSWORD` | Role `pharos_app`: used by the API and worker; cannot bypass row-level security. |
| `PHAROS_REDIS_PASSWORD` | Redis password. Redis is not published on the host. |
| `PHAROS_DOMAIN` | Host name users open. |
| `PHAROS_TLS` | `internal`, an e-mail address (automatic certificate for a public domain), or paths of your own certificate and key. |
| `PHAROS_HTTP_PORT`, `PHAROS_HTTPS_PORT` | Host ports. |
| `PHAROS_PUBLIC_URL` | The address users open, as an origin (`https://pharos.example`, with `:PORT` if not 443). Production requires https. Redirects and the CSRF Origin check use it, never the Host header. |
| `PHAROS_ORG_SLUG` | Short name of the one organization this installation serves (default `default`). Created by the operator after the stack is up; `/api/readyz` reports `organization: fail` until it exists. |
| `PHAROS_APP_SUBNET`, `PHAROS_CADDY_IP` | The `app` network has a fixed address range (default `172.28.20.0/24`) and Caddy a fixed address in it (default `172.28.20.254`, at the high end because Docker hands out the low addresses to the other containers). The API trusts `X-Forwarded-For` only from that address. The web container can reach the API too, so trusting the whole network would let it choose the client address that rate limits and the audit log record. Change both together if the range clashes with a network on the host. |
| `PHAROS_LOCAL_LOGIN` | `break-glass` (default: administrators may sign in with a password), `on` (every local account) or `off`. |
| `PHAROS_OIDC_ISSUER`, `PHAROS_OIDC_CLIENT_ID`, `PHAROS_OIDC_CLIENT_SECRET` | Single sign-on with any OpenID Connect provider. The issuer and the client id together switch it on. The issuer is the exact `iss` of the provider (https in production; the provider's discovery document must name the same issuer). Production also needs the client secret. Register `PHAROS_PUBLIC_URL` + `/api/auth/oidc/callback` as the redirect URI, and `PHAROS_PUBLIC_URL` + `/login` as the post-logout redirect URI if the provider asks for one. |
| `PHAROS_OIDC_PRESET`, `PHAROS_ENTRA_TENANT_ID` | `generic` (default) or `entra`. With `entra`, set the directory (tenant) id, a GUID, and leave the issuer empty: the issuer becomes `https://login.microsoftonline.com/<tenant>/v2.0`, the scopes gain `User.Read` (to read the department from Microsoft Graph) and the button says "Microsoft". The aliases `common`, `organizations` and `consumers` and domain names are refused, so only people of that one directory can sign in (also refused: the tenant of personal Microsoft accounts). Guests invited from another directory are refused when the token says so (`acct` or `idp` claims, which Entra sends only if you add them as optional claims); `PHAROS_ENTRA_ALLOW_GUESTS=true` allows them. The stronger control is in Entra: switch on "Assignment required" for the enterprise application and assign only the people or groups that should use Pharos. This preset replaces `PHAROS_OIDC_DEPARTMENT_CLAIM` (the department comes from Graph) and an empty or default scope list gains `User.Read`. Moving an installation from the generic provider to this preset gives every person a new account, because people are identified by object id instead of `sub`; do it before the first users sign in, or plan a manual migration of roles. In the app registration use the account type "single tenant", the web platform with the redirect URI above, and the delegated permission `User.Read`. People are identified by the object id, never by e-mail. Real Entra ID has not been tested yet (task M0-02). |
| `PHAROS_OIDC_SCOPES`, `PHAROS_OIDC_PROVIDER_NAME`, `PHAROS_OIDC_DEPARTMENT_CLAIM` | Scopes (default `openid profile email`), the name on the sign-in button, and the ID-token claim that holds the department (default `department`). |
| `PHAROS_SECRET_KEY` | Root secret (at least 32 random bytes; `make env` generates it). Keys for tokens and hashes are derived from it. Production refuses short or placeholder values. |
| `PHAROS_ENVIRONMENT` | `production` when unset. Production refuses placeholder passwords and serves no API docs. |
| `PHAROS_DEFAULT_LOCALE` | Language for visitors whose browser language is not supported: `en`, `az`, `tr` or `ru`. |
| `PHAROS_MIGRATE_ON_START` | `true` migrates before the API starts; see below. |

Passwords go into connection URLs, so use URL-safe characters, for example `openssl rand -hex 24`.

## Services and networks

| Service | Networks | Published | Notes |
| --- | --- | --- | --- |
| `caddy` | edge, app | 80, 443 | TLS and routing: `/api` to the API, the rest to the web app. |
| `web` | app | no | Next.js standalone server. |
| `api` | app, data, egress | no | FastAPI. Calls out to the identity provider (from M2). |
| `worker`, `beat` | data, egress | no | Celery. Call out to content sources and LLM providers (from M3, M4). |
| `migrate` | data | no | One-shot job, see below. |
| `postgres`, `redis` | data | no | No route to the internet. |

`app` and `data` are Docker `internal: true` networks: no route to the internet. `caddy` and `web`
are not on `data`, so they cannot connect to the database or to Redis.

**Compose does not restrict which hosts the egress services can reach.** Until you add a host
firewall rule or an outbound proxy for `api`, `worker` and `beat`, they can reach any internet
host. Add that rule before storing real data.

**Host services.** Traffic from a container to the host's own bridge gateway address does not pass
through the forwarding rules that make `internal` networks internal. A service that listens on all
host interfaces (SSH, a proxy, a Docker TCP API) can therefore be reached from every container. Add
a host firewall rule that drops input from the Docker subnets to anything the containers do not
need. This was reasoned about from Docker's documentation, not tested.

The profiles `dev`, `local-llm` and `observability` are reserved; their services arrive with the
milestones that need them.

## Database roles and migrations

The first start of an empty `postgres_data` volume runs `deploy/postgres/init/01-roles.sh`, which
creates `pharos_owner` and `pharos_app` from the passwords in `deploy/.env` and enables the
`vector` extension. It does not run again on an existing volume. If you restore a database from a
backup, create the roles first (see [DATA_MODEL.md](DATA_MODEL.md#database-roles)).

- **First install and demos:** `PHAROS_MIGRATE_ON_START=true` applies migrations before the API
  starts.
- **Production:** set it to `false` in `deploy/.env`, review the new migration, back up, then run
  `docker compose -f deploy/docker-compose.yml --env-file deploy/.env run --rm -e PHAROS_MIGRATE_ON_START=true migrate`.
  Without the `-e` the job prints that migrations were skipped and exits successfully. Until the
  migration has run, `/api/readyz` reports `migrations: fail`.

## Checking a running installation

`/api/readyz` is deliberately not served through the proxy. Ask the API container:

```bash
docker compose -f deploy/docker-compose.yml --env-file deploy/.env exec api \
  python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8000/api/readyz').read().decode())"
```

`make test-compose` builds and checks the whole stack in a throwaway project, with its own
volumes and ports, and removes it afterwards.

## Known limits

- Backup and restore are not defined or tested yet. A plain `pg_dump` of the `pharos` database is
  possible; it has not been rehearsed.
- Images are pinned by tag, not by digest. CI scans the two images we build (and, weekly, the
  proxy, database and cache images) with Trivy; Dependabot proposes updates. The build downloads
  `uv` (pinned by version) and `pnpm` (pinned by version in `package.json`) without a hash check;
  the Python and Node dependencies themselves are verified against `uv.lock` and `pnpm-lock.yaml`.
- The settings in `deploy/.env` are trusted. They are inserted into connection URLs and into the
  Caddy configuration without escaping, so a password with characters such as `@ : / # % $` breaks
  or redirects the URL, and `PHAROS_TLS` or `PHAROS_DOMAIN` can add Caddy directives. `make env`
  generates safe values. Only people who may administer the installation can write that file.
- The `cert key` form of `PHAROS_TLS` needs the files inside the container; mount them with a
  Compose override file, as no certificate directory is mounted by default.
- The published ports listen on all host interfaces, and Docker bypasses some host firewalls
  (for example `ufw`). Choose `PHAROS_HTTP_PORT`/`PHAROS_HTTPS_PORT` and your firewall deliberately.
- Behind the proxy the API does not yet trust `X-Forwarded-*` headers; client addresses and the
  scheme in redirects matter from M2 on.
- `caddy` is the only process that runs as root inside its container, with all capabilities
  dropped except `NET_BIND_SERVICE`. A container escape would therefore give root on the host.
- No HSTS header and no strict Content-Security-Policy yet.
- Redis and database passwords are environment variables, so a user who can run `docker inspect`
  on the host can read them. Treat access to the Docker socket as root access.
- Log retention, log shipping and monitoring are not configured.

## Troubleshooting

| Symptom | Cause |
| --- | --- |
| `set PHAROS_APP_PASSWORD` (or another variable) when starting | `deploy/.env` is missing or incomplete: run `make env`. |
| `api` exits right away with "real password" in its log | A password in `deploy/.env` is still `change-me`, or `PHAROS_REDIS_PASSWORD` is empty. |
| "port is already allocated" | Something else uses 80 or 443: set `PHAROS_HTTP_PORT` and `PHAROS_HTTPS_PORT`. |
| `/api/readyz` shows `worker: fail` shortly after start | The worker has not written its first heartbeat yet; wait a few seconds. |
