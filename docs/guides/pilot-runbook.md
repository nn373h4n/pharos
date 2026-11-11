# Pilot runbook

For the person who installs and runs Pharos for a pilot on one server. It puts the steps in the
order you need them. The detail lives in [DEPLOYMENT.md](../DEPLOYMENT.md); this page links to it
and does not repeat it. Where something has been measured, the number is given; where it has not,
the text says so.

What this release is and is not: read "Not ready in this release" in [CHANGELOG.md](../../CHANGELOG.md)
before you promise anything to anyone. In short, the egress rules for the containers are not built
in (your host firewall is yours to set), and the pilot server, the egress check and the restore
rehearsal on that server are gates only you can pass (G6 to G8 of the
[M5 plan](../plans/m5-pilot-readiness.md)).

## 0. Before you start

You need:

- A Linux server with Docker Engine 26 or newer and Compose v2.24.4 or newer. Free ports 80 and 443
  (or other ports in `deploy/.env`). A host name that points at it, and a certificate plan:
  `PHAROS_TLS` takes an e-mail address (automatic certificate for a public name), your own
  certificate and key, or `internal` (a private CA, for tests only).
- An identity provider for single sign-on (Microsoft Entra ID, Keycloak or any OpenID Connect
  provider): [Entra](sso-entra.md), [Keycloak](sso-keycloak.md), [generic](sso-generic.md). You can
  start with password sign-in for administrators and add it later.
- If you want summaries and "Try this": a model provider key and a daily money cap you accept. The
  product works without a model; start without one.
- A place off the server for backups, and a way to encrypt what you put there.

Do not put the name of your organization, people, addresses or tenant identifiers into the
repository, an issue or a pull request. They belong in `deploy/.env` and the admin pages only.

## 1. Install

