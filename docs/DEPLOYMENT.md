# Deployment

Pharos runs as one Docker Compose stack on one Linux host: one installation serves one organization
([ADR-0005](adr/0005-tenancy-and-rls.md)). Network and container choices are explained in
[ADR-0010](adr/0010-compose-network-topology-and-container-hardening.md).

Status: M5 in progress. The stack starts and passes `make test-compose`, which now also rehearses a
backup and a restore. The production checklist and the pilot runbook are not written yet.

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
| `PHAROS_HSTS_MAX_AGE` | Seconds a browser keeps using HTTPS for this host (`Strict-Transport-Security`; default one year, `0` clears it). No `includeSubDomains` and no `preload`: those are decisions for the whole domain. Set `0` first on a host that was served over HTTPS by mistake, and on a development machine that uses `localhost` (HSTS applies to every port of a host, so other local servers would be forced to HTTPS). |
| `PHAROS_HTTP_PORT`, `PHAROS_HTTPS_PORT` | Host ports. |
| `PHAROS_PUBLIC_URL` | The address users open, as an origin (`https://pharos.example`, with `:PORT` if not 443). Production requires https. Redirects and the CSRF Origin check use it, never the Host header. |
| `PHAROS_GITHUB_TOKEN` | Optional. A fine-grained GitHub token with no scopes and no repository access; it only raises the API rate limit for GitHub release sources from 60 to 5000 requests an hour. Only the worker receives it (not the API and not the scheduler), it is sent to `api.github.com` over HTTPS and nowhere else, and a production API process refuses to start with it. Without it GitHub sources still work, within the lower limit. |
| `PHAROS_ORG_SLUG` | Short name of the one organization this installation serves (default `default`). Created by the operator after the stack is up; `/api/readyz` reports `organization: fail` until it exists. |
| `PHAROS_LLM_PROVIDER`, `PHAROS_LLM_API_KEY`, `PHAROS_LLM_MODEL_FILTER`, `PHAROS_LLM_MODEL_ENRICHMENT`, `PHAROS_LLM_MODEL_INSIGHT`, `PHAROS_LLM_BASE_URL`, `PHAROS_LLM_DAILY_BUDGET_MICRO_USD` | The model layer (M4). `off` (default) means no model is ever called and the product works without one. `anthropic`, `openai` or `azure` need a key and a model name per stage; the key is given to the worker only. Only public article text is sent. `PHAROS_LLM_BASE_URL` is for a provider that needs another **https** endpoint; an address inside the network is refused. The day's spending is capped at `PHAROS_LLM_DAILY_BUDGET_MICRO_USD` millionths of a US dollar (default 2000000, two dollars): at 80% an administrator is warned and at 100% no new summary is made while the ones that exist keep showing. Suggestions ("Try this") may use only half of the day's limit, and one person may ask for at most 30 a day, so asking for suggestions cannot stop the reading of new articles. `fake` exists for tests and demos and is refused in production. Every minute the worker reads up to `PHAROS_LLM_ENRICH_BATCH` (default 20) public articles nobody has read yet: a summary, a category, a quality score and the fit to each profession; poor or suspicious ones are held for an editor. Nothing is read while the model is off or the day's limit is reached. To change the key, edit the worker's `.env` and restart the worker; an administrator can then use "test the key" on the model page. |
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

