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

With `make` the same two steps are `make org ORG_SLUG=default ORG_NAME="Your organization"` and `make admin ADMIN=admin`.

The slug must equal `PHAROS_ORG_SLUG`. For scripts, add `--password-stdin` (and `-T` to `exec`) and pipe the password in. `set-password` changes a password and `unlock --username NAME` clears the sign-in delays of an account.

### Add the starter sources

Pharos ships a list of 25 public sources (`backend/src/pharos/sources/starter_sources.json`: AI news feeds, releases of open-source tools and two Hacker News searches). Import it once; every source is added **switched off**, so nothing is fetched until an administrator reads the list on the Sources page and switches on what the organization wants:

```bash
docker compose --env-file deploy/.env -f deploy/docker-compose.yml exec api \
  python -m pharos.cli import-sources --dry-run
docker compose --env-file deploy/.env -f deploy/docker-compose.yml exec api \
  python -m pharos.cli import-sources
```

`--dry-run` shows how many sources would be added and how many are there already, and writes nothing. Running the command again adds only what is missing and never changes a source that is already there, so what an administrator renamed or switched on stays as it is. `--file PATH` imports your own list in the same format (`version` 1; each entry has `type`, `name`, `config`, `interval_seconds` and `note`; an entry cannot ask to be switched on). The whole file is checked first with the rules of the Sources API, and either everything is added or nothing is.

`PHAROS_LOCAL_LOGIN` controls password sign-in: `break-glass` (default) lets administrators sign in with a password so you can always get in when the identity provider is down, `on` allows every local account, `off` disables it. After repeated wrong passwords an account and address are slowed down with growing waits (never locked out); `unlock` removes the waits at once.

## Configuration (`deploy/.env`)

`deploy/.env.example` documents every variable. `make env` fills in the secrets. The file is
git-ignored and written with mode 600.

| Variable | Meaning |
| --- | --- |
| `POSTGRES_PASSWORD` | Database superuser. Used by PostgreSQL itself and the first-start script only. |
| `PHAROS_OWNER_PASSWORD` | Role `pharos_owner`: owns the database and runs migrations. |
| `PHAROS_APP_PASSWORD` | Role `pharos_app`: used by the API; cannot bypass row-level security. |
| `PHAROS_WORKER_DB_PASSWORD` | Role `pharos_worker`: used by the worker and the scheduler; no access to people or sign-in tables. |
| `PHAROS_REDIS_PASSWORD` | Redis user `pharos_api` (the API). Redis is not published on the host. |
| `PHAROS_REDIS_WORKER_PASSWORD` | Redis user `pharos_worker` (worker and scheduler): it can use only the Celery queue and `pharos:worker:*`, not sessions, rate limits or sign-in state. Must differ from the API password. |
| `PHAROS_DOMAIN` | Host name users open. |
| `PHAROS_TLS` | `internal`, an e-mail address (automatic certificate for a public domain), or paths of your own certificate and key. |
| `PHAROS_HTTP_PORT`, `PHAROS_HTTPS_PORT` | Host ports. |
| `PHAROS_PUBLIC_URL` | The address users open, as an origin (`https://pharos.example`, with `:PORT` if not 443). Production requires https. Redirects and the CSRF Origin check use it, never the Host header. |
| `PHAROS_GITHUB_TOKEN` | Optional. A fine-grained GitHub token with no scopes and no repository access; it only raises the API rate limit for GitHub release sources from 60 to 5000 requests an hour. Only the worker receives it (not the API and not the scheduler), it is sent to `api.github.com` over HTTPS and nowhere else, and a production API process refuses to start with it. Without it GitHub sources still work, within the lower limit. |
| `PHAROS_ORG_SLUG` | Short name of the one organization this installation serves (default `default`). Created by the operator after the stack is up; `/api/readyz` reports `organization: fail` until it exists. |
| `PHAROS_LLM_PROVIDER`, `PHAROS_LLM_API_KEY`, `PHAROS_LLM_MODEL_FILTER`, `PHAROS_LLM_MODEL_ENRICHMENT`, `PHAROS_LLM_MODEL_INSIGHT`, `PHAROS_LLM_BASE_URL`, `PHAROS_LLM_DAILY_BUDGET_MICRO_USD` | The model layer (M4). `off` (default) means no model is ever called and the product works without one. `anthropic`, `openai` or `azure` need a key and a model name per stage; the key is given to the worker only. Only public article text is sent. `PHAROS_LLM_BASE_URL` is for a provider that needs another **https** endpoint; an address inside the network is refused. The day's spending is capped at `PHAROS_LLM_DAILY_BUDGET_MICRO_USD` millionths of a US dollar (default 2000000, two dollars): at 80% an administrator is warned and at 100% no new summary is made while the ones that exist keep showing. `fake` exists for tests and demos and is refused in production. Every minute the worker reads up to `PHAROS_LLM_ENRICH_BATCH` (default 20) public articles nobody has read yet: a summary, a category, a quality score and the fit to each profession; poor or suspicious ones are held for an editor. Nothing is read while the model is off or the day's limit is reached. To change the key, edit the worker's `.env` and restart the worker; an administrator can then use "test the key" on the model page. |
| `PHAROS_PERSONAL_FEEDS` | `on` (default) lets every signed-in person follow up to five RSS or Atom feeds of their own, seen by nobody else. `off` removes the feature: the endpoints answer 404 and the worker stops fetching the feeds that exist (they are kept). Use it if a feed is abused: administrators cannot see or change a person's feeds. |
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

