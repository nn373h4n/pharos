# Data model

Status: v0 (milestone M1). Only `organizations` and the RLS helper exist in the first migration (M1-05); everything else is the target model and arrives with its milestone. Tenancy rules: [ADR-0005](adr/0005-tenancy-and-rls.md).

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
| `pharos_app` | API and worker | `NOSUPERUSER NOBYPASSRLS`, no ownership; only the grants a migration gives it (for `organizations`: `SELECT`, and `UPDATE` on `name`, `settings`, `updated_at`) |

```sql
CREATE ROLE pharos_owner LOGIN PASSWORD '<generated>';
CREATE ROLE pharos_app LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE PASSWORD '<generated>';
CREATE DATABASE pharos OWNER pharos_owner;
GRANT CONNECT ON DATABASE pharos TO pharos_app;
```

Creating an organization is an operator task done as `pharos_owner`; the application cannot create or delete organizations or change an organization's `id`, `slug` or `created_at`.

## Tables

### Tenant plane

| Table | Main columns | Notes |
| --- | --- | --- |
| `organizations` | id, name, slug, settings (jsonb: branding, locale, policies) | Usually one row per installation |
| `users` | id, org_id, external_id, email, display_name, department_id, locale, role, status | Role: `admin`, `editor`, `member` |
| `departments` | id, org_id, name | |
| `user_profiles` | user_id, org_id, profession_id, ai_level, interests | Onboarding result |
| `user_sources` | id, org_id, user_id, source_id | User-added sources are checked by `SafeHttpClient` |
| `interactions` | org_id, user_id, item_id, type, value, created_at | Types: view, save, useful, not_useful, tried, time_saved |
| `llm_provider_configs` | org_id, provider, stage_model_map, api_key_encrypted, key_version | |
| `llm_usage` | org_id, stage, provider, model, tokens_in, tokens_out, cost_estimate, created_at | No content stored |
| `audit_logs` | org_id, actor_id, action, target, ip_hash, created_at | No secrets or content |

### Content plane

| Table | Main columns | Notes |
| --- | --- | --- |
| `professions` | id, org_id (null = global), code, names (jsonb: en/az/tr/ru), parent_id | Global taxonomy plus an organization's own |
| `sources` | id, scope, owner_org_id, connector_id, config_encrypted, schedule, enabled, last_fetched_at | Connector configuration is encrypted |
| `raw_items` | id, source_id, external_id, url, payload (jsonb), fetched_at | Temporary; proposed retention 30 days |
| `items` | id, scope, owner_org_id, canonical_url, url_hash (unique), title, excerpt, lang, published_at, quality_score, cluster_id, status | Full text is not stored: title, short excerpt and link only |
| `item_embeddings` | item_id, model, dim, embedding (vector) | Recomputed when the model changes ([ADR-0007](adr/0007-embedding-model-and-dimension.md)) |
| `item_professions` | item_id, profession_id, relevance | Enrichment output |
| `item_insights` | item_id, profession_id, language, prompt_version, model, summary, how_to_apply, try_prompt, safety_note, status | Unique on (item, profession, language, prompt_version) |

### Phase 2 (not in MVP)

`ideas`, `idea_votes`, `success_stories`, `time_savings`, `badges`, `user_badges`, `challenges`. Company-internal content never goes to a cloud LLM by default; when an admin enables it, masking is applied and the choice is audited.

## Indexes and pagination

Indexes follow real feed queries (profession, language, published date, quality) and are added with the feature that needs them, not up front. Feeds use cursor pagination.

## Open items

- Final embedding model and vector dimension (ADR-0007).
- Retention defaults for logs, audit records and activity data.
- Anonymous idea pool design (phase 2): the "admins cannot see authors" claim is not made until it is proven by design and tests.
