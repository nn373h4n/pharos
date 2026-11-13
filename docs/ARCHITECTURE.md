# Pharos architecture

Status: v0 (milestone M1). Describes the intended system; sections marked *planned* are not implemented yet.
Decisions behind it live in [`docs/adr/`](adr/). Where this file and an accepted ADR disagree, the ADR wins.

## 1. Shape of the system

Pharos is a **modular monolith**: one FastAPI backend package (`pharos`), run as two processes (`api` and `worker`), plus PostgreSQL 16 with pgvector, Redis, a Next.js frontend and Caddy as the edge proxy. There are no microservices (ADR-0001).

```mermaid
flowchart TB
  U["User (browser)"]
  subgraph compose["docker compose"]
    C["caddy · TLS, /api → api, / → web"]
    W["web · Next.js · SSR, i18n · holds no tokens"]
    A["api · FastAPI · OIDC BFF, RBAC, feed, admin"]
    P[("postgres 16 + pgvector · RLS, Alembic")]
    R[("redis · Celery broker, cache, rate limits, sessions")]
    K["worker + beat · Celery · connectors → SafeHttpClient → pipeline · LLMGateway"]
  end
  E["OIDC provider"]
  S["External sources"]
  L["LLM providers or local Ollama"]
  U -->|HTTPS| C
  C --> W
  C --> A
  W --> A
  A --> P
  A --> R
  K --> P
  K --> R
  A -. egress .-> E
  K -. egress .-> S
  K -. egress .-> L
```

Dashed arrows are the only traffic that leaves the compose networks (default-deny egress).

- **User requests** never call an LLM. If an insight is not ready, the card shows "being prepared" and a job is queued.
- **Collection** runs on a schedule: beat → worker → connector → pipeline → PostgreSQL.
- **api and worker share the same Python package** and differ only in entry point: `uvicorn --factory pharos.main:create_app` for the API, `celery --app pharos.workers.entrypoint worker` and `... beat` for background work (`make worker`, `make beat` locally).
- **Heartbeat:** beat schedules `pharos.heartbeat` every 60 seconds and a worker writes the current time to Redis (`pharos:worker:heartbeat`); a worker also writes one when it starts. `/readyz` treats the worker as alive while that timestamp is at most 210 seconds old. A fresh timestamp proves beat, broker and worker all work. Tasks are JSON-only, acknowledged late and must be idempotent, because a task is redelivered if its worker dies.
- **Local LLM** (M4-10, [ADR-0020](adr/0020-local-model-profile.md)): with `PHAROS_LLM_PROVIDER=ollama` the worker calls a model server that runs inside Compose (profile `local-llm`) on an internal network of its own. The server has no route to the internet and no published port; only the worker shares its network, and models are put in by a separate one-shot job (`scripts/ollama-pull.sh`). This keeps article text from going to a provider. It does not make the installation "fully local": the worker keeps its `egress` network to fetch sources, which only a host firewall or proxy can narrow (F-04), and no claim of quality is made before the Azerbaijani evaluation has been run on a local model (F-36).

## 2. Backend layout

```text
backend/src/pharos/
├── core/        config, db session, RLS helper, security, logging
├── auth/        OIDC (any provider, Entra preset), sessions, local accounts
├── orgs/ users/ onboarding/
├── sources/     source management, connector registry
├── ingestion/   fetch and normalization tasks
├── content/     items, dedup, embeddings
├── enrichment/  enrichment and insight tasks
├── llm/         LLMGateway, prompts, budget
├── feed/        ranking, cache, cursor pagination
├── admin/ audit/
└── workers/     Celery app, beat schedule
```

Each module has `router.py → service.py → repository.py`, plus `schemas.py` and `models.py`.

- A router holds no SQL and no business logic.
- A service never imports FastAPI.
- A module never touches another module's repository.

These rules are enforced in CI with `import-linter` (*planned*, M1-05).

`connectors/` is a separate Python package (`pharos-connectors`) discovered through entry points; the `SourceConnector` protocol lives in a small SDK package (ADR-0006, *planned* for M3).

## 3. Content pipeline

Six idempotent Celery tasks. The LLM is called in three of them and has no tools: delimited text in, schema-validated JSON out.

