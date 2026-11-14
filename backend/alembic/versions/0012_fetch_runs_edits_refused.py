"""How many edits of a fetch a hold rule refused (F-24, ADR-0028).

Revision ID: 0012
Revises: 0011
Create Date: 2026-11-14

When a source changes the text of an article it reported before, and the new text is something a
hold rule flags, the item keeps its old text: the worker may not hold an article a person has
decided about. Until now that left no trace. `fetch_runs.edits_refused` counts those edits for
the run, so a source that starts to send flagged text shows on the Sources page.

The worker writes the column with the other counts of the run (the grant is widened by this one
column); the application role reads it through its existing `SELECT` on the table. Nothing changes
on `items` or its triggers. A downgrade drops the column and the counts in it; they are
operational numbers for the last 30 days of runs, rebuilt by the next fetches.
"""

import sqlalchemy as sa
from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None

WORKER_ROLE = "pharos_worker"


def upgrade() -> None:
    op.add_column("fetch_runs", sa.Column("edits_refused", sa.Integer(), nullable=True))
    op.create_check_constraint("fetch_runs_edits_refused_check", "fetch_runs", "edits_refused >= 0")
    op.execute(f"GRANT UPDATE (edits_refused) ON fetch_runs TO {WORKER_ROLE}")


def downgrade() -> None:
    op.execute(f"REVOKE UPDATE (edits_refused) ON fetch_runs FROM {WORKER_ROLE}")
    op.drop_constraint("fetch_runs_edits_refused_check", "fetch_runs", type_="check")
    op.drop_column("fetch_runs", "edits_refused")