## Single sign-on

Password sign-in works out of the box; single sign-on is optional and switched on by the
`PHAROS_OIDC_*` settings above. Step-by-step guides:

- [Keycloak](guides/sso-keycloak.md): a throwaway Keycloak on your machine (`make dev-sso`), and your own Keycloak in production.
- [Microsoft Entra ID](guides/sso-entra.md): the Entra preset (one directory, object id as identity, department from Graph).
- [Any other OpenID Connect provider](guides/sso-generic.md): Google Workspace, Okta, Authentik, ...

Whatever the provider, keep `PHAROS_LOCAL_LOGIN=break-glass` (the default) so an administrator can
still sign in with a password when the provider is down, and create that administrator
(`make admin`) before you rely on single sign-on.

### Private certificate authorities

The API verifies the provider's certificate with the system store. If your provider (or a proxy in
front of it) uses a certificate from an internal authority, give the API a bundle that contains it.
The bundle **replaces** the system store for the provider connection, so it must also contain every
public authority the connection needs (Entra ID needs the ones for `login.microsoftonline.com` and
`graph.microsoft.com`). In a `deploy/docker-compose.override.yml`:

```yaml
services:
  api:
    environment:
      SSL_CERT_FILE: /etc/pharos/ca-bundle.pem
    volumes:
      - ./ca-bundle.pem:/etc/pharos/ca-bundle.pem:ro
```

`make test-sso-dev` does the same with Caddy's own authority; it is the working example.

## Services and networks

| Service | Networks | Published | Notes |
| --- | --- | --- | --- |
| `caddy` | edge, app | 80, 443 | TLS and routing: `/api` to the API, the rest to the web app. |
| `web` | app | no | Next.js standalone server. |
| `api` | app, data, egress | no | FastAPI. Calls out to the identity provider (from M2). |
| `worker` | data, egress | no | Celery. Calls out to content sources and LLM providers (from M3, M4). |
| `beat` | data | no | Celery scheduler. Talks to Redis only, so it has no route to the internet. |
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
creates `pharos_owner`, `pharos_app` and `pharos_worker` from the passwords in `deploy/.env` and
enables the `vector` extension. It does not run again on an existing volume. If you restore a database from a
backup, create the roles first (see [DATA_MODEL.md](DATA_MODEL.md#database-roles)).

- **First install and demos:** `PHAROS_MIGRATE_ON_START=true` applies migrations before the API
  starts.
- **Production:** set it to `false` in `deploy/.env`, review the new migration, back up, then run
  `docker compose -f deploy/docker-compose.yml --env-file deploy/.env run --rm -e PHAROS_MIGRATE_ON_START=true migrate`.
  Without the `-e` the job prints that migrations were skipped and exits successfully. Until the
  migration has run, `/api/readyz` reports `migrations: fail`.

### Upgrading to M3 (worker role and Redis users)

M3 gives the worker its own database role and its own Redis user, so a compromised worker cannot
read people or sign-in data. Two new variables are needed in `deploy/.env`:
`PHAROS_REDIS_WORKER_PASSWORD` and `PHAROS_WORKER_DB_PASSWORD`, each a new random value that differs
from the other passwords (`openssl rand -hex 24`).

The Redis user needs nothing else. The database role is created only when the data volume is first
created, so an installation that existed before M3 has none and the worker stops at start-up with
"recreate the volumes". There is no upgrade script: the demo installations this applies to hold no
data worth keeping, so remove the volumes (`docker compose -f deploy/docker-compose.yml down` with
the `--volumes` option) and start again. If you do have data to keep, create the role by hand with
the statements in [DATA_MODEL.md](DATA_MODEL.md#database-roles) before starting the new version.

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
| `worker` exits with "recreate the volumes" in its log | The database was created before M3 and has no `pharos_worker` role: see "Upgrading to M3". |
| `/api/readyz` shows `worker: fail` shortly after start | The worker has not written its first heartbeat yet; wait a few seconds. |