1. **Collect** — `connector.fetch(since)`; all HTTP goes through `SafeHttpClient` → `raw_items`.
2. **Normalize** — canonical URL (tracking parameters removed), `url_hash` → `items`.
3. **Cheap filter** (LLM #1) — rules and embedding similarity first; only uncertain items reach a small model.
4. **Cluster** — embedding similarity above a threshold assigns a shared `cluster_id`. Items are grouped, never merged.
5. **Enrich** (LLM #2, strong model) — summary, category, profession relevance, quality score.
6. **Insight** (LLM #3, strong model, lazy) — for `(item, profession, language, prompt_version)`: summary, how to apply, "Try this" prompt, data-safety note.

**How step 1 is scheduled (built in M3-07).** A beat entry runs `pharos.ingest.dispatch` every minute. In one transaction it takes a *lease* (a token and an end time, five minutes) on the sources that are enabled and due, oldest first, at most 20 per run and 40 in flight, using `FOR UPDATE SKIP LOCKED` so two dispatchers never get the same source; the count of sources in flight and the new leases happen under an advisory lock per organization, so two dispatchers at once cannot pass the cap together (F-23). Only after that commit does it enqueue one `pharos.ingest.fetch` task per source carrying two identifiers, the source and the lease token; the address and the configuration are read from the database inside the task. The task (1) checks the lease and reads the source, (2) asks a per-host gate in Redis for permission (one request per host every five seconds, otherwise it gives the source back for a few seconds), starts a run and fetches **with no transaction open**, and (3) writes the result in a short transaction only if the lease is still its own. A task delivered twice, or a worker that died, is harmless: the second task finds the lease gone, and an expired lease makes the source due again. After a failure the wait doubles up to a day and obeys `Retry-After`; a host that says "not now" (`Retry-After`, or a spent rate limit such as GitHub's) also closes its gate for that long, at most a day, so every source on that host and every trial fetch wait too, and a source deferred by a closed gate is given back until it opens (F-27); ten failures in a row mean one try a day until a fetch succeeds; only a person disables a source. A cleanup task (every six hours) deletes fetch runs after 30 days, raw items after 14 and ordinary items after 180, keeps held and hidden items, and marks runs stuck for over an hour as abandoned.

Cost control: insights are generated only for profession/language pairs that have active users and only for items that reach a feed. The AI level is not part of the cache key. A budget check runs in `LLMGateway`: at 80% the admin is warned, at 100% new generation stops while cached insights keep being served.

### Ingestion as built in M3

Steps 1 and 2 of the pipeline above exist in this form (M3, ADR-0006, ADR-0012, ADR-0013). Steps 3 to 6 do not exist yet, and nothing here calls an LLM.

- **Sources.** Rows in `sources`: RSS or Atom feeds, GitHub releases and Hacker News searches, each configuration validated for its type by `pharos.ingest.sourceconfig`. An organization source has no owner; a personal feed has one. Every source has an `enabled` switch. The starter catalogue is imported switched off.
- **Scheduling.** Beat (on the `data` network only) runs `pharos.ingest.dispatch` every minute. It leases due sources and enqueues `pharos.ingest.fetch` with identifiers only (see the scheduling paragraph above).
- **Fetch.** The worker runs the connector. All outbound HTTP goes through `SafeHttpClient` (`pharos.net`): addresses checked at connect time, redirects checked by hand, size and time limits, and the GitHub token sent only to `api.github.com`. Feed bytes go to the parser, which refuses document type declarations and entities before parsing.
- **Pipeline.** Runs in the same transaction as the raw entries: canonical link, exact duplicate check on organization, owner and URL hash, three hold rules that only set flags (a link to a numeric host, an unusual port, and phrases that address a model, in the four languages of the app and on folded text), and links between items and their sources. Personal items are never merged into organization items.
- **Items.** `pharos.items`: members see current articles; administrators and editors hold, approve, hide and restore them. The `/items` page shows feed text as text only.
- **Approved AI tools (M5-07, ADR-0026).** `pharos.aitools` under `/api/ai-tools`: everybody signed in reads a short list (ten at most) of names and https addresses, administrators change it (page `/ai-tools`, audited). The "Try this" panel of a card shows a plain link "Open in {tool}" for each, in a new tab with no referrer and no part of the prompt in the address; the person copies first and pastes there. With no tool, or if the list cannot be read, a card offers "Copy" only. The server never opens an address: the person's browser does, and the address is checked by the service, by the table and again by the browser before a link is made.
- **Personal feeds.** `pharos.feeds` under `/api/me/feeds`: five per person, at least 60 minutes between fetches, visible to the owner only. The address is checked when it is added and again when it is switched on, and `SafeHttpClient` checks it at every fetch. The worker skips the feeds of people who are not active.
- **Trial fetch.** The API queues `pharos.ingest.test_fetch` with identifiers only. The worker fetches the source, writes nothing to the database, and stores a small result under its own Redis prefix (`pharos:worker:testfetch:<test id>`, two minutes). The API reads that result as untrusted data and returns it to the administrator. The API's markers and cooldowns live under its own prefix (ADR-0013).
- **Boundaries.** The worker connects as the `pharos_worker` database role (column grants only) and as a Redis user limited to the Celery queue and `pharos:worker:*` (ADR-0012). Sessions and sign-in state are out of its reach.

## 4. Tenancy

One installation serves one organization, but the code is tenant-aware (ADR-0005).

| Plane | Contents | Rule |
| --- | --- | --- |
| Content (public) | global sources, items, embeddings, insights | `scope='public'`, `owner_org_id` is null |
| Organization-owned content | items from an organization's own sources | `scope='org'`, `owner_org_id` NOT NULL |
| Tenant | users, profiles, interactions, LLM config and usage, audit | `org_id` NOT NULL, RLS |

`org_id` comes only from the authenticated session. Each transaction sets `app.org_id` with `set_config(..., true)`; PostgreSQL row-level security is the second line of defence. The application role does not own the tables and cannot bypass RLS; migrations run with a separate role. See [DATA_MODEL.md](DATA_MODEL.md).

## 5. Authentication

The backend is the BFF (ADR-0003): OIDC Authorization Code with PKCE, `state` and `nonce`, issuer and audience checks, server-side sessions in Redis, and an httpOnly, Secure, SameSite=Lax cookie. Tokens never reach the browser. Entra ID is the first preset; Keycloak is used in dev and E2E tests; local accounts are the fallback. LDAP and SAML are phase 2.

## 6. Frontend

Next.js App Router, TypeScript, Tailwind, shadcn/ui, `next-intl` (ADR-0008). The frontend is presentation only: authentication, authorization and business rules live in the backend, and the browser calls `/api` on the same origin. Locales: `en` (source and fallback), `az`, `tr`, `ru`.

- **Language routing:** every URL starts with the language (`/en`, `/az`, `/tr`, `/ru`). `src/proxy.ts` redirects a path without one: a supported browser language wins, otherwise the installation default applies. The default is `PHAROS_DEFAULT_LOCALE`, read at runtime, so one image serves installations with different defaults.
- **Missing translations:** `scripts/check-i18n.mjs` fails `pnpm build` and `pnpm lint` when a locale lacks a key, has an empty value, has an extra key or uses different placeholders than `en`. At runtime, a gap would still fall back to the English text instead of showing a raw key. `useTranslations` keys are type-checked against `messages/en.json`.
- **Session in the browser (M2):** the session is an HttpOnly cookie the browser cannot read. `SessionProvider` asks `GET /api/me` once and keeps the user and the CSRF token in memory only (never in storage, a cookie or a URL); every state-changing call sends the token as `X-CSRF-Token`. A 401 on any call except sign-in turns the state into "anonymous", and `AppGate` sends the person to `/<locale>/login?next=<page>`; a failed check (backend down) is shown as an error, never as "signed out". `next` is only ever a path on this site (`safeNext`, the backend applies the same rule). Pages in the `(app)` route group are gated in the browser for comfort only: the backend decides on every call. Sign-in and sign-out end with a full page load; signing out does not flip the state first, so the gate cannot start a soft navigation that races the load of the identity provider's sign-out page. `next` also refuses dot segments (`/en/../api/me`). Rule for later work: a server component inside `(app)` must not render tenant data, because Next.js sends the rendered children of the gate to every browser in the page payload whether or not the gate shows them; fetch such data on the client, after the session is known. A strict Content-Security-Policy is still open (F-03).
- **Onboarding (M2):** `/<locale>/onboarding` asks four things in four steps: profession (with an optional department), interests, language and experience with AI. Every step is saved with `PUT /api/me/profile` (only the fields of that step) and the last one calls `POST /api/me/onboarding/complete`; the server decides whether the answers are complete. The page resumes at the first open step, chooses the language names from the server's list of supported locales, and after a language choice reloads in that language and continues. `AppGate` sends anyone whose `onboarding_completed_at` is empty to this page and nowhere else, without looping. Choices arrive as codes; a code this version has no text for is shown as itself instead of failing.
- **Evaluation (M4-09):** `python -m pharos.evaluation` (`backend/src/pharos/evaluation/`) runs candidate models over a corpus of public articles through the product's own gateway and prompts, writes a blind rating sheet per language, imports the raters' marks strictly, and reports per model, profession and language whether it clears the bar (mean 4 of 5, 90% safe and checked, enough articles and raters), recommending the cheapest model that does. It reads no database during a run and writes none; the answers stay in files. The only door that makes public text for it is `pharos.llm.corpus_door.public_text_from_corpus` (it checks its own input, and an import contract lets only the evaluation's runner and `dedup` use it). Guide: `docs/guides/evaluation.md`.
- **Home feed cost (F-49, [ADR-0027](adr/0027-the-home-feed-ranks-once-a-minute-for-everyone-who-shares-a-profession.md), `Proposed`):** the first page is ranked at the start of the current five minutes. The shared part of the score (fit to the profession, recency, quality) is worked out once for each organization, profession and period and kept in Redis for fifteen minutes (the best 2,000 articles); each request adds the person's own part (interest and level bonuses) in the application with exact decimals, reads the cards of its page from the database with a fresh check that each may be shown, and leaves out what the person hid. Past the part the list can prove, or when Redis does not answer, the existing query answers. Each API process keeps the rankings it read parsed (used only while Redis holds the same mark of that making), with each article's shared part floored once, so a request adds whole points for its bonuses and sorts 2,000 whole numbers; a first page that arrives while a ranking is being made is ranked at the previous period's moment from the previous ranking instead of waiting (F-50, ADR-0027 amendment).
- **Near-duplicates (M4-05, [ADR-0007](adr/0007-embedding-model-and-dimension.md), [ADR-0021](adr/0021-near-duplicates-are-clusters-and-the-feed-shows-one.md)):** with `PHAROS_EMBEDDING_PROVIDER` set (`ollama`, `openai`, `azure`, or the test provider), a worker sweep every minute embeds the public, current articles the model has read (class A text only, masked, booked and recorded as stage `embedding`), stores the vector in `item_embeddings` and joins an article to the cluster of its nearest recent neighbour when the cosine similarity reaches `PHAROS_EMBEDDING_SIMILARITY`. Articles are never merged or deleted; the Home feed query shows only the best member of a cluster. The model and the threshold are the operator's choice and are tuned on labelled pairs with `python -m pharos.evaluation dedup`; no model is named in the code.
- **Model layer in the real stack (M4-11):** `scripts/stack-e2e.sh` runs the worker alone with `PHAROS_LLM_PROVIDER=fake` (the stack overlay; `PHAROS_ENVIRONMENT=test` on the worker only) while the API and the scheduler keep a named real provider and no key, as in a real installation: the API never builds a provider. The test provider (`pharos.llm.provider.DemoProvider`) reads the schema the product put into the prompt and returns a valid answer for each task, so the whole path (fetch, filter, enrichment, cards, lazy insight, budget) runs without a model, a key or a cost. It says nothing about quality; that is the evaluation. `frontend/e2e-stack/llm-feed.spec.ts` is the test.
- **Home screen (M4-08):** `/<locale>` shows the cards best for the person (`GET /api/cards`, twenty at a time with a cursor). Each card: title (a link only if `safeHref` accepts the address), host, summary and category as text, and buttons to open **Try this**, save, mark useful, or hide (with undo). Try this asks `GET /api/items/{id}/insight` only when it is opened, waits while the worker makes it, then shows how it applies, a prompt in a read-only field with a Copy button, the data note and the means to report that it was tried and the minutes saved. Everything a model wrote is a React text node or a read-only field's value. The Content-Security-Policy (nonce per request) is set in `src/proxy.ts` from `src/lib/csp.ts`, so pages are rendered per request.
- **Accessibility:** Playwright runs axe (WCAG 2.2 AA) on every language, in light and dark themes, on a desktop and a phone viewport; the skip link and language navigation are tested with the keyboard.
- **Serving:** the production build uses Next's `standalone` output, started with `pnpm start` (`scripts/start-standalone.mjs`) and configured by `PORT`, `HOSTNAME` and `PHAROS_DEFAULT_LOCALE`. The web app answers `/healthz`; the backend answers `/api/healthz` and `/api/readyz`.
- **Hard-coded text** in components is rejected by the `react/jsx-no-literals` lint rule.

## 7. Deployment

One `deploy/docker-compose.yml`, three networks and reserved profiles `dev` (Keycloak), `local-llm` (Ollama, M4-10) and `observability` (OpenTelemetry, Prometheus, Grafana, M5). The profile `local-llm-pull` downloads a model on request. The details, the variables and the known limits are in [DEPLOYMENT.md](DEPLOYMENT.md); the reasoning is in [ADR-0010](adr/0010-compose-network-topology-and-container-hardening.md).

- `edge` carries the published ports and only `caddy`. `app` (caddy, web, api) and `data` (api, worker, beat, migrate, postgres, redis) are Docker `internal: true` networks with no route to the internet; `caddy` and `web` are not on `data`, so a compromised proxy or web app cannot open a connection to the database or Redis. `egress` is joined only by the services that must call out.
- Every container drops all capabilities, forbids privilege escalation and has a read-only root file system. All run as non-root users; Caddy (uid 65532, no capability) binds 80 and 443 through a sysctl of its own network namespace, and only two one-shot helpers that change the owner of a volume run as root (F-08).
- TLS is automatic ACME for public domains, or `tls internal` / a custom certificate for private ones.

| Service | Networks | Internet access |
| --- | --- | --- |
| caddy | edge, app | inbound 80/443; outbound for certificate issuance |
| web | app | none |
| api | app, data, egress | identity provider (restricting destinations is a host firewall task) |
| worker | data, egress | content sources and LLM providers (same) |
| beat | data | none: it only schedules through Redis |
| migrate | data | none (one-shot; applied automatically on first install, by the operator in production) |
| postgres, redis | data | none |

The proxy does not serve `/api/readyz` to the internet and replaces the client's `X-Request-ID`.

Health: `/healthz` (process) and `/readyz` (database, application role, Redis, worker heartbeat, migration version).

## 8. Differences from the planning documents

| Topic | Planning document | In force |
| --- | --- | --- |
| Default UI language | `az` (PLAN, MASTER_PROMPT) | `en`; each installation chooses its own default (ADR-0008) |
| Tooling layout | PLAN §7 lists more agents, `commands/` and different skill names | The `.claude/` package in the repository |
| Network layout | PROPOSAL §9 puts `web` and `api` on `edge` | Only `caddy` is on `edge`, because every container on a normal bridge network can reach the internet; the internal side is split into `app` and `data` (ADR-0010) |
| Metrics for the heartbeat | "visible in metrics" | Written to Redis and shown in `/readyz`; Prometheus metrics come with the `observability` profile |
| Anonymous idea pool | "admins cannot see it" | Not claimed until a design proves it (phase 2) |

## 9. Not covered yet

Retention and deleting a person are operator commands run as the database owner in the on-demand `operator` service (ADR-0025, `Proposed`; see `docs/DEPLOYMENT.md`). Embedding model choice (ADR-0007) and the load-test profile are open; see `docs/ARCHITECTURE_PROPOSAL.md` §16. Backup and restore are scripts (`scripts/backup.sh`, `scripts/restore.sh`, ADR-0022, `Proposed`) with a rehearsal on a throwaway installation; see `docs/DEPLOYMENT.md`.
