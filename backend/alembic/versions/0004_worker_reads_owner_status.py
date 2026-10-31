"""The worker may read whether the owner of a personal feed is still an active person.

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-30

A personal feed belongs to one person. When that person is disabled or deprovisioned their feeds
must stop being fetched, and no administrator can see or stop them (row-level security hides them).
The worker is the only process that decides what is fetched, so it needs one fact about people and
nothing more: the state of the account. The grant is by column (`org_id`, `id`, `status`); the
e-mail, the name, the role and everything else about a person stay unreadable for the worker, and
row-level security still limits it to the organization it entered (ADR-0014,
`tests/integration/test_worker_role.py`).
"""

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

WORKER_ROLE = "pharos_worker"


def upgrade() -> None:
    op.execute(f"GRANT SELECT (org_id, id, status) ON users TO {WORKER_ROLE}")


def downgrade() -> None:
    op.execute(f"REVOKE SELECT (org_id, id, status) ON users FROM {WORKER_ROLE}")  # noqa: S608
