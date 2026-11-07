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
again after a restore) or `deploy/.env`. Keep `deploy/.env` separately: its root secret is needed to
read the restored audit chain and tokens, and it must not be stored beside the dumps.

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
never commit a dump or the root secret; `.gitignore` is a second line. Not checked: links or open
permissions further up the path, and POSIX ACLs on the directory. The files are private (modes 700 and 600). Keep
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

After the restore, `restore.sh` looks for exactly two things a good dump never makes: settings on
the database or on a role, and schemas other than `public`. **That is all it looks for.** It does
not compare policies, grants, default privileges, row-level security or functions with what Pharos
ships; a hostile dump can pass it. If the origin of a dump is in doubt, do not restore it, or
compare the restored catalogs with a database made by the migrations before you start the stack.

**Restoring**, on a new installation, before anything else is started:

1. Put `deploy/.env` back (the same root secret).
2. Start only the database on a new volume: `docker compose -f deploy/docker-compose.yml --env-file deploy/.env up -d postgres`. The init script creates the roles and the `vector` extension.
3. `scripts/restore.sh /srv/pharos-backups/dumps/daily/pharos-YYYYMMDDTHHMMSSZ.dump` (a relative
   path is read from where you run it). It refuses a dump whose checksum file is missing or does not
   match, a file that is not a Pharos dump, a database whose two roles do not both exist, are not
   plain login roles or are members of other roles, and a database that already has tables, views
   or sequences; it restores in one transaction, so a failure leaves nothing half-restored. The
   script first copies the dump once into a private directory (`PHAROS_RESTORE_TMP`, else `TMPDIR`,
   else `/tmp`, which is memory on many hosts): for a large dump name a directory on a real disk with
   room for it. The free space is checked before the copy, and the copy is removed on every way out.
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
- `fold-interactions` first writes monthly counts, then deletes the interactions of the months
  before the cutoff (the first day of the month `--months` back, in UTC), all in one transaction. A
  month and kind with fewer than five people is not kept; one person's minutes count at most 120
  a month and the total is rounded to a multiple of 30 (the daily summary caps each row instead). If a count for a
  month already exists (after a restore, for example) it stops and deletes nothing. A monthly count
  next to the daily summary can let someone work out a smaller group by subtraction; the pilot's
  report should publish one or the other.
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

## Checking a running installation

`/api/readyz` is deliberately not served through the proxy. Ask the API container:

```bash
docker compose -f deploy/docker-compose.yml --env-file deploy/.env exec api \
  python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8000/api/readyz').read().decode())"
```

`make test-compose` builds and checks the whole stack in a throwaway project, with its own
volumes and ports, and removes it afterwards.

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
- `caddy` is the only process that runs as root inside its container, with all capabilities
  dropped except `NET_BIND_SERVICE`. A container escape would therefore give root on the host.
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
