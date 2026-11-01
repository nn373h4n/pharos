"""What people do with the cards, private to each person (ADR-0018).

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-01

One row per (person, article, kind): view, save, useful, not_useful, tried, time_saved (minutes).
The database, not the code, keeps them private: a restrictive policy lets the application role see
and write only the rows of the person named in `app.user_id`, so an administrator cannot read what
anybody did. The organization sees counts only, through one `SECURITY DEFINER` function that
returns, per COMPLETE UTC day and kind, how many people and actions there were and the minutes
saved (each person's share capped, the total rounded), and leaves out any day and kind that rests
on fewer than five people. The caller cannot move the window or the day boundary (it passes a
number of days, clamped, and the function fixes the time zone), and a row never moves to another
day (`created_at` is not updated), so successive answers cannot be differenced to see one action.

The partial index serves the Home feed: public, current articles the model has read.
"""

import os

import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None

APP_ROLE = "pharos_app"
FLOOR = 5
# One person's minutes count for at most this much in a day's total, and the total is rounded to a
# multiple of the step, so a sum does not give away one person's figure.
MINUTES_CAP = 120
MINUTES_STEP = 30


def upgrade() -> None:
    op.create_table(
        "interactions",
        sa.Column("org_id", sa.Uuid(), sa.ForeignKey("organizations.id"), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("item_id", sa.Uuid(), nullable=False),
        sa.Column("type", sa.Text(), nullable=False),
        sa.Column("value", sa.SmallInteger(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.PrimaryKeyConstraint("org_id", "user_id", "item_id", "type", name="interactions_pkey"),
        sa.ForeignKeyConstraint(
            ["org_id", "user_id"],
            ["users.org_id", "users.id"],
            name="interactions_user_fkey",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "item_id"],
            ["items.org_id", "items.id"],
            name="interactions_item_fkey",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "type IN ('view', 'save', 'useful', 'not_useful', 'tried', 'time_saved')",
            name="interactions_type",
        ),
        sa.CheckConstraint(
            "(type = 'time_saved' AND value BETWEEN 1 AND 480) "
            "OR (type <> 'time_saved' AND value IS NULL)",
            name="interactions_value",
        ),
    )
    op.create_index("interactions_by_day", "interactions", ["org_id", "created_at"])
    op.create_index("interactions_by_item", "interactions", ["org_id", "item_id", "type"])

    op.execute("ALTER TABLE interactions ENABLE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY interactions_tenant_isolation ON interactions "
        "USING (org_id = pharos_current_org()) WITH CHECK (org_id = pharos_current_org())"
    )
    # Private to the person: it narrows what the tenant policy allows, for every role.
    op.execute(
        "CREATE POLICY interactions_own_rows ON interactions AS RESTRICTIVE FOR ALL "
        "USING (user_id = pharos_current_user()) WITH CHECK (user_id = pharos_current_user())"
    )
    op.execute(f"GRANT SELECT, INSERT, DELETE ON interactions TO {APP_ROLE}")
    op.execute(f"GRANT UPDATE (value) ON interactions TO {APP_ROLE}")

    op.execute(
        """
        CREATE FUNCTION public.pharos_interaction_counts(days integer)
        RETURNS TABLE (day date, type text, people bigint, actions bigint, minutes bigint)
        LANGUAGE sql STABLE SECURITY DEFINER
        SET search_path = pg_catalog, pg_temp
        SET timezone = 'UTC'
        AS $$
            SELECT i.created_at::date,
                   i.type,
                   count(DISTINCT i.user_id),
                   count(*),
                   CASE WHEN i.type = 'time_saved'
                        THEN round(sum(least(i.value, __CAP__)) / __STEP__.0) * __STEP__ END
            FROM public.interactions i
            WHERE i.org_id = public.pharos_current_org()
              AND i.created_at::date < now()::date
              AND i.created_at::date >= now()::date - greatest(1, least(days, 90))
            GROUP BY 1, 2
            HAVING count(DISTINCT i.user_id) >= __FLOOR__
            ORDER BY 1, 2
        $$
        """.replace("__FLOOR__", str(FLOOR))
        .replace("__CAP__", str(MINUTES_CAP))
        .replace("__STEP__", str(MINUTES_STEP))
    )
    op.execute("REVOKE ALL ON FUNCTION public.pharos_interaction_counts(integer) FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION public.pharos_interaction_counts(integer) TO {APP_ROLE}")

    op.create_index(
        "items_for_cards",
        "items",
        ["org_id", sa.text("(coalesce(published_at, fetched_at)) DESC")],
        postgresql_where=sa.text(
            "owner_user_id IS NULL AND status = 'new' AND enriched_at IS NOT NULL "
            "AND summary IS NOT NULL"
        ),
    )


def downgrade() -> None:
    has_data = op.get_bind().execute(sa.text("SELECT EXISTS (SELECT 1 FROM interactions)")).scalar()
    if has_data and os.environ.get("PHAROS_ALLOW_DATA_LOSS") != "yes":
        raise RuntimeError(
            "Downgrading 0008 would delete what people did with the cards. "
            "Back up first, then set PHAROS_ALLOW_DATA_LOSS=yes to proceed."
        )
    op.drop_index("items_for_cards", table_name="items")
    op.execute("DROP FUNCTION public.pharos_interaction_counts(integer)")
    op.drop_table("interactions")