Passwords go into connection URLs, so they may contain only letters, digits and `_ . -`, for example
`openssl rand -hex 24`. Anything else (`@ : / ? # %` would change how the address is read, so a
process could send another password or reach another host) is refused at start-up: by the API, the
worker, the migrations and the operator commands, by Redis, and by PostgreSQL's init script on a new
volume. That refusal leaves an initialized but empty data directory: fix `deploy/.env`, remove the
postgres volume and start again. The refusals never print the password.

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
| `worker` | data, egress, llm | no | Celery. Calls out to content sources and LLM providers (from M3, M4); `llm` holds the local model server when its profile runs, and is empty otherwise. |
| `operator` | data | no | Not started by `up` (profile `operator`). Runs one retention or deletion command as the database owner and exits; see [Retention and deleting a person](#retention-and-deleting-a-person). |
| `beat` | data | no | Celery scheduler. Talks to Redis only, so it has no route to the internet. |
| `migrate` | data | no | One-shot job, see below. |
| `postgres`, `redis` | data | no | No route to the internet. |
| `ollama` (profile `local-llm`) | llm (fixed address) | no | The local model server. Not started by default. See "The local model". |
| `ollama-init` (profiles `local-llm`, `local-llm-pull`) | none | no | Gives the model volume to the model user, then exits. |
| `ollama-pull` (profile `local-llm-pull`) | pull | no | One-shot model download; the only part with a route to the internet, on a network of its own. |

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

The profiles `dev` and `observability` are reserved; their services arrive with the milestones
that need them. The profile `local-llm` is described under "The local model" below.

## Database roles and migrations

The first start of an empty `postgres_data` volume runs `deploy/postgres/init/01-roles.sh`, which
creates `pharos_owner`, `pharos_app` and `pharos_worker` from the passwords in `deploy/.env` and
enables the `vector` extension. It does not run again on an existing volume. A dump does not hold
the roles, so a restore goes into a new volume where this script has just run (see "Backup and
restore" below and [DATA_MODEL.md](DATA_MODEL.md#database-roles)).

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

## Near-duplicate articles (embeddings)

By default every article is its own card. To show one card for one story, set
`PHAROS_EMBEDDING_PROVIDER` and `PHAROS_EMBEDDING_MODEL` in `deploy/.env` and restart the worker.
Every minute the worker then embeds up to `PHAROS_EMBEDDING_BATCH` public articles that the model has
read, and joins an article to an earlier one when their similarity reaches
`PHAROS_EMBEDDING_SIMILARITY`. Nothing is merged or deleted: the copies stay as articles, editors
still see them, and the Home feed shows the best one.

- **Provider.** `ollama` uses the local model server (see "The local model"): pull an embedding
  model with `scripts/ollama-pull.sh <model>` and write its name as `PHAROS_EMBEDDING_MODEL`.
  `openai` or `azure` use the chat provider's key, so `PHAROS_LLM_PROVIDER` must be the same; the
  Anthropic chat provider has no embeddings, so use `ollama` with it. Only public article text is
  sent, masked, like any other model call.
- **The model is yours to choose.** It must handle your languages (Azerbaijani, Turkish, Russian,
  English). The vector size follows the model. Changing the model means embedding every recent
  article again (the sweep does it by itself); the old vectors are then ignored.
- **The threshold.** `0.92` is a cautious start, not a measurement: with the one real model that
  was tried (`bge-m3`, 14 hand-written pairs in four languages) it joined nothing at all, while 0.80
  joined every copy with no wrong join. Do not leave it untuned. A threshold that is too low
  hides an article behind another; one that is too high shows a story twice. Tune it on your own
  articles: write 30 to 100 pairs of articles, each labelled the same story or not, and run
  `docker compose exec worker python -m pharos.evaluation dedup --pairs pairs.json`
  (`docs/guides/dedup-pairs.sample.json` shows the format). Choose the lowest threshold whose
  "wrong" column is still zero.
- **Cost.** A remote embedding is booked against the day's limit like other calls, at
  `PHAROS_EMBEDDING_PRICE_MICRO_USD`; a local one costs nothing.
- **Upgrading.** The database needs the `vector` extension, which a superuser creates when the
  database is first made. An installation older than that has it already (M1 created it); if the
  migration stops and says it is missing, run once:
  `docker compose exec postgres psql -U postgres -d pharos -c 'CREATE EXTENSION IF NOT EXISTS vector'`.

## The local model (Ollama)

With `PHAROS_LLM_PROVIDER=ollama` the worker asks a model that runs on this host, so article text
does not go to a provider. Nothing else changes: the same prompts, the same schema checks, the
same stage models (write the Ollama names, for example `qwen2.5:7b`, in `PHAROS_LLM_MODEL_*`).

1. Put a model into the volume. The model server has no route to the internet, so a separate
   one-shot job downloads it, and only while it runs: `scripts/ollama-pull.sh qwen2.5:7b`
2. Start the server with the profile `local-llm`:
   `docker compose -f deploy/docker-compose.yml --env-file deploy/.env --profile local-llm up -d`
3. Set `PHAROS_LLM_PROVIDER=ollama` and the model names in `deploy/.env`, then restart the worker.

What this does and does not give you:

- The server runs as a non-root user with a read-only file system and no capability, on the
  internal network `llm`, which only it and the worker share. It publishes no port and it reads its
  models without being able to change them. The network has no gateway to the internet, so the
  server has no route out of its own. As for the other internal networks (see "Known limits"), a
  service on the host that listens on all interfaces, such as a proxy, can be reached through the
  network's gateway address: name the `llm` range in your host firewall rule too.
- The worker reaches the server by its fixed address `PHAROS_OLLAMA_IP` (default `172.28.30.10`,
  inside `PHAROS_OLLAMA_SUBNET`), never by a name, and ignores any proxy setting of the
  environment for these calls. If the range clashes with a network on the host, change both
  variables. No key and no URL are accepted for this provider (ADR-0020).
- `scripts/local-llm-check.sh` repeats the isolation checks on a real server (it pulls the image on
  its first run, several GB, so it is not part of the pipeline).
- A local model costs nothing per token, so the daily money cap does not stop it. The calls per
  minute limit and the timeouts still apply.
- It needs memory (`PHAROS_OLLAMA_MEMORY`, 6 GB by default) and, for a useful model, a lot more.
  There is no GPU setup.
- The model files come from the Ollama registry as it serves them; choose a model on purpose.
- It is **not** "fully local": the worker still has the `egress` network to fetch sources.
  Narrowing that is a host firewall or proxy decision (see "Known limits").
- Its quality in Azerbaijani, Turkish and Russian is unmeasured until you run the evaluation
  below on it (the tool works with `PHAROS_LLM_PROVIDER=ollama`). Do not call the mode usable
  before.

## Choosing the models (evaluation)

The default model of each stage should be chosen from a human-rated evaluation in the languages of
your people. The tool and the steps are in [docs/guides/evaluation.md](guides/evaluation.md); run
it in the worker's container (`docker compose exec worker python -m pharos.evaluation ...`). A real
run costs money, capped by `--max-cost-usd` (five dollars by default).

## Backup and restore

The decision is [ADR-0022](adr/0022-backup-is-a-script-and-a-restore-that-is-rehearsed.md) (`Proposed`):
two scripts run by the host, no resident service. A dump that has never been restored is not a
backup, so restore it once on a spare machine before you rely on it.

**What is backed up, and what is not.** The dump holds the PostgreSQL database (articles, people,
settings, the audit log, stored model usage). It does **not** hold the roles (they are made by
`deploy/postgres/init/01-roles.sh` on a new volume), Redis (sessions and queues; people sign in
again after a restore) or `deploy/.env`. Keep `deploy/.env` separately, and never beside the dumps:
it holds every password and the root secret, so a dump together with it is the whole installation.
Restore with the same root secret: nothing in the database is encrypted with it, so a dump is
readable without it, but the address hashes in the audit log are keyed with it, and with another
secret old and new entries can no longer be matched by address.

**Backing up.** From the repository directory, as a user who may run Docker:

```bash
scripts/backup.sh run --dir /srv/pharos-backups/dumps       # the database
scripts/backup.sh env --dir /home/operator/pharos-env \
  --dumps /srv/pharos-backups/dumps                          # deploy/.env, somewhere else entirely
```

`run` makes a custom-format `pg_dump` as the database owner through the container's local socket
(no password on a command line) and reads it back **to its end** with `pg_restore`, not only its
table of contents. It refuses an empty dump, one it cannot read or one without the schema, writes a
SHA-256 file beside each dump, and keeps the 7 newest daily dumps, 4 weekly (made on Sundays) and 3
monthly (made on the 1st), in `daily/`, `weekly/` and `monthly/` under the directory you name. Only
one run can go at a time. A dump is never pruned in the run that made it, and the script warns if a
dump is stamped later than the new one (a clock that was wrong once would otherwise push every new
dump out). `env` refuses a directory that is, holds or sits inside the dump directory.

The directory must be yours and closed to others: an existing directory that someone else owns, that
has any permission for group or others (so `chmod 700` an existing one first) or that is a link is
refused, and so is any path inside this repository, whether you name it directly, relatively (a
relative path is read from where you run the script) or through a link, so that `git add -A` can
never commit a dump or the root secret; `.gitignore` is a second line. A directory with a POSIX
access control list is refused too, even when its mode is 700: a default list is inherited by every
dump made in it, whatever the umask (`getfacl` shows it; `setfacl -b -k` removes it). Every directory
above it must belong to root or to you and must not be writable by others unless it has the sticky
bit (like `/tmp`), because whoever can rename a parent can swap the dumps. So a directory under
another person's home (`/home/alice/...` used by root or by a separate backup user) is refused;
`/srv/...`, `/var/backups/...` and a directory under your own home are fine. NFSv4 and SMB access
lists are not POSIX lists and are not seen: on such a share, check them yourself. The files are
private (modes 700 and 600). Keep
the dump directory and the `.env` copy under **different parents**: one routine sync of a shared
parent would ship both together. Schedule `run` with the host's scheduler (cron or a systemd timer),
for example once a night, and check now and then that new files appear: nothing alerts on a missed
backup.

**The operator's part.** Copy the dumps off the host and encrypt them and the `.env` copy before
they leave it; Pharos ships no destination and no encryption. A dump holds every person's data.

**A dump is code.** `pg_restore` runs what the dump says as the database owner, and the owner owns
every table. A dump that someone changed can switch row-level security off, add a permissive policy,
grant the application role more, change default privileges, add a function that runs with the
owner's rights or add an administrator. Restore only a dump whose origin you trust.

The SHA-256 file (`<dump>.sha256`, one line, `<hash>  <the dump's own file name>`) catches damage;
it catches a *changed* dump only if whoever changed the dump could not change the file beside it, so
keep the checksums, or the dumps encrypted with authenticated encryption (for example `age`), where
the keepers of the dumps cannot rewrite them. `restore.sh` copies the dump once into a private
directory, takes the hash from that copy and reads only the copy afterwards; it refuses a checksum
file that is missing, has more than one line, names another file or does not match. There is no
option to skip the check: for a dump you made by hand, make its checksum file by hand with
`sha256sum`.

After the restore, `restore.sh` first looks for two things a good dump never makes: settings on the
database or on a role, and schemas other than `public`, and it refuses any extension other than
`vector` (and the built-in `plpgsql`) or one that a superuser does not own: the owner role owns the
database, so a changed dump could make a trusted extension of its own and hide objects in it. Every
query on the restored database resolves names in `pg_catalog` only, because a dump can make a schema
named after the owner, which is first in the owner's default `search_path`, with operators that
change what an unguarded query says. The roles live outside the database, so the script also takes
a digest of every role (attributes and password hash), membership and setting as the superuser
before and after `pg_restore`, and refuses any difference: the owner may change its own password,
and a dump can make it do so.

Then it **compares the restored database with what the migrations make** (F-44). It reads the
dump's schema version and builds a reference at that version in two scratch databases on the same
server, with the database grants the init script gives: the migrations of the `migrate` image run
in the first (the image of the stack you restore into, so restore with the release that made the
dump or a newer one), and the result goes through `pg_dump` and `pg_restore` like the real dump, so
both are written the way PostgreSQL writes a restored database. `scripts/restore/catalog.sql` then
compares, by name: the database's owner and grants, whether it is a template, takes connections and
how many; schemas; relations with their owner, kind, persistence (an `UNLOGGED` table, such as an
audit log made so, is emptied after any crash), replica identity, row-level security flags, options
and columns (type, collation, storage and compression, defaults); the grants on relations, columns, functions,
schemas, types, languages, large objects, foreign servers and wrappers, and default privileges;
policies in full; functions (owner, whether they run with the owner's rights, settings, the whole
definition); aggregates; triggers, rules, views, constraints and indexes; inheritance; types and
domains; operators, casts, collations, conversions, text search configurations and dictionaries,
statistics, publications, user mappings and event triggers; comments; extensions and settings. Objects inside an extension
are not compared (only a superuser can have made them, see above). Any difference stops it with the
names of what differs (`only in the dump: policy|public.users.…`); then do not start the stack,
remove the postgres volume and find out where the dump came from. The scratch databases are removed
at the end; the comparison adds a few seconds. A dump from a newer Pharos than the `migrate` image
is refused, because there is nothing to compare it with.

**What it still cannot see: the rows.** A hostile dump can still carry hostile *data*, for example
an extra administrator or changed settings in the tables, and pass. The origin of a dump still
matters: keep the checksums out of reach of whoever keeps the dumps, as above.

**Restoring**, on a new installation, before anything else is started:

1. Put `deploy/.env` back (the same root secret).
2. Start only the database on a new volume: `docker compose -f deploy/docker-compose.yml --env-file deploy/.env up -d postgres`. The init script creates the roles and the `vector` extension.
3. `scripts/restore.sh /srv/pharos-backups/dumps/daily/pharos-YYYYMMDDTHHMMSSZ.dump` (a relative
   path is read from where you run it). It refuses a dump whose checksum file is missing or does not
   match, a file that is not a Pharos dump, a database whose two roles do not both exist, are not
   plain login roles or are members of other roles, and a database that already has tables, views,
   sequences, functions or default privileges. It restores in one transaction, but **a failed
   restore is not proof that nothing was written**: a hostile dump can end that transaction itself
   and keep what came before the failure (a default privilege, a function, a role's setting or
   password). After any failure, remove the postgres volume and start on a new one; do not start the
   stack on it. The script first copies the dump once into a private directory
   (`PHAROS_RESTORE_TMP`, else `TMPDIR`, else `/tmp`, which is memory on many hosts): for a large dump
   name a directory on a real disk with room for it. That directory and every directory above it
   must belong to root or to you and must not be writable by others unless the sticky bit is set
   (like `/tmp`), or someone could swap the copy after its checksum was checked. The free space is
   checked before the copy, and the copy is removed on every way out.
4. Start the rest: `docker compose ... up -d`. The migration step finds the schema at its latest
   version. People sign in again.

**What was rehearsed.** `make test-compose` backs up its throwaway installation, destroys the
volumes, starts a new database, restores, and checks that the schema version, the **number of rows**
of every table, row-level security, the policies (with whether they are permissive or restrictive),
the exact column grants of the two login roles, the functions (arguments, a hash of the body, owner,
whether they run with the owner's rights, settings, grants), the triggers, the owner and grants of
every relation, the schemas, the default privileges, the database settings, the roles' attributes
and their memberships are identical (a failed query stops the snapshot, so a broken query cannot
hide behind two equal halves); that a damaged, an unsigned and a non-Pharos file are refused;
that the services become healthy, that `readyz` is ok, that the administrator signs in with the same
password, that the Home feed shows the same article before the backup and after the restore, that a
session from before the backup no longer works (Redis is not backed up, so people sign in again),
and that five access checks of the two roles (two that must work, three that must be refused) still
give the same answers. On a small database the backup and the restore each took about a second to
three seconds; that is no promise for a large one. **Not rehearsed:** a restore on the pilot server
(gate G8, only the maintainer can pass it), a large database, a dump from an older version of
Pharos restored into a newer one, the content of the rows (only their counts are compared), the audit
chain read with the root secret, a real dump that is cut short (that `backup.sh` refuses it is shown
only with a stand-in for Docker; the rehearsal changes a dump with a checksum, which `restore.sh`
refuses), and the time to copy a dump off the host.

## Retention and deleting a person

Three commands do this work. They run as the **database owner** in the `operator` service, which is
never started by `up` and holds only the owner's address, the organization, the API's Redis user
(to end sessions) and the retention defaults; it has no root key, no identity-provider secret, no
application database password and no model key. The application and the worker cannot delete any of
this. Each command only reports unless `--confirm` is given, and the owner role ignores row-level
security, so each command names the organization (`PHAROS_ORG_SLUG`) and filters by it.

```bash
docker compose --env-file deploy/.env -f deploy/docker-compose.yml --profile operator run --rm operator python -m pharos.cli delete-person --username ayla
docker compose --env-file deploy/.env -f deploy/docker-compose.yml --profile operator run --rm operator python -m pharos.cli delete-person --username ayla --confirm
docker compose --env-file deploy/.env -f deploy/docker-compose.yml --profile operator run --rm operator python -m pharos.cli prune-audit --confirm
docker compose --env-file deploy/.env -f deploy/docker-compose.yml --profile operator run --rm operator python -m pharos.cli fold-interactions --confirm
```

Nothing runs them by itself. Put the last two in the host's scheduler (for example monthly in
cron) or run them by hand; the first dry run shows what would happen.

| Data | Default | Setting | Floor |
| --- | --- | --- | --- |
| Audit log | 365 days | `PHAROS_AUDIT_RETENTION_DAYS` (or `--days`) | 30 days |
| What people did with the cards | 12 months | `PHAROS_INTERACTIONS_RETENTION_MONTHS` (or `--months`) | 3 months |
| Container logs | 5 files of 10 MB per service | `PHAROS_LOG_MAX_FILE`, `PHAROS_LOG_MAX_SIZE` | none |

- `prune-audit` deletes entries strictly older than the retention and leaves an `audit.pruned`
  entry with the count.
- Nothing runs these commands for you. `python -m pharos.cli retention-status` (read-only, in the
  `api` service) prints how many days ago each last ran with `--confirm`, and `scripts/status.sh`
  warns when that is more than 40 days, or when a job never ran on an installation older than that
  (F-48). Schedule both, for example monthly (the runbook has crontab lines).
- `fold-interactions` first writes monthly counts, then deletes the interactions of the months
  before the cutoff (the first day of the month `--months` back, in UTC), all in one transaction. A
  month and kind with fewer than five people is not kept; one person's minutes count at most 120
  a month and the total is rounded to a multiple of 30 (the daily summary caps each row instead). If a count for a
  month already exists (after a restore, for example) it stops and deletes nothing. The pilot's
  report must not show the monthly counts to anyone who could see the daily summary while those
  months were recent (administrators), and must not put a month next to days of the same month, or
  any two periods that overlap; the application itself never shows the monthly counts, and the
  months that are folded are no longer in the daily summary's 30-day window.
- To find the person, `docker compose ... exec api python -m pharos.cli list-people` prints the
  organization's people with their identifiers (read-only, as the application role, 100 at a time
  with `--limit` and `--offset`); `--user-id` takes the first column.
- `delete-person` removes the person's profile, password, interactions, personal feeds and the
  articles of those feeds; clears their username from the target of earlier audit entries and the
  address hash from their own entries and from refused sign-ins that named them; clears the `created_by` marks on sources and tools; writes
  one `user.deleted` entry with counts and no name; and ends their sessions. It refuses the last
  active administrator. A person who signs in through the identity provider is refused unless
  `--allow-reprovision`: their account is made again, empty, at the next sign-in, so remove or
  disable them at the provider first.

What stays after a deletion, and is yours to handle: the person's identifier in audit entries (it
leads to nobody and goes with the audit retention); backups made before the deletion (they hold the
person until they expire or are destroyed); container logs until they rotate; session records in
Redis if Redis was down (they expire by themselves and cannot be used, the account is gone); and
any counts that never held an identity.

