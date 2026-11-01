# Data model

Status: milestone M2. Migrations 0001 (`organizations`, the RLS helper) and 0002 (identity, profiles, audit log) exist; the other tables are the target model and arrive with their milestones. Rows marked **Built** below exist. Tenancy rules: [ADR-0005](adr/0005-tenancy-and-rls.md).

## Conventions

- Primary keys are UUIDs. Every table has `created_at` and `updated_at`.
- Tenant-plane tables have `org_id NOT NULL` and a row-level security policy.
- Content-plane tables carry `scope` (`public` or `org`) and `owner_org_id`. A CHECK constraint requires `owner_org_id IS NOT NULL` when `scope = 'org'` and `IS NULL` when `scope = 'public'`.
- Secrets are stored encrypted (AES-256-GCM with a master key from the environment, `key_version` for rotation). Plaintext keys are never stored or logged.
- Schema changes go through Alembic. Migrations run with a role that is separate from the application role.

## Row-level security

Each transaction starts with `set_config('app.org_id', <org from session>, true)` (equivalent to `SET LOCAL`), done by `tenant_session()` in `pharos.core.rls`. Policies compare `org_id` to the SQL function `pharos_current_org()`, which returns `NULLIF(pg_catalog.current_setting('app.org_id', true), '')::pg_catalog.uuid`. The wrapper matters: after a scoped transaction ends, the setting stays defined as an empty string on that connection, and a bare `::uuid` cast of it would raise an error instead of matching no rows.

Content tables get separate policies per command (`content_policy_sql()`):

- **Read:** `scope = 'public' OR owner_org_id = pharos_current_org()`.
- **Insert, update, delete:** only rows with `owner_org_id = pharos_current_org()`. No tenant can create, change, hide or delete the public rows every tenant shares. Public rows are written by the table owner or by a dedicated ingestion role passed as `public_writer_role` (added with the connectors in M3).

Tenant tables get one policy for all commands (`tenant_policy_sql()`). Background jobs set the same parameter before touching tenant data. Cache keys include the organization wherever data is tenant-specific.

**What this protects, and what it does not.** RLS stops queries that forget a `WHERE org_id = ...` filter, and a tenant-scoped session cannot see or change another organization's rows. It does not stop code that can run arbitrary SQL as the application role: that code can call `set_config('app.org_id', ...)` and switch organization. A pooled connection is reset (`RESET app.org_id`) when it is returned, so a switch cannot leak to the next request, but preventing arbitrary SQL (parameterized queries only, no SQL built from user input or LLM output) stays a code-level rule. `/readyz` checks that the application role is not a superuser, has no `BYPASSRLS` and owns no table.

## Database roles

Three login roles are provisioned by the deployment before the first migration. The Docker Compose stack does it with `deploy/postgres/init/01-roles.sh` on the first start of an empty data volume. A migration refuses to run if `pharos_app` is missing, is a superuser or has `BYPASSRLS`, or if it is run as `pharos_app`.

| Role | Used by | Rights |
| --- | --- | --- |
| `pharos_owner` | Alembic migrations only; owns the schema objects | DDL; its credentials are not part of the API/worker settings |
| `pharos_app` | API | `NOSUPERUSER NOBYPASSRLS`, no ownership; only the grants a migration gives it, by column where it matters (for example `organizations`: `SELECT`, and `UPDATE` on `name`, `settings`, `updated_at`). `tests/integration/test_identity_schema.py` compares the complete list of its privileges with an expected set, so a grant added by mistake fails the build |
| `pharos_worker` | Celery worker and scheduler | `NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE`, no ownership, and **no privilege on any table** until a migration grants it column-level access to the ingestion tables (M3-05). It cannot read people (except the state of an account: `users.org_id`, `id`, `status`, migration 0004, ADR-0014), credentials, profiles or the audit log. `tests/integration/test_worker_role.py` fails if it holds a privilege that is not on its allow-list; the worker refuses to start if it is connected as another role or as a widened one (ADR-0012) |

