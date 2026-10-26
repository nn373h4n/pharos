# 0001. Modular monolith platform

- Status: Accepted
- Date: 2026-10-26

## Context

Pharos is a self-hosted, open-source platform that one developer plus contributors must build,
and that any organization must be able to install on a single Linux VM with Docker Compose.
The workload is a feed API, a scheduled ingestion pipeline and LLM enrichment. The product needs
relational data with row-level security, vector similarity for dedup and ranking, and strict
module boundaries so that tenant isolation and the LLM boundary stay reviewable.
Separate services are only justified by a measured need (principle 1, "modular monolith").

## Options considered

1. **Modular monolith: FastAPI + PostgreSQL 16/pgvector, SQLAlchemy 2 + Alembic** — pros: one
   codebase and one deployable for api and worker; typed async Python fits the LLM and connector
   ecosystem; pgvector keeps vectors next to relational data, so no extra vector database; RLS is
   native. Cons: boundaries are enforced by convention and tooling, not by the network; one bad
   module can still affect the process.
2. **Microservices** — pros: independent scaling and deployment per service. Cons: more
   containers, network calls and failure modes for every self-hoster; distributed transactions
   and per-service auth; no measured load that requires it.
3. **Django** — pros: batteries included (admin, ORM, migrations). Cons: its built-in auth and
   admin overlap with the OIDC BFF and custom admin panel we need anyway; async support and
   typed request/response schemas are weaker than FastAPI + Pydantic.
4. **Node.js backend** — pros: one language with the Next.js frontend. Cons: the LLM, embedding
   and connector tooling we rely on (LiteLLM, Celery) is Python-first; two runtimes would remain
   for the worker anyway.

## Decision

Option 1. Pharos is one Python package (`pharos`) started with different entry points for
`api` and `worker`. Stack: FastAPI, SQLAlchemy 2, Alembic, PostgreSQL 16 + pgvector, Redis.
Tooling: a `uv` workspace for Python (backend + connectors) and `pnpm` for the frontend.

Every backend module (`core`, `auth`, `orgs`, `users`, `onboarding`, `sources`, `ingestion`,
`content`, `enrichment`, `llm`, `feed`, `admin`, `audit`, `workers`) follows
`router.py -> service.py -> repository.py`, plus `schemas.py` and `models.py`. Rules:

- routers do not write SQL;
- services do not import FastAPI;
- a module never touches another module's repository; it calls that module's service.

These rules are enforced in CI with `import-linter` contracts, not by review alone.

## Consequences

- Positive: one compose file, one image for api and worker, simple local development and
  self-hosting; module rules are machine-checked.
- Negative: scaling is per process type (api, worker), not per module; a module that later
  needs isolation has to be extracted deliberately, with a new ADR and a measured need.
- Follow-up: add `import-linter` contracts with the backend skeleton (M1-05) and run them in CI
  (M1-09); document the module map in `docs/ARCHITECTURE.md`; add a deliberately violating
  import in a test fixture or CI check to prove the contract fails.

## References

- `docs/ARCHITECTURE_PROPOSAL.md` sections 1, 3, 4, 10, 15
- [0004 Background jobs](0004-background-jobs-celery.md), [0005 Tenancy and RLS](0005-tenancy-and-rls.md)