## Load test (`make loadtest`)

`make loadtest` builds a throwaway copy of the stack (own project name, volumes and ports, removed
at the end), fills it with synthetic data (30,000 articles, 300 people; the articles are invented,
six in ten sit in clusters of about three), opens one single-sign-on session for each person and
runs k6 (a digest-pinned container) against the Home feed through Caddy. One virtual user is one
person, kept for the whole run: a visit every 30 seconds or so (spread, and the first visit at a
random moment), the first page of the feed and, one visit in two, the second page. That stays inside
the product's own limit of 300 feed requests an hour for one person; hammering the feed with no
pause would only measure that limit. It prints the numbers **with the hardware** and a verdict on
each target of decision D5 (feed p95 under 400 ms, under 1% failed requests, for the first step).

Settings: `LOAD_STEPS` (people at work, one run of k6 for each; the first is the D5 target; default
`50`, for the stepped run `50,150,300`), `LOAD_DURATION` (default `10m`), `LOAD_THINK` (default
`30`), `LOAD_ARTICLES`, `LOAD_PEOPLE`, `LOADTEST_OUT`. It needs Docker and about 15 minutes, and it
loads the machine it runs on: do not run it on a server people are using.

Results of 2026-11-08, all on one development virtual machine with **2 CPU threads and 8 GB RAM**
that also ran about ten containers of other projects. A pilot server will be larger, so these are
not promises about it; they show how the feed behaves as people are added.

