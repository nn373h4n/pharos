"""Monthly counts that outlive the interactions they were made from (M5-09, ADR-0025).

Revision ID: 0011
Revises: 0010
Create Date: 2026-11-05

What people do with the cards is private to each person and is kept for twelve months (a setting).
Before the rows of older months are deleted, the operator command `fold-interactions` writes what
the organization may know about them into this table: per month and kind, how many people, how many
actions and the minutes saved. A month and kind that rests on fewer than five people is not
written (the same floor as the daily summary of 0008), so the counts cannot be used to find out
what one person did. No person, article or day is kept.

Only the table owner touches it: the command runs as the owner role. The application and the
worker roles are granted nothing on it, which is what keeps them out; nothing in the application
reads it yet and the pilot's report reads it with SQL. Row-level security is switched on with the
usual tenant policy as a second line for a role that is given access later; the table owner
bypasses it (as on every table, see DATA_MODEL.md), so it protects no operator command.
"""

import os

import sqlalchemy as sa
from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None

FLOOR = 5


def upgrade() -> None:
    op.create_table(
        "interaction_counts_monthly",
        sa.Column(
            "org_id",
            sa.Uuid(),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("month", sa.Date(), nullable=False),
        sa.Column("type", sa.Text(), nullable=False),
        sa.Column("people", sa.Integer(), nullable=False),
        sa.Column("actions", sa.BigInteger(), nullable=False),
        sa.Column("minutes", sa.BigInteger(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.PrimaryKeyConstraint("org_id", "month", "type", name="interaction_counts_monthly_pkey"),
        sa.CheckConstraint(
            "EXTRACT(day FROM month) = 1", name="interaction_counts_month_first_day"
        ),
        sa.CheckConstraint(
            "type IN ('view', 'save', 'useful', 'not_useful', 'tried', 'time_saved')",
            name="interaction_counts_type",
        ),
        # A month and kind that rests on fewer people than the floor is never stored: this is the
        # privacy rule, kept by the table itself and not only by the command that writes it.
        sa.CheckConstraint(f"people >= {FLOOR}", name="interaction_counts_floor"),
        sa.CheckConstraint("actions >= people", name="interaction_counts_actions"),
        sa.CheckConstraint(
            "(type = 'time_saved' AND minutes >= 0) OR (type <> 'time_saved' AND minutes IS NULL)",
            name="interaction_counts_minutes",
        ),
    )
    op.execute("ALTER TABLE interaction_counts_monthly ENABLE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY interaction_counts_monthly_tenant_isolation ON interaction_counts_monthly "
        "USING (org_id = pharos_current_org()) WITH CHECK (org_id = pharos_current_org())"
    )
    # No GRANT: the application and the worker roles have no access at all.


def downgrade() -> None:
    has_data = (
        op.get_bind()
        .execute(sa.text("SELECT EXISTS (SELECT 1 FROM interaction_counts_monthly)"))
        .scalar()
    )
    if has_data and os.environ.get("PHAROS_ALLOW_DATA_LOSS") != "yes":
        raise RuntimeError(
            "Downgrading 0011 would delete the monthly counts that replaced old interactions. "
            "Back up first, then set PHAROS_ALLOW_DATA_LOSS=yes to proceed."
        )
    op.drop_table("interaction_counts_monthly")
