"""Insights: how an article applies to a profession, in a language (ADR-0015, points 4 to 6).

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-01

One row per (article, profession, language, prompt version): the cache key of ADR-0015. A row is
either `ok`, with every text of the card, or `rejected`, with none: the answer did not pass the
checks and is remembered so it is not paid for again at every view, but only for a day: the worker
may delete a rejected row once it is a day old, so a model glitch or an article that tripped a
rule does not block the insight for ever. The worker writes rows once (it may insert and read,
never change them, and delete nothing else); the application reads them. A new prompt version
is a new row, and old rows stay until a later clean-up.

The person's level and interests are not part of the key: they change ranking and tone, not the
text, so the number of calls stays the number of (profession, language) pairs that have people.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None

APP_ROLE = "pharos_app"
WORKER_ROLE = "pharos_worker"


def upgrade() -> None:
    op.create_table(
        "item_insights",
        sa.Column("org_id", sa.Uuid(), sa.ForeignKey("organizations.id"), nullable=False),
        sa.Column("item_id", sa.Uuid(), nullable=False),
        sa.Column("profession_code", sa.Text(), nullable=False),
        sa.Column("language", sa.Text(), nullable=False),
        sa.Column("prompt_version", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("how_to_apply", postgresql.JSONB(), nullable=True),
        sa.Column("try_prompt", sa.Text(), nullable=True),
        sa.Column("safety_note", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.PrimaryKeyConstraint(
            "org_id",
            "item_id",
            "profession_code",
            "language",
            "prompt_version",
            name="item_insights_pkey",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "item_id"],
            ["items.org_id", "items.id"],
            name="item_insights_item_fkey",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint("language IN ('az', 'tr', 'ru', 'en')", name="item_insights_language"),
        sa.CheckConstraint(
            "profession_code ~ '^[a-z][a-z0-9_]{1,63}$' AND prompt_version ~ '^[a-z_0-9.]{1,40}$'",
            name="item_insights_names",
        ),
        sa.CheckConstraint("status IN ('ok', 'rejected')", name="item_insights_status"),
        sa.CheckConstraint(
            "(status = 'ok' AND summary IS NOT NULL AND how_to_apply IS NOT NULL "
            "AND try_prompt IS NOT NULL AND safety_note IS NOT NULL "
            "AND char_length(summary) BETWEEN 1 AND 400 "
            "AND char_length(try_prompt) BETWEEN 1 AND 800 "
            "AND char_length(safety_note) BETWEEN 1 AND 300 "
            "AND jsonb_typeof(how_to_apply) = 'array' "
            "AND jsonb_array_length(how_to_apply) BETWEEN 1 AND 3) "
            "OR (status = 'rejected' AND summary IS NULL AND how_to_apply IS NULL "
            "AND try_prompt IS NULL AND safety_note IS NULL)",
            name="item_insights_content",
        ),
    )

    predicate = "org_id = pharos_current_org()"
    for statement in (
        "ALTER TABLE item_insights ENABLE ROW LEVEL SECURITY",
        f"CREATE POLICY item_insights_tenant_isolation ON item_insights "
        f"USING ({predicate}) WITH CHECK ({predicate})",
        # The worker may delete one kind of row: a rejected answer that is a day old.
        "CREATE POLICY item_insights_worker_deletes_old_rejections ON item_insights "
        f"AS RESTRICTIVE FOR DELETE USING (current_user <> '{WORKER_ROLE}' "
        "OR (status = 'rejected' AND created_at < now() - interval '24 hours'))",
        # Rows of an article inherit its visibility, as item_sources does.
        "CREATE POLICY item_insights_personal_rows ON item_insights AS RESTRICTIVE "  # noqa: S608
        f"FOR SELECT USING (current_user = '{WORKER_ROLE}' OR EXISTS (SELECT 1 FROM items i "
        "WHERE i.org_id = item_insights.org_id AND i.id = item_insights.item_id))",
    ):
        op.execute(statement)

    op.execute(f"GRANT SELECT ON item_insights TO {APP_ROLE}")
    op.execute(f"GRANT SELECT, DELETE ON item_insights TO {WORKER_ROLE}")
    op.execute(
        "GRANT INSERT (org_id, item_id, profession_code, language, prompt_version, status, "
        f"summary, how_to_apply, try_prompt, safety_note) ON item_insights TO {WORKER_ROLE}"
    )


def downgrade() -> None:
    op.drop_table("item_insights")
