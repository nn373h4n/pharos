---
paths:
  - "backend/**/*.py"
  - "connectors/**/*.py"
---

# Backend rules

- Layers: `router.py` → `service.py` → `repository.py`. Routers hold no SQL or business logic; services do not import FastAPI; a module never touches another module's repository.
- Get the tenant with the `current_org` dependency; never read `org_id` from the request.
- SQLAlchemy 2.0 style, async sessions, parameterized queries only.
- Pydantic v2 schemas for every request and response; one error format.
- Every schema change is an Alembic migration with upgrade and downgrade; never edit a merged migration.
- Background tasks are idempotent and safe to retry.
- Outbound HTTP only through `SafeHttpClient`.
- Tests in `backend/tests/` mirror the module tree; use the fake LLM provider and recorded fixtures.