| People at work | Before (ranking on every request) | After (ADR-0027) |
| --- | --- | --- |
| 50, first page p95 (median) | 6,304 ms (2,259 ms) | **134 ms (26 ms), 10 minutes** |
| 50, second page p95 | 9,052 ms | 85 ms |
| 150, first page p95 | 34,589 ms, 5% of requests failed | **235 ms**, none failed |
| 300, first page p95 | 34,899 ms, 44% of requests failed | 2,012 ms, none failed |
| 300, requests a second | 5.0 | 12.7 |

The D5 target (50 people, 10 minutes, 30,000 articles) is **met**: first page p95 134 ms, second
page p95 85 ms, 0 failed of 1,476 requests (the last run, of the code as merged). The stepped run (made before the last review fixes) shows 150 people inside the limit and
300 outside it on this hardware. Two things the numbers hide: the slowest answers (p99 about
1.8 s, at most 3.3 s) are the requests that arrive as a profession's ranking is being made, once in
five minutes for each profession; and above 150 people the limit is the API's own CPU (reading the
kept list and adding the person's part for every request), fixed by F-50 (below).

**After F-50 (2026-11-13), same virtual machine and settings.** Each API process now keeps the
rankings parsed and adds a person's bonuses in whole numbers, and a first page that arrives while
a ranking is being made is answered from the previous period's ranking (ADR-0027 amendment). The
stepped run (3 minutes a step) of `main` just before F-50 and of F-50, the same day:

