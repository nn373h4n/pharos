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

Two login roles are provisioned by the deployment before the first migration. The Docker Compose stack does it with `deploy/postgres/init/01-roles.sh` on the first start of an empty data volume. A migration refuses to run if `pharos_app` is missing, is a superuser or has `BYPASSRLS`, or if it is run as `pharos_app`.

| Role | Used by | Rights |
| --- | --- | --- |
| `pharos_owner` | Alembic migrations only; owns the schema objects | DDL; its credentials are not part of the API/worker settings |
| `pharos_app` | API and worker | `NOSUPERUSER NOBYPASSRLS`, no ownership; only the grants a migration gives it, by column where it matters (for example `organizations`: `SELECT`, and `UPDATE` on `name`, `settings`, `updated_at`). `tests/integration/test_identity_schema.py` compares the complete list of its privileges with an expected set, so a grant added by mistake fails the build |

```sql
CREATE ROLE pharos_owner LOGIN PASSWORD '<generated>';
CREATE ROLE pharos_app LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE PASSWORD '<generated>';
CREATE DATABASE pharos OWNER pharos_owner;
GRANT CONNECT ON DATABASE pharos TO pharos_app;
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
| `sources` | id, scope, owner_org_id, connector_id, config_encrypted, schedule, enabled, last_fetched_at | Connector configuration is encrypted |
| `raw_items` | id, source_id, external_id, url, payload (jsonb), fetched_at | Temporary; proposed retention 30 days |
| `items` | id, scope, owner_org_id, canonical_url, url_hash (unique), title, excerpt, lang, published_at, quality_score, cluster_id, status | Full text is not stored: title, short excerpt and link only |
| `item_embeddings` | item_id, model, dim, embedding (vector) | Recomputed when the model changes ([ADR-0007](adr/0007-embedding-model-and-dimension.md)) |
| `item_professions` | item_id, profession_id, relevance | Enrichment output |
| `item_insights` | item_id, profession_id, language, prompt_version, model, summary, how_to_apply, try_prompt, safety_note, status | Unique on (item, profession, language, prompt_version) |

### Phase 2 (not in MVP)

`ideas`, `idea_votes`, `success_stories`, `time_savings`, `badges`, `user_badges`, `challenges`. Company-internal content never goes to a cloud LLM by default; when an admin enables it, masking is applied and the choice is audited.

## Known limits of the identity schema (0002)

- **Case folding depends on the database locale.** `departments.normalized_name` uses `lower()`, which follows the database's `LC_CTYPE`. The migration refuses the `C` locale, but under `en_US.utf8` Turkish and Azerbaijani names that differ only in the dotted and dotless i (`SATIŞ` and `satış`) are two departments, and a glibc upgrade can change the stored values. Use a UTF-8 locale and do not change it on a live database.
- **Downgrading 0002 deletes every user, credential, profile and audit entry.** The migration refuses unless `PHAROS_ALLOW_DATA_LOSS=yes` is set. Back up first.
- **The application role can change `role` and `status` at the schema level**; the service layer decides who may (RBAC, M2-07). The first sign-in upsert must never write them, and removing or demoting an admin needs a lock so two admins cannot demote each other at once (both are tested in their tasks).
- **Upserting a department** (`ON CONFLICT ... DO UPDATE SET name = departments.name`) writes a new row version, so `updated_at` changes on every sign-in. Treat `updated_at` on departments as "last seen", not "last edited".
- **Display names over 200 characters** are rejected by the database; the sign-in code must truncate them.
- **Anyone who can run arbitrary SQL as the application role** can turn a guessable slug into the organization id with `pharos_org_id_for_slug` and then set `app.org_id` themselves. That is the documented limit of RLS (see above) and does not matter in a one-organization installation, where the slug is in the environment anyway.

## Indexes and pagination

Indexes follow real feed queries (profession, language, published date, quality) and are added with the feature that needs them, not up front. Feeds use cursor pagination.

## Open items

- Final embedding model and vector dimension (ADR-0007).
- Retention defaults for logs, audit records and activity data.
- Anonymous idea pool design (phase 2): the "admins cannot see authors" claim is not made until it is proven by design and tests.