A published release is run by digest, so nothing is built on the server and what you run is what
was signed ([DEPLOYMENT.md, "Installing a published release"](../DEPLOYMENT.md#installing-a-published-release)).

```bash
git clone https://github.com/<owner>/<repo>.git pharos && cd pharos
git checkout vX.Y.Z          # the release you are installing: the scripts and Compose files come from it
make env                      # creates deploy/.env with generated passwords (mode 600)
```

Open the release notes of that tag and take the commit and the two image references from them. Put
the two references into `deploy/.env` (the lines are in the file, commented out):

```text
PHAROS_BACKEND_IMAGE=ghcr.io/<owner>/pharos-backend@sha256:<digest>
PHAROS_WEB_IMAGE=ghcr.io/<owner>/pharos-web@sha256:<digest>
```

Check them before you run anything. The script refuses any reference that is not a digest and fails
if either one differs from the release's:

```bash
scripts/verify-release.sh <owner>/<repo> vX.Y.Z <commit-from-the-notes> \
  --backend "$(sed -n 's/^PHAROS_BACKEND_IMAGE=//p' deploy/.env)" \
  --web "$(sed -n 's/^PHAROS_WEB_IMAGE=//p' deploy/.env)"
```

If it fails, stop. A passing check says the images were signed by this repository's release
workflow for this tag and commit; it does not say the commit is on `main`, that the SBOM is
complete, or that the images have no vulnerability.

Edit `deploy/.env` (every variable is described in [DEPLOYMENT.md, "Configuration"](../DEPLOYMENT.md#configuration-deployenv)).
For the pilot, set these on purpose:

| Variable | Pilot value | Why |
| --- | --- | --- |
| `PHAROS_DOMAIN`, `PHAROS_PUBLIC_URL`, `PHAROS_TLS` | your host name, `https://...`, your certificate plan | redirects and the CSRF check use the public URL; production needs https |
| `PHAROS_ORG_SLUG` | one short name | must equal the slug you create in step 2 |
| `PHAROS_LOCAL_LOGIN` | `break-glass` | administrators can always sign in with a password, even when the identity provider is down; nobody else can |
| `PHAROS_PERSONAL_FEEDS` | `off` | people's own feeds fetch arbitrary addresses; leave them off until the egress check has passed on this host (gate G7) |
| `PHAROS_EMBEDDING_PROVIDER` | `off` | near-duplicate vectors and the local model stay off unless you have tuned them |
| `PHAROS_LLM_PROVIDER` | `off` first | switch the model on in step 4, once everything else works |
| `PHAROS_OIDC_*` or the Entra preset | your provider | see the SSO guides |

Leave `PHAROS_ENVIRONMENT` unset: production refuses placeholder or short secrets, and serves no
API documentation.

Start it, then create the organization and the first administrator:

```bash
docker compose -f deploy/docker-compose.yml -f deploy/docker-compose.release.yml \
  --env-file deploy/.env up -d
```

To avoid typing the two files every time, put this line in your shell profile and use `pharos` for
every Compose command below; use the same two files in **every** later command for this
installation, or Compose looks for images built on the host:

```bash
pharos() { docker compose -f deploy/docker-compose.yml -f deploy/docker-compose.release.yml --env-file deploy/.env "$@"; }
export PHAROS_COMPOSE="docker compose -f deploy/docker-compose.yml -f deploy/docker-compose.release.yml --env-file deploy/.env"
```

(`PHAROS_COMPOSE` is read by `scripts/backup.sh`, `scripts/restore.sh` and `scripts/status.sh`.)

```bash
pharos run --rm migrate python -m pharos.cli create-org --slug default --name "Your organization"
pharos exec api python -m pharos.cli create-admin --username admin   # asks for a password, 15 characters or more
```

Until the organization exists `/api/readyz` reports `organization: fail`. Then look at the whole
installation:

```bash
scripts/status.sh
```

Every line should say PASS. It checks the services, the API's own readiness report and the disk;
it does not check that sources fetch or that a model answers (steps 3 and 4).

## 2. First sign-in

- **Administrators, by password (break-glass):** open `https://<your host>`, use the password form
  with the account from `create-admin`. After repeated wrong passwords an account waits longer; clear
  it with `pharos exec api python -m pharos.cli unlock --username admin`. A forgotten password is
  changed with `set-password`.
- **Everyone else, by single sign-on:** configure `PHAROS_OIDC_*` (or the Entra preset), run
  `pharos up -d` to apply it, and sign in with a test account first. Test that a person from
  the identity provider gets an account at the first sign-in, and that you can still get in by
  password as an administrator. The guides list what to check at the provider.
- Make one more administrator you trust now, so one lost password does not lock you out.

## 3. Sources

```bash
pharos exec api python -m pharos.cli import-sources --dry-run
pharos exec api python -m pharos.cli import-sources
```

This adds 25 public sources, all **switched off**. Open the Sources page as an administrator, read
the list, and switch on the ones you want. Each row says whether it is working, failing or not
fetched yet, the last success and the last problem. The first fetches take a few minutes. Gate G10
asks for at least 20 cards for each pilot profession: if a profession has fewer, add or switch on
more sources before you invite anyone.

Sources are public addresses; Pharos fetches them through its guarded HTTP client, not directly.
That is a rule inside the application; it is not a firewall (see "Known limits" in DEPLOYMENT.md).

## 4. The model: key and budget

Skip this step to run without a model; people still get the cards.

1. In `deploy/.env` set `PHAROS_LLM_PROVIDER` (`anthropic`, `openai` or `azure`), `PHAROS_LLM_API_KEY`,
   the three model names (`PHAROS_LLM_MODEL_FILTER`, `..._ENRICHMENT`, `..._INSIGHT`) and, **before
   the first call**, the real prices of your models (`PHAROS_LLM_PRICE_IN_MICRO_USD` and the one
   for output). The defaults are high on purpose: with them the cap is reached early.
2. `PHAROS_LLM_DAILY_BUDGET_MICRO_USD` is the cap in millionths of a US dollar (`2000000` is two
   dollars a day). With the default settings the cap covers roughly 62 insights a day (measured
   while building M5; your prices change the number).
3. `pharos up -d` recreates the services whose settings changed.
4. Open the Model page as an administrator and press **Test the key**. Only the worker holds the
   key; the answer says whether the key works, was refused, or the provider did not answer.

Only public article text goes to a cloud model: the title, an excerpt of at most 500 characters,
the host of the link and the language ([ADR-0015](../adr/0015-what-may-reach-a-model.md)). Nothing a
person typed, no name, e-mail address, department or personal-feed article is sent. The gateway also
masks e-mail addresses, phone numbers and similar patterns as a second line of defence.

**Raising the budget:** change the number in `deploy/.env`, run `pharos up -d`, and look at the
Model page: "Today's spending" shows the new cap. When the cap is reached, new summaries stop until
the next day and the ones that exist keep showing. The page warns at eighty percent.

## 5. Approved AI tools

The AI tools page (administrators) holds the list of AI tools people may open an insight in. It is
**empty by default**: a card then offers only "Copy". "Open in" appears only for a tool you list,
and only as an https link. Which tools to list is your decision (gate G9); list none for a first
week if in doubt. Every change is audited.

## 6. Invite people

Send the participant briefing first ([pilot-briefing-template.md](pilot-briefing-template.md)):
what Pharos stores, what it sends to a model, and what people must not paste into any AI tool
(risk R1 of the plan is a behaviour risk; the briefing reduces it and cannot remove it). Gate G11
is that they were briefed.

## 7. Every day

```bash
scripts/status.sh --backups /srv/pharos-backups/dumps
```

PASS, WARN, FAIL or NOT CHECKED per line; exit status 1 if anything fails. In the admin pages:

- **Sources:** rows marked Failing, and how many failed in a row.
- **Model:** today's spending against the cap, the key check, and the last seven days of calls.
- **Backups:** the newest dump is from the last night. Nothing alerts on a missed backup, so this
  line is the alert.

Container logs rotate by size (five files of 10 MB for each service by default). Read them with
`pharos logs --tail 100 <service>`. Pharos is built not to log prompts, answers or secrets and the real stack's logs were searched
for them in M5-12; if you ever see one in a log, treat it as an incident and tell the maintainers.

## 8. Backups and retention (set up once, then watch)

[DEPLOYMENT.md, "Backup and restore"](../DEPLOYMENT.md#backup-and-restore) has the detail. Minimum
for the pilot:

```bash
scripts/backup.sh run --dir /srv/pharos-backups/dumps
scripts/backup.sh env --dir /home/operator/pharos-env --dumps /srv/pharos-backups/dumps
```

- Schedule `run` nightly with the host's scheduler. Copy the dumps off the server, encrypted. Keep
  the `.env` copy somewhere else than the dumps: without the root secret in it, a restored audit
  chain and the tokens cannot be read.
- **Restore once on a spare machine before you rely on it** (gate G8). A dump that has never been
  restored is not a backup.
- Schedule the two retention commands, for example monthly, after reading their dry runs
  ([DEPLOYMENT.md, "Retention and deleting a person"](../DEPLOYMENT.md#retention-and-deleting-a-person)):

```bash
pharos --profile operator run --rm operator python -m pharos.cli prune-audit --confirm
pharos --profile operator run --rm operator python -m pharos.cli fold-interactions --confirm
```

- Deleting one person: run `delete-person --username NAME` (a dry run), then again with
  `--confirm`. Backups made before the deletion still hold the person until they expire.

## 9. When something goes wrong

| Situation | Do this |
| --- | --- |
| Model misbehaves, costs too much, or the provider is down | In `deploy/.env` set `PHAROS_LLM_PROVIDER=off`, then `pharos up -d`. Existing summaries keep showing; no new model call is made. |
| A personal feed is abused | Set `PHAROS_PERSONAL_FEEDS=off`, then `pharos up -d`. The feature answers 404 and the worker stops fetching those feeds; the feeds are kept. |
| Identity provider is down | Administrators sign in with a password (`break-glass`). Others wait. |
| `status.sh` says a service is not healthy | `pharos ps -a`, then `pharos logs --tail 100 <service>`. [DEPLOYMENT.md, "Troubleshooting"](../DEPLOYMENT.md#troubleshooting) lists the common causes. |
| `readyz` fails on `worker` just after start | Wait a few seconds for the first heartbeat. |
| A secret may have leaked | Replace it in `deploy/.env` (for the model key, also revoke it at the provider), `pharos up -d`. Changing `PHAROS_SECRET_KEY` signs everyone out and makes old audit entries unverifiable: ask the maintainers first. |
| The database is lost or damaged | Restore ([DEPLOYMENT.md, "Backup and restore"](../DEPLOYMENT.md#backup-and-restore)). People sign in again; sessions are not backed up. |

## 10. Upgrade and roll back

1. Read the new release's notes and the upgrade notes in `CHANGELOG.md`. Take a backup (step 8)
   and copy it off the server.
2. `git fetch --tags && git checkout vX.Y.Z`, put the new release's two digests into `deploy/.env`,
   and run `scripts/verify-release.sh` as in step 1. Do not continue if it fails.
3. `pharos up -d`. The database is migrated when the API starts (`PHAROS_MIGRATE_ON_START=true`).
4. `scripts/status.sh --backups ...` until every line passes.

**Roll back:** put the previous release's digests back in `deploy/.env` and run `pharos up -d`.
If the newer version changed the database schema, the older code does not run on it: restore the
backup taken in step 1 of the upgrade (people lose what they did after that backup).

## 11. End of the pilot

Decide first, with the people who own the data, whether anything is kept. Then, in this order:

1. Take a last backup if you need one, and say who owns it and when it is destroyed.
2. Run `pharos down`. Data stays in the Docker volumes until you remove them. Removing the volumes
   (`pharos down -v`) **destroys the database**: do it only when the decision above says so.
3. Delete the off-server dumps and the `.env` copy, or encrypt and archive them under that decision.
4. At the identity provider, delete the Pharos application registration and its secret. Revoke the
   model key at the provider. Remove the host name from DNS.
5. Tell participants what happened to their data, using the briefing's wording.

## Production checklist

Tick each line yourself and write PASS, FAIL or NOT RUN next to it. A line nobody ran is NOT RUN,
never PASS.

- [ ] `scripts/verify-release.sh` passed for the exact digests in `deploy/.env`.
- [ ] `deploy/.env` is mode 600, not in git, and a copy is kept apart from the dumps.
- [ ] `PHAROS_ENVIRONMENT` unset (production); valid TLS; `PHAROS_PUBLIC_URL` is https.
- [ ] `PHAROS_LOCAL_LOGIN=break-glass`; single sign-on tested with a test account; two administrators.
- [ ] `PHAROS_PERSONAL_FEEDS=off` unless gate G7 has passed on this host.
- [ ] Embeddings off; model off, or key tested, real prices set, cap accepted.
- [ ] Approved tools list reviewed (empty is fine).
- [ ] At least 20 cards for each pilot profession (G10).
- [ ] Backups scheduled; off-server encrypted copy; one restore rehearsed on a spare machine (G8).
- [ ] Retention commands scheduled; dry runs read.
- [ ] Host firewall allows only what you intend; the egress rules and `scripts/egress-check.sh`
      are **not part of this release** (M5-03), so write what you did here: ______ (G7).
- [ ] `scripts/status.sh --backups ...` all PASS.
- [ ] Participants briefed (G11).