| People at work | Before F-50 | After F-50 |
| --- | --- | --- |
| 50, first page p95 | 144 ms (no period boundary in the step) | 462 ms (a period boundary in the step) |
| 150, first page p95 (p99) | 3,319 ms (8,826 ms) | **28 ms (70 ms)** |
| 300, first page p95 (p99) | 2,761 ms (6,972 ms) | **157 ms (1,528 ms)** |
| 300, second page p95 | 1,016 ms | 73 ms |

The D5 run (50 people, 10 minutes) after F-50: first page p95 **64 ms**, second page 33 ms, 0
failed of 1,507 requests. What is left is the request that *makes* a profession's ranking at the
start of a period, one for each profession that is asked for (twelve in the catalogue), all at the
same moment: on two threads they take one to five seconds together (the p99 and the maximum). A
three-minute step that holds a period boundary as well as the cold start has about two dozen such
requests in some 460, enough to move its p95: the 50-people step of F-50 above, and 575 ms in a
second run that also held a boundary, while the step of `main` held none.

**After F-61 (2026-11-13), same machine.** The request that would make a ranking is answered from
the previous period's ranking and the new one is made after it, in the background. The same
stepped run, all three steps inside the limits:

| People at work | First page p95 (p99, max) | Period boundary in the step |
| --- | --- | --- |
| 50 | 47 ms (548 ms, 699 ms) | no (the cold start only) |
| 150 | 48 ms (125 ms, 650 ms) | yes |
| 300 | 140 ms (707 ms, 1,683 ms) | yes; after F-50 alone: 157 ms (1,528 ms, 11,132 ms) |

These are single runs on a shared machine, so a few tens of milliseconds either way are noise; the
tail at a boundary is what changed. The cold start (the first request of each profession, with no
previous ranking to stand in) still waits for its ranking.

What it was before: one feed request scored every eligible article of the window (about 20,000)
before it kept twenty, 393 ms alone and about 120,000 buffer reads, and PostgreSQL used both cores
(215%) while the API used 13%. Forcing hash joins gave 373 ms, so it was not the planner. The fix is
ADR-0027 (`Proposed`). A first run, with a harsher profile (a one-second pause, people taken in
turn), also failed and is not the basis of these numbers: it would have met the limit of 300
requests an hour for one person on a fast system.

Until a run reaches 30,000 *people* on comparable hardware, the "30,000 users" figure of the
proposal is an estimate, not a measurement; what is measured is 150 people at work on two threads.

### The insight burst (`LOAD_SCENARIO=insights make loadtest`)

The same harness (same throwaway stack, with the test provider in the worker: no model, no key)
makes a burst: `LOAD_BURST` people (default 100) open "Try this" within 20 seconds, each on another
article, and look again every 3 seconds until the insight is ready. The verdict is PASS when no
answer was a server error or a timeout and every person got a ready insight within five minutes;
the report also checks that the insights are in the database.

