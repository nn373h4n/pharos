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
- Images are pinned by tag, not by digest, and are not scanned yet (M1-09). The build downloads
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
