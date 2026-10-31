"""The record of model calls: numbers only (ADR-0016).

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-31

One row per call or refusal: which stage, provider and model, how many tokens, what it cost in
millionths of a US dollar, and how it ended. Never a prompt, an answer, an article, a person or a
key. The table belongs to an organization under the ordinary tenant policy. The worker, which makes
the calls, may only add rows (by column); the application reads them for the budget page and
changes nothing.
"""

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

APP_ROLE = "pharos_app"
WORKER_ROLE = "pharos_worker"

STAGES = "('filter', 'enrichment', 'insight', 'key_test')"
OUTCOMES = "('ok', 'rejected', 'failed', 'refused')"


def upgrade() -> None:
    op.create_table(
        "llm_usage",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("org_id", sa.Uuid(), sa.ForeignKey("organizations.id"), nullable=False),
        sa.Column("stage", sa.Text(), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("tokens_in", sa.Integer(), nullable=False),
        sa.Column("tokens_out", sa.Integer(), nullable=False),
        sa.Column("cost_micro_usd", sa.BigInteger(), nullable=False),
        sa.Column("outcome", sa.Text(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint(f"stage IN {STAGES}", name="llm_usage_stage_check"),
        sa.CheckConstraint(f"outcome IN {OUTCOMES}", name="llm_usage_outcome_check"),
        sa.CheckConstraint(
            "tokens_in >= 0 AND tokens_out >= 0 AND cost_micro_usd >= 0",
            name="llm_usage_numbers_check",
        ),
        sa.CheckConstraint(
            "length(provider) <= 40 AND length(model) <= 120", name="llm_usage_names"
        ),
    )
    op.create_index("llm_usage_by_day", "llm_usage", ["org_id", "created_at"])

    predicate = "org_id = pharos_current_org()"
    op.execute("ALTER TABLE llm_usage ENABLE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY llm_usage_tenant_isolation ON llm_usage USING ({predicate}) "
        f"WITH CHECK ({predicate})"
    )
    op.execute(f"GRANT SELECT ON llm_usage TO {APP_ROLE}")
    op.execute(
        "GRANT INSERT (org_id, stage, provider, model, tokens_in, tokens_out, cost_micro_usd, "
        f"outcome) ON llm_usage TO {WORKER_ROLE}"
    )


def downgrade() -> None:
    op.drop_table("llm_usage")