Result of 2026-11-08 (the same 2-thread machine): 100 people, **all 100 ready**, median 39 s, p95
157 s, longest 163 s, no 5xx, no 429, 100 insights stored. The pace is the limit of calls to the
model, 30 a minute for the whole organization (`PHAROS_LLM_CALLS_PER_MINUTE`): 100 insights take
about three and a half minutes whatever the machine. Before the fix of F-51 the same kind of burst
left everyone past the limit waiting for five minutes and then asking again (30 of 60 and 30 of 300
people got their insight in five minutes). With the default limit a burst of 300 people is
expected to take ten minutes or more; it was not run.

Two facts for whoever runs a pilot: the limit of 30 calls a minute is shared by every stage (the
filter, the enrichment and the insights), and the **default budget of two dollars a day is too small
for a pilot**: the insights stop when the day's total spending reaches half of it, and at the
default (conservative) prices one insight costs $0.05 to $0.11. Set the real prices of your model and
a budget from the table in the pilot runbook ("How many insights a day"); the Model page then shows
how many more insights the day still covers (F-52).

### The single sign-on burst (`LOAD_SCENARIO=sso make loadtest`)

The same harness, with the development Keycloak of `deploy/docker-compose.sso-dev.yml` in the
throwaway stack. The script makes `LOAD_BURST` (default 100) synthetic Keycloak users through
Keycloak's admin API with one random password for the run, signs one of them in with `curl` as a
control, and then k6 signs every user in the way a browser does: start at Pharos, Keycloak's form,
the callback, `/api/me`. The starts are spread evenly over `LOAD_RAMP` seconds (default 100), all
from the one address of this machine. A person counts only when the callback sets the session cookie
and `/api/me` answers with that person's own name. The verdict is PASS when everybody got in, there
was no 429, no 5xx and no timeout, and the last person was in within 120 seconds of the start.

Result of 2026-11-08 (the same 2-thread machine, Keycloak in `start-dev` mode): 100 of 100 signed
in, no 429, no 5xx, one sign-in from the first request to the person's own name median 146 ms, p95
264 ms, longest 593 ms, the last person in at 99 s (set by the spread, not by the speed), 100 users
created in Pharos (the database count is checked against the burst size). Highest CPU sample (every
30 s): Keycloak about a fifth of a CPU, the API about an eighth.

What this does and does not show: it fits D5's "100 sign-ins in 2 minutes from one address" with a
wide margin, and it shows the per-address limit of the sign-in endpoints (120 a minute for the
start and 120 for the callback, 600 for the whole installation) does not turn this crowd away. At
one sign-in a second the test is gentle; a faster spread (`LOAD_RAMP=30` is 200 a minute from one
address) is expected to meet the limit and be turned away with 429, which the verdict then reports
as FAIL; this was not run. Not covered: Entra ID (real Keycloak and Entra differ in speed and in
the token they send), a Keycloak configured for production, and people behind a corporate proxy
that shares one address (they share the same limit).

## Resilience check (`make test-resilience`)

`make test-resilience` builds a throwaway copy of the stack (like the load test and the stack
tests; it uses the same test-only overlay, in which the worker may reach one fake feed host and
nothing else) and makes two checks of the worker. It takes about 15 minutes, because the second
waits for a lease of five minutes to run out. `RESIL_ONLY=memory` runs the first check alone.

1. **Memory.** A feed of 10,000 entries (1.7 MB; the fetcher refuses an answer over 2 MiB, so a
   bigger feed would test the refusal) is fetched and read, and the worker container's own peak
   memory (its cgroup) is read. The parser stops at 200 entries, and the run ends `ok`. Result of
   2026-11-08: peak **215 MiB** (214 before the feed), limit 300. The peak was the same before the big feed, so it is set
   by the process starting up and not by the feed; the check says "NOT MEASURED" and fails if the
   feed was not read.
2. **A worker killed in the middle of a fetch.** A source whose file takes about six seconds to
   arrive is fetched; two seconds in, the worker is stopped with `docker kill`. Result: the run is
   left open as `running`, nothing is stored from it (the entries and the articles are written in
   one transaction at the end), and after the worker is back and the five-minute lease has run out
   the source is fetched in full: exactly one run ends `ok`, all 120 entries are there once (none
   lost, none doubled), an article was made for each, and a second run adds nothing. Note that
   `docker kill` counts as stopping a container by hand, so Docker's `unless-stopped` policy did
   **not** bring the worker back (the check started it again); a worker that dies by itself (for
   example out of memory) is a different case that this check does not show.

## Starter catalogue check (`scripts/starter-catalogue-check.sh`)

`make check-catalogue` (manual, never in CI) builds a throwaway copy of the stack (own project
name `pharos-catalogue`, volumes, image tag, random passwords, ports 19880 and 19843; no test
overlay, so the worker reaches the real internet through its egress network and `SafeHttpClient`,
as in an installation). It creates the organization and an administrator with a generated
password that is never printed or put on a command line, imports the starter sources, signs in
through the API, switches every source on and waits (`--timeout SECONDS`, default 600) until each
one has been fetched or has failed. It prints one table (name, type, health, articles, last
problem). The language model and personal feeds stay off. Exit code 0 means every source was
fetched well, 1 that one failed or was not fetched in time, 2 wrong usage. It contacts the 25
public sites of the starter catalogue from the host it runs on.

- **Isolation.** Every `PHAROS_*`, `POSTGRES_*` and `COMPOSE_*` variable of the calling shell is
  dropped first, so settings of a real installation that happen to be exported (its image tag,
  tokens, profiles) never reach this stack. Its subnets (172.28.36.0/24 and 172.28.37.0/24 for the
  `llm` network) are used by no other script. A lock (`$XDG_RUNTIME_DIR` or `$HOME`,
  `.pharos-catalogue.lock`) refuses a second run at the same time.