```sql
CREATE ROLE pharos_owner LOGIN PASSWORD '<generated>';
CREATE ROLE pharos_app LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE PASSWORD '<generated>';
CREATE DATABASE pharos OWNER pharos_owner;
GRANT CONNECT ON DATABASE pharos TO pharos_app;
CREATE ROLE pharos_worker LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE PASSWORD '<generated>';
GRANT CONNECT ON DATABASE pharos TO pharos_worker;
```

Creating an organization is an operator task done as `pharos_owner`; the application cannot create or delete organizations or change an organization's `id`, `slug` or `created_at`.

## Cross-organization keys

A foreign key check ignores row-level security, so a plain reference could point into another organization. Tables that reference each other therefore carry the organization in a composite key: `users (org_id, department_id)` references `departments (org_id, id)`, and `local_credentials` and `user_profiles` reference `users (org_id, id)`. The profession of a profile is checked by a trigger (global or the profile's own organization), which also protects against the table owner, who is not subject to RLS.

## Organization lookup before sign-in

At sign-in the application does not know the organization yet, and RLS hides every row until `app.org_id` is set. The function `pharos_org_id_for_slug(text)` returns the id for one slug and nothing else. It is `SECURITY DEFINER` with a fixed `search_path`, `EXECUTE` is revoked from `PUBLIC` and granted to `pharos_app` only (a view would bypass RLS for every column and organization). See [ADR-0011](adr/0011-sessions-csrf-idp-trust-and-organization-resolution.md).

## Tables

### Tenant plane

| Table | Main columns | Notes |
| --- | --- | --- |
| `organizations` | id, name, slug, settings (jsonb: branding, locale, policies) | Usually one row per installation |
| `users` | id, org_id, issuer, subject, email, display_name, role, status, department_id, department_source, locale, last_login_at | **Built (0002).** Identity is `(org_id, issuer, subject)`; local accounts use the issuer `local`. E-mail is information and is never used to find or link an account. Role: `admin`, `editor`, `member`. Status: `active`, `disabled`, `deprovisioned`. The application cannot change `org_id`, `issuer` or `subject` or delete a user |
| `local_credentials` | user_id, org_id, password_hash, password_changed_at | **Built (0002).** Argon2id hash only; failed-attempt counters live in Redis. The application cannot delete rows |
| `departments` | id, org_id, name, normalized_name | **Built (0002).** `normalized_name` is generated (trim, Unicode NFC, lower case) and unique per organization, so "Café" and "CAFÉ " are one department |
| `user_profiles` | user_id, org_id, profession_id, ai_level, interests, onboarding_started_at, onboarding_completed_at | **Built (0002).** `ai_level`: `beginner`, `intermediate`, `advanced`; `interests`: up to 12 fixed codes; a trigger accepts only a global or own-organization profession |
| `user_sources` | id, org_id, user_id, source_id | User-added sources are checked by `SafeHttpClient` |
| `interactions` | org_id, user_id, item_id, type, value, created_at | Types: view, save, useful, not_useful, tried, time_saved |
| `llm_provider_configs` | org_id, provider, stage_model_map, api_key_encrypted, key_version | |
| `llm_usage` | org_id, stage, provider, model, tokens_in, tokens_out, cost_estimate, created_at | No content stored |
| `audit_logs` | id, org_id, occurred_at, actor_type, actor_id, action, target, outcome, ip_hash, details | **Built (0002).** Append-only for the application (INSERT and SELECT only). `actor_type`: `user`, `local_admin`, `operator_cli`, `system`. `ip_hash` is an HMAC. No secrets or content; `details` is a small JSON object. `actor_id` is not a foreign key, so the history survives a deleted user |

### Content plane

| Table | Main columns | Notes |
| --- | --- | --- |
| `professions` | id, scope, owner_org_id, code, names (jsonb: en/az/tr/ru) | **Built (0002).** Global rows are seeded by the migration (about a dozen generic professions); the application role can only read them. Organization-owned rows (`scope = 'org'`) arrive with the admin panel |
| `sources` | id, org_id, owner_user_id, type, name, config, dedupe_key, enabled, interval_seconds, next_fetch_at, lease_until, lease_token, etag, last_modified, consecutive_failures, last_error_code, last_fetched_at, last_success_at, created_by | **Built (0003), organization-scoped (ADR-0012).** `type`: `rss`, `github`, `hackernews`; `config` is a small JSON object (8 KiB at most), validated per type by the application. `owner_user_id` is set for a personal feed. One source per `(org_id, owner_user_id, dedupe_key)`. A lease (`lease_until` with `lease_token`, both or neither) says a fetch is running and expires on its own. Nothing secret is stored here: the GitHub token lives in the worker's environment |
| `fetch_runs` | id, org_id, source_id, started_at, finished_at, outcome, error_code, http_status, duration_ms, bytes_fetched, items_seen, items_new | **Built (0003).** Bounded history for the admin screen; `outcome` is one of a fixed list, `error_code` a short lower-case code (never a URL or a message). Proposed retention 30 days |
| `raw_items` | id, org_id, source_id, external_id, title, excerpt, url, published_at, fetched_at, content_sha256, processed_at | **Built (0003).** What a source said after cleaning and truncation (title 300, excerpt 500 characters, link `http(s)` only), unique per `(org_id, source_id, external_id)`. Not visible to the application role. Proposed retention 14 days |
| `items` | id, org_id, owner_user_id, canonical_url, url_hash, title, excerpt, lang, published_at, fetched_at, content_sha256, status, hold_reason, summary, category, quality, enriched_at, enrichment_version | **Built (0003, enrichment columns 0006).** One row per article: unique on `(org_id, owner_user_id, url_hash)` with NULL owners counted as equal, so an organization item exists once and a personal item is never merged into it. Title, short excerpt and link only; `status`: `new`, `held`, `hidden` (`hold_reason` set exactly when held). Proposed retention 180 days unless held. A later fetch can refresh the text but never overwrites a person's decision |
| `item_professions` | org_id, item_id, profession_code, relevance (0-100) | **Built (0006).** How well an article fits each profession, as the model said it (codes the catalogue has). Primary key (item, code); removed with its item. The worker adds and removes rows; the application reads them |
| `item_insights` | org_id, item_id, profession_code, language, prompt_version, status (`ok`/`rejected`), summary, how_to_apply (jsonb, 1-3 steps), try_prompt, safety_note, created_at | **Built (0007).** The cache of ADR-0015: one row per (article, profession, language, prompt version); the person's level and interests are not in the key. `rejected` rows carry no text and are remembered for a day. The worker adds rows and may delete only a rejected row a day old (policy); the application reads. Removed with its article |
| `item_sources` | org_id, item_id, source_id, first_seen_at | **Built (0003).** One item seen in several sources. A trigger requires the item and the source to have the same owner |
| `item_embeddings` | item_id, model, dim, embedding (vector) | Recomputed when the model changes ([ADR-0007](adr/0007-embedding-model-and-dimension.md)) |
| `item_professions` | item_id, profession_id, relevance | Enrichment output |
| `item_insights` | item_id, profession_id, language, prompt_version, model, summary, how_to_apply, try_prompt, safety_note, status | Unique on (item, profession, language, prompt_version) |

### Phase 2 (not in MVP)

`ideas`, `idea_votes`, `success_stories`, `time_savings`, `badges`, `user_badges`, `challenges`. Company-internal content never goes to a cloud LLM by default; when an admin enables it, masking is applied and the choice is audited.

## Model usage (0005)

- `llm_usage` (M4-02, ADR-0016): one row per model call or refusal: `org_id`, `stage` (`filter`, `enrichment`, `insight`, `key_test`), `provider`, `model`, `tokens_in`, `tokens_out`, `cost_micro_usd` (millionths of a US dollar), `outcome` (`ok`, `rejected`, `failed`, `refused`), `created_at`. Numbers and words from fixed lists only: never a prompt, an answer, an article, a person or a key. Under the ordinary tenant policy. `pharos_worker` may only add rows, by column (it cannot read, change or delete them, and cannot set `id` or `created_at`); `pharos_app` may read them for the budget page and change nothing. Both are pinned by `test_worker_role.py`.
- The day's running total is not in the database but in Redis under `pharos:worker:llm:` (a counter per organization and day, atomic reserve and settle); if Redis does not answer no call is made.

## Ingestion tables (0003)

- **Tenant isolation.** All five tables carry `org_id` and the ordinary tenant policy; there are no `public` rows and no role that writes across organizations. Foreign keys include `org_id`, so a row cannot point into another organization.
- **Personal feeds.** A source or item with `owner_user_id` is hidden from everybody but that person: for `pharos_app` a restrictive policy compares it with `pharos_current_user()` (the transaction setting `app.user_id`, unset means nobody), so an administrator listing sources does not see other people's feeds, and a feed's runs and links disappear with it. The API must set `app.user_id` for the signed-in person (M3-10); until it does, personal rows are simply invisible. `pharos_worker` is not subject to this policy because it processes every feed.
- **Privileges** (column level, tested in `test_ingestion_schema.py` and `test_worker_role.py`):
  - `pharos_app`: read `sources`, `fetch_runs`, `items`, `item_sources`; create, edit and delete sources (configuration only: not the fetch state and not `next_fetch_at`, so "fetch now" has to go through a worker task and a person cannot make the worker fetch their URL as often as they like); change `items.status` and `hold_reason` only; read `item_professions`; no access to `raw_items`. `owner_user_id` and `created_by` can never be changed after creation.
  - `pharos_worker`: read everything here except `sources.created_by`; update the fetch state of a source (lease, validators, failures, next time) but not its configuration; create and finish fetch runs; create raw items and mark them processed; create items and refresh their text but never change `status` or `hold_reason` of an existing one; create item links; delete old runs, raw items and items for retention. It can create no source and delete none.
  - Migration 0004 (F-32, ADR-0014): `pharos_worker` also holds `SELECT (org_id, id, status)` on `users`, so it can skip the personal feeds of a person who is not `active`. Nothing else about people is readable or writable for it; the exact columns are pinned by `test_worker_role.py`.
  - Both are pinned by exact-set tests, so a grant added later fails the build.
- **Personal feeds API (M3-13, no migration).** `pharos.feeds` uses what is here: `owner_user_id` and `created_by` are the person, set from the session; a statement names the person (`owner_user_id = :user`) next to the restrictive policy and sets `app.user_id` for the transaction; the type is always `rss`; deleting a feed cascades to its runs, raw entries and links but leaves its items (the application role cannot delete items), which nobody can reach and retention removes.
- **Limits enforced by the database.** A personal feed is fetched at most hourly (`sources_personal_interval_check`) and a person has at most five (a trigger with an advisory lock, so parallel requests cannot pass it); `created_by` is NULL or the person making the request; links must be `http(s)` without white space, control characters, quotes or angle brackets.
- **The worker cannot undo a decision.** Restrictive policies stop it from deleting a `hidden` item and from creating an item that is already `hidden`, so a rejected article cannot be brought back by a later fetch. A compromised worker can still create items (including personal ones) and delete the others; ADR-0012 treats that as the cost of having a worker at all.
- **Downgrading 0003** deletes all sources, items and fetch history, and refuses unless `PHAROS_ALLOW_DATA_LOSS=yes` is set.

## Known limits of the identity schema (0002)

- **Case folding depends on the database locale.** `departments.normalized_name` uses `lower()`, which follows the database's `LC_CTYPE`. The migration refuses the `C` locale, but under `en_US.utf8` Turkish and Azerbaijani names that differ only in the dotted and dotless i (`SATIŞ` and `satış`) are two departments, and a glibc upgrade can change the stored values. Use a UTF-8 locale and do not change it on a live database.
- **Downgrading 0002 deletes every user, credential, profile and audit entry.** The migration refuses unless `PHAROS_ALLOW_DATA_LOSS=yes` is set. Back up first.
- **The application role can change `role` and `status` at the schema level**; the service layer decides who may (RBAC, M2-07). The first sign-in upsert must never write them, and removing or demoting an admin needs a lock so two admins cannot demote each other at once (both are tested in their tasks).
- **Upserting a department** (`ON CONFLICT ... DO UPDATE SET name = departments.name`) writes a new row version, so `updated_at` changes on every sign-in. Treat `updated_at` on departments as "last seen", not "last edited".
- **Display names over 200 characters** are rejected by the database; the sign-in code must truncate them.
- **Anyone who can run arbitrary SQL as the application role** can turn a guessable slug into the organization id with `pharos_org_id_for_slug` and then set `app.org_id` themselves. That is the documented limit of RLS (see above) and does not matter in a one-organization installation, where the slug is in the environment anyway.

## Indexes and pagination

Indexes follow real feed queries (profession, language, published date, quality) and are added with the feature that needs them, not up front. Feeds use cursor pagination.

### Pipeline from raw entries to items (M3-08)

`pharos.ingest.pipeline` runs in the transaction that stores the raw entries, so a lost lease discards both. Only entries that are new or changed are looked at.

- **Canonical link:** tracking parameters (`utm_*`, `fbclid`, `gclid`, found even when their name is percent-escaped) and the fragment are removed, the host is lowercased and loses a trailing dot, the default port is dropped; every other parameter is kept. The query is split on `&` and never decoded and encoded again (that would lengthen a link past the database limit and make different invalid escapes the same link). The result must pass the same link rules as a stored link, otherwise the entry is dropped. `url_hash` is the SHA-256 of that link.
- **Exact de-duplication:** `INSERT ... ON CONFLICT (org_id, owner_user_id, url_hash)`, in a fixed `url_hash` order so two fetches that share links lock rows alike. On a conflict only the text of an item is refreshed, and only when all three hold: the content changed, the new text is not flagged by a rule, and this source is the one that first reported the article (earliest `item_sources` row). `status` and `hold_reason` are never touched by the ingestion path. Since migration 0006 the worker has the right to write them in one case only, holding a `new` article as `low_quality` after the model read it; a trigger refuses every other change (ADR-0017). Another source reporting the same link adds a link, not a new title. An entry stored again with unchanged content (for example after the 14-day raw cleanup, F-23) changes nothing. Near-duplicates are deferred to M4.
- **Hold rules** (flags for a person, not verdicts): `link_ip_host` (the link names an IP address in any form a browser accepts: dotted, decimal such as `2130706433`, short such as `127.1`, hex, IPv6), `link_odd_port` (a port left after the scheme's default was dropped), `instruction_phrase` (text that addresses a model, such as "ignore previous instructions" or "forget all prior prompts"; the text is already NFKC-normalized and stripped of invisible characters). A held item has `status = held` and the rule name in `hold_reason`.
- **Known limits:** the rules run on the text that would be stored. Flagged new text of an existing item is not stored at all, so an approved or clean item keeps its old text until a person acts (the app can see neither the discarded text nor that it was discarded: F-24). Look-alike letters and phrases in other languages are not recognized. Different parameter order, Unicode versus punycode hosts and `%7E` versus `~` still count as different articles. Text from a feed stays untrusted data for every later step either way.
- `lang` stays empty until M4.

### Source configurations (M3-09)

Validated by the connector of the type, stored as small JSON, and checked again by the connector at every fetch (a stored configuration is never trusted).

- `rss`: `{"url": "https://..."}`.
- `github`: `{"repo": "owner/name"}`. One request per run to `api.github.com/repos/<owner>/<name>/releases`; drafts and pre-releases are skipped. The optional token is not part of the configuration: it comes from `PHAROS_GITHUB_TOKEN` in the worker's environment.
- `hackernews`: `{"query": "words", "min_points": 20}`. One Algolia search per run (stories only, at most 30 hits); a story without a link gets the address of its discussion page.
- Rules for a configuration live in `pharos.ingest.sourceconfig` (no network in it): the connectors and the API use the same code, and the API refuses an address that can only be internal when it is typed (M3-10a).
- Entries from JSON connectors are cleaned like feed entries (plain text, bounded, `http(s)` links only, no lone surrogates) and arrive at the pipeline in the same shape.

## Open items

- Final embedding model and vector dimension (ADR-0007).
- Retention defaults for logs, audit records and activity data.
- Anonymous idea pool design (phase 2): the "admins cannot see authors" claim is not made until it is proven by design and tests.
