# 0005. Tenancy: one installation per organization, two data planes, PostgreSQL RLS

- Status: Accepted
- Date: 2026-10-26

## Context

Two planning rules collide: "every table has `org_id`" and "LLM results are shared across
organizations by profession and language" to keep cost down. The master prompt also asks for the
tenant in every cache key. If insights were keyed per tenant, the same public article would be
processed once per organization, multiplying LLM cost; if nothing were tenant-scoped, company
data could leak. Earlier docs also never said where `org_id` comes from.

## Options considered

1. **Hosted multi-tenant SaaS** — pros: one operator, maximum sharing. Cons: a single breach
   exposes every customer; operating a SaaS is out of scope for the MVP.
2. **`org_id` on every table, all caches per tenant** — pros: simple rule. Cons: public content
   and insights are regenerated per organization; LLM cost scales with tenant count for no
   privacy gain, because the content is public.
3. **One installation = one organization, tenant-aware code, content plane + tenant plane, RLS**
   — pros: public work is done once; company data is isolated twice (code and database).
   Cons: two scoping rules to understand; RLS adds migration and testing discipline.

## Decision

Option 3 (accepted decision 2). No hosted multi-tenant SaaS in the MVP, but the code is tenant-aware.

- **Content plane (public, shared):** `sources` (global), `raw_items`, `items`,
  `item_embeddings`, `item_professions`, `item_insights` with `scope='public'` and
  `owner_org_id` NULL. User or company data is never stored here.
- **Organization-owned content:** items from an organization's own sources and their insights use
  `scope='org'` with `owner_org_id` NOT NULL. A CHECK constraint ties `scope` to `owner_org_id`.
  RLS policy: `scope = 'public' OR owner_org_id = current org`.
- **Tenant plane:** `organizations`, `users`, `departments`, `user_profiles`, `user_sources`,
  `interactions`, `llm_provider_configs`, `llm_usage`, `audit_logs` and Phase 2 tables have
  `org_id` NOT NULL, are filtered in the repository layer and protected by RLS.
- **RLS mechanism:** at the start of each transaction the API sets `app.org_id` with
  `SET LOCAL` (or `set_config(..., true)`) to the session's org. The application role does not
  own the tables, so it cannot bypass RLS. Migrations run under a separate, more privileged role.
- `org_id` comes only from the authenticated session ([0003](0003-authentication-oidc-bff.md)),
  never from a request body, path or header.

**Cost vs isolation.** Public insights are cached once per (item, profession, language,
prompt_version) and shared, because they contain only public input. Organization-owned items are
cached under the same key but are reachable only by their owner through `owner_org_id` and RLS.
This supersedes the master-prompt rule to add the tenant to every cache key: tenant scoping
applies to the data, and public data carries no tenant.

## Consequences

- Positive: LLM cost scales with active profession/language pairs, not with tenants; isolation
  is enforced in code and in the database.
- Negative: every new table must be classified into a plane; RLS policies need migrations and tests.
- Follow-up tests: two-organization fixture with a cross-tenant test per endpoint; a test that
  the application role cannot bypass RLS; CHECK-constraint tests on `scope`/`owner_org_id`.
- Follow-up docs: plane per table in `docs/DATA_MODEL.md`; RLS helper in the Alembic baseline (M1-05).

## References

- `docs/ARCHITECTURE_PROPOSAL.md` accepted decision 2, sections 1, 2 (rows 4-5), 3, 5, 6, 7, 15
- [0002 LLM gateway](0002-llm-gateway-litellm.md), [0007 Embeddings](0007-embedding-model-and-dimension.md)