- **Cleanup.** On every normal exit, error or Ctrl-C it removes its containers, volumes, networks,
  the two `:catalogue` images and its temporary directory. It leaves Docker's build cache and the
  base images it pulled. If the process is killed with `SIGKILL` (or the machine stops), nothing is
  removed: run `docker compose -p pharos-catalogue down --volumes` and delete the leftover
  `tmp.*` directory under `$TMPDIR` (it holds the run's throwaway passwords).
- **Exposure.** The two ports listen on all interfaces for the minutes it runs, like the other
  throwaway stacks; the administrator's password is 48 random hex characters and is never shown.

Result of 2026-11-11: **25 of 25 ok** in 524 seconds (115 of them to build and start the stack),
1,185 articles in all; no redirect, block, 404, timeout or parse failure. The sources do not
all finish in the first minute: 16 were done within about a minute of being switched on and the rest over the next six
minutes. Feeds move and start blocking bots over time, so the maintainer runs it before every release,
before a pilot, after any change to `starter_sources.json` and once a quarter, and fixes the list
as described in [the release guide](guides/releasing.md#keeping-the-starter-catalogue-alive)
(F-30).

## Checking a running installation

`/api/readyz` is deliberately not served through the proxy. Ask the API container:

```bash
docker compose -f deploy/docker-compose.yml --env-file deploy/.env exec api \
  python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8000/api/readyz').read().decode())"
```

`make test-compose` builds and checks the whole stack in a throwaway project, with its own
volumes and ports, and removes it afterwards.

## Pinned images and tools

Every image Pharos builds from or starts is pinned by digest (ADR-0024): the `python` and `node`
base images, `caddy`, `pgvector`, `redis`, `ollama`, `keycloak` and the `uv` image the backend
build copies `uv` from. `pnpm` is pinned by the hash in the `packageManager` field of
`frontend/package.json`, which Corepack checks. Two builds of one commit therefore use the same
bases. The images Compose builds itself (`pharos-backend`, `pharos-web`) are the only references
without a digest.

Dependabot opens the update pull requests (`docker` for the two Dockerfiles, `docker-compose` for
`deploy/`); each one changes the tag and the digest together, and a person reviews it like any
other dependency change. To bump one by hand, read the digest of the multi-architecture index:

```bash
docker buildx imagetools inspect python:3.12-slim --format '{{.Manifest.Digest}}'
```

`backend/tests/test_image_pins.py` fails on a floating `FROM` or `image:` line, on a `pip install`
in the backend Dockerfile, on a `uv` version that differs from `UV_VERSION` in
`.github/workflows/ci.yml`, and on a `packageManager` without a hash. A pinned digest also
pins its known vulnerabilities: the scheduled Trivy scan reports them, and a bump is how they
are fixed.

### Tool images

The workflows and `scripts/verify-release.sh` also run two tool images, Trivy and cosign, given as
`TRIVY_IMAGE` and `COSIGN_IMAGE`. Dependabot does not read such variables, so
`.github/images/Dockerfile` lists both images with their tag and digest. That file is never built;
Dependabot's `docker` entry reads it and proposes a new version there. A proposal changes only that
file, so `backend/tests/test_workflow_images.py` fails on purpose and names every copy to update:
`TRIVY_IMAGE` in the security and release workflows, and `COSIGN_IMAGE` in the release workflow and
in `scripts/verify-release.sh`, each with its version comment. Before merging a new cosign image,
verify its own Sigstore signature with `cosign verify` against the signing identity and issuer
that the Sigstore project publishes for its release images (in the cosign release notes and
installation docs; take them from there, not from memory), and write the command and its result
in the pull request: it is the tool that signs and verifies Pharos releases. A new Trivy image
needs the usual review of a dependency change.

## Release images (`.github/workflows/release.yml`)

The release workflow builds the two images (`pharos-backend`, `pharos-web`), scans them with the
same Trivy rules as the security workflow (HIGH and CRITICAL, fixable only; a finding fails the
run) and makes a CycloneDX SBOM for each. It runs in two situations:

- **A pull request that changes `release.yml`** runs the build, the scan and the SBOMs as a dry
  run. It never logs in to a registry and never publishes. The SBOMs are the artifact `sbom` of
  the run.
- **A pushed tag `vX.Y.Z`** does the same and then publishes: the `publish` job pushes the images
  it just scanned (not a second build) to `ghcr.io/<owner>/pharos-backend:<tag>` and
  `ghcr.io/<owner>/pharos-web:<tag>` and prints the digest of each in the run summary. There is no
  `latest` tag; pull by digest. A tag is refused unless it has the form `vX.Y.Z` and its commit is
  in the history of `main`. Before the push, the job checks the SHA-256 of each image file against
  the one recorded when it was scanned, and it stops if the tag already exists in the registry (a
  release is never overwritten). The SBOMs must name the packages we ship (`fastapi`, `next`), so
  an empty scan cannot pass.

After the images are pushed, the `sign` job signs each of them **by digest** with cosign, keylessly:
the signing certificate is issued by Sigstore to the workflow's own GitHub identity, so there is
no key to store or lose. It also attaches the image's SBOM as a signed CycloneDX attestation. cosign
runs as a container pinned by digest (no new action). The job then verifies its own work with
`scripts/verify-release.sh`, the command an operator runs, and proves the check can fail: four more
checks (a wrong identity, a wrong issuer, a wrong repository, a wrong commit) must be refused.

Only the `publish` job (`packages: write`) and the `sign` job (`packages: write` and
`id-token: write`) can write; every other job and the workflow as a whole hold `contents: read`.
`backend/tests/test_release_workflow.py` fails if that changes, if a pull request path gains a
registry login, if an action is not pinned by commit, if a workflow expression is pasted into a
shell script, or if signing is weakened (a stored key, a skipped transparency log, a swallowed
failure, an extra variable handed to cosign).

### Verifying a release

```bash
scripts/verify-release.sh <owner>/<repo> vX.Y.Z <commit-sha>
```

It needs Docker and network access to `ghcr.io` and Sigstore, and, while the packages are private,
a `docker login ghcr.io` with `DOCKER_CONFIG` set to that login's directory. The release notes
state the tag and the commit. For each image the script resolves the digest, then requires that
the signature and the SBOM attestation were made by `release.yml` of that repository, for a push
of that tag, at that commit, and prints the digest. Run the images by that digest, not by the tag.

A passing check **does** mean: both images were signed, and given a CycloneDX attestation, by this
repository's release workflow on a push of that tag, at that commit, and the signature is in the
public transparency log. It does **not** mean: that the commit is on `main` (the workflow checks
that, the signature does not say it), that the SBOM is complete, that the image has no
vulnerability (the scan covers fixable HIGH and CRITICAL on the day of the release), or that the
tag was never moved. Without the commit argument the script says so and does not check which commit
was built; with it, a tag deleted and recreated on another commit fails.

Sigstore's public instance records the repository name, workflow, ref, commit and run in a public,
permanent log. That is fine for a public repository; a private fork must not release through it.

A tag is also refused unless the version in `backend/pyproject.toml` and in
`frontend/package.json` equals the tag's `X.Y.Z` (`scripts/check-release-version.sh`), so a release
cannot be built or published before its version was raised (the tag itself can still be pushed;
the workflow stops at its first step).

### Installing a published release

`deploy/docker-compose.release.yml` replaces the images Compose would build with the published
ones, so nothing is built on the host:

```bash
export PHAROS_BACKEND_IMAGE=ghcr.io/<owner>/pharos-backend@sha256:<digest>
export PHAROS_WEB_IMAGE=ghcr.io/<owner>/pharos-web@sha256:<digest>
docker compose -f deploy/docker-compose.yml -f deploy/docker-compose.release.yml \
  --env-file deploy/.env up -d
```

Take both references from the release notes and check them first, passing the very values you are
about to run:

```bash
scripts/verify-release.sh <owner>/<repo> vX.Y.Z <commit-sha> \
  --backend "$PHAROS_BACKEND_IMAGE" --web "$PHAROS_WEB_IMAGE"
```

The script refuses a reference that is not in the digest form (a tag, a local image, another
registry) and fails if either one differs from the digest it finds for the tag, owner included.
Without the two flags it only verifies the release and says that your references were not compared.
Compose itself only stops if a variable is missing; it does not check the form, so run the check
above first. The overlay needs Docker Compose 2.24.4 or later (`!reset`). Use the same two
files in every later `docker compose` command for this installation (stop, logs, running a command
in a container), or Compose will look for images built on the host. To go back to a previous
release, set the two variables to that release's digests and run `up -d` again; a restore of the
backup (ADR-0022) is a separate step, taken only if the newer version changed the schema.

If the `sign` job fails after `publish` succeeded, the images are already in the registry but
unsigned, and a full re-run stops at the "tag already exists" check. Use "Re-run failed jobs" for
the `sign` job; do not delete the tag or the images.

What the repository must provide, because the workflow cannot enforce it:

- **Who creates tags.** Tags are created by the maintainer only. Restrict `v*` tags with a tag
  ruleset in the repository settings (no create, update or delete except for the maintainer); the
  workflow publishes whatever `v*` tag reaches it, and without a ruleset a deleted and recreated
  tag could point a release at another commit (on 2026-11-09 the repository had no ruleset).
- **Actions policy.** Turn on "Require actions to be pinned to a full-length commit SHA"; today the
  pinning is guarded by the test alone.
- **Package visibility.** A first push creates the packages, and GitHub may make them private. The
  maintainer sets them to public in the package settings once, after the first release.
- **Not yet done.** Nothing in the release workflow, `publish` and `sign` included, has run against
  the real registry and Sigstore yet; that happens at the first tag, which is the maintainer's. The
  steps of a release are in [the releasing guide](guides/releasing.md).
- **cosign upkeep.** Dependabot does not see the cosign image written in the workflow and the
  script; move both together, and when bumping, check the new image's own Sigstore signature.

## Known limits

- Backup and restore are scripts with a rehearsal on a small throwaway database (see "Backup and
  restore"). Nothing alerts on a missed backup, the off-host copy and its encryption are the
  operator's, and a restore on a large database or on the pilot server has not been tried.
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
- `caddy` runs as an ordinary user (uid 65532) with no capability at all and binds 80 and 443
  through a sysctl of its own network namespace (F-08). Its volumes are handed to that user by the
  one-shot `caddy-volumes` service before it starts, on every start; on an installation made with
  an older release this happens on the first `up` after the upgrade, with no manual step. No
  long-running service runs as root; only that helper and `ollama-init` do, for the moment it takes
  to change the owner of a volume.
- Caddy sends `Strict-Transport-Security` (see `PHAROS_HSTS_MAX_AGE`) and refuses request bodies over 1 MiB; the API refuses the same size too. The web application sets a strict Content-Security-Policy with a nonce on every page (ADR-0019); leave it to the application and do not add a second policy in a proxy, since the browser would apply both. `style-src` still allows inline styles.
- Redis and database passwords are environment variables, so a user who can run `docker inspect`
  on the host can read them. Treat access to the Docker socket as root access.
- Container logs are rotated by size (see above), not by days; log shipping and monitoring are not configured.

## Troubleshooting

| Symptom | Cause |
| --- | --- |
| `set PHAROS_APP_PASSWORD` (or another variable) when starting | `deploy/.env` is missing or incomplete: run `make env`. |
| `api` exits right away with "real password" in its log | A password in `deploy/.env` is still `change-me`, or `PHAROS_REDIS_PASSWORD` is empty. |
| "port is already allocated" | Something else uses 80 or 443: set `PHAROS_HTTP_PORT` and `PHAROS_HTTPS_PORT`. |
| `worker` exits with "recreate the volumes" in its log | The database was created before M3 and has no `pharos_worker` role: see "Upgrading to M3". |
| `/api/readyz` shows `worker: fail` shortly after start | The worker has not written its first heartbeat yet; wait a few seconds. |
