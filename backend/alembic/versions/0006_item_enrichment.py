"""What the model says about a public article (ADR-0017).

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-31

An item gains a short summary, a category, a quality score and the version of the prompt that made
them (an article judged irrelevant has only the time and the version); `item_professions` holds how relevant the article is to each
profession. The worker writes them. It also gets one narrow new right over an article's state: it
may move an article from `new` to `held` with the reason `low_quality`, and only an article that
no person has decided about and that the model has not read yet. A trigger
enforces this (a policy cannot, because the ingestion path updates the text of held rows too), so
the worker still cannot release, hide or restore an article, and cannot give any other reason.

The profession list is read by the worker (public rows and the organization's own, by the existing
policy) so the model's answer can be limited to codes that exist. One global profession is added
for the pilot: the SAP Basis administrator.
"""

import json

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

APP_ROLE = "pharos_app"
WORKER_ROLE = "pharos_worker"

CATEGORIES = (
    "('tool', 'technique', 'case_study', 'research', 'policy', 'tutorial', 'news', 'other')"
)
BASIS = (
    "sap_basis_administrator",
    {
        "en": "SAP Basis administrator",
        "az": "SAP Basis administratoru",
        "tr": "SAP Basis yöneticisi",
        "ru": "Администратор SAP Basis",
    },
)


def _run(statements: list[str]) -> None:
    # One statement per call: the asyncpg driver cannot prepare several commands at once.
    for statement in statements:
        op.execute(statement)


def upgrade() -> None:
    op.add_column("items", sa.Column("summary", sa.Text(), nullable=True))
    op.add_column("items", sa.Column("category", sa.Text(), nullable=True))
    op.add_column("items", sa.Column("quality", sa.SmallInteger(), nullable=True))
    op.add_column("items", sa.Column("enriched_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("items", sa.Column("enrichment_version", sa.Text(), nullable=True))
    # Set by the application when a person decides about the article (hold, approve, hide,
    # restore). The model never reads or holds an article a person has decided about.
    op.add_column("items", sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True))
    op.create_check_constraint(
        "items_enrichment_check",
        "items",
        # The summary, category and quality come together. An article the small model found
        # irrelevant has none of them but was still read (enriched_at); a summary is never
        # without the time and the prompt version that made it.
        "(summary IS NULL) = (category IS NULL) AND (summary IS NULL) = (quality IS NULL) "
        "AND (enriched_at IS NULL) = (enrichment_version IS NULL) "
        "AND (summary IS NULL OR enriched_at IS NOT NULL)",
    )
    op.create_check_constraint(
        "items_enrichment_values",
        "items",
        f"(summary IS NULL OR char_length(summary) BETWEEN 1 AND 400) "
        f"AND (category IS NULL OR category IN {CATEGORIES}) "
        "AND (quality IS NULL OR quality BETWEEN 1 AND 5) "
        "AND (enrichment_version IS NULL OR enrichment_version ~ '^[a-z_0-9.]{1,40}$')",
    )
    # The sweep looks for public articles that have not been read by the model yet.
    op.create_index(
        "items_to_enrich",
        "items",
        ["org_id", "created_at"],
        postgresql_where=sa.text(
            "enriched_at IS NULL AND reviewed_at IS NULL AND owner_user_id IS NULL AND status = 'new'"
        ),
    )

    op.create_table(
        "item_professions",
        sa.Column("org_id", sa.Uuid(), sa.ForeignKey("organizations.id"), nullable=False),
        sa.Column("item_id", sa.Uuid(), nullable=False),
        sa.Column("profession_code", sa.Text(), nullable=False),
        sa.Column("relevance", sa.SmallInteger(), nullable=False),
        sa.PrimaryKeyConstraint(
            "org_id", "item_id", "profession_code", name="item_professions_pkey"
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "item_id"],
            ["items.org_id", "items.id"],
            name="item_professions_item_fkey",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint("relevance BETWEEN 0 AND 100", name="item_professions_relevance"),
        sa.CheckConstraint(
            "profession_code ~ '^[a-z][a-z0-9_]{1,63}$'", name="item_professions_code_check"
        ),
    )
    op.create_index("item_professions_by_code", "item_professions", ["org_id", "profession_code"])

    predicate = "org_id = pharos_current_org()"
    _run(
        [
            "ALTER TABLE item_professions ENABLE ROW LEVEL SECURITY",
            f"CREATE POLICY item_professions_tenant_isolation ON item_professions "
            f"USING ({predicate}) WITH CHECK ({predicate})",
            # Rows of an article inherit its visibility, as item_sources does.
            "CREATE POLICY item_professions_personal_rows ON item_professions AS RESTRICTIVE "  # noqa: S608
            f"FOR SELECT USING (current_user = '{WORKER_ROLE}' OR EXISTS (SELECT 1 FROM items i "
            "WHERE i.org_id = item_professions.org_id AND i.id = item_professions.item_id))",
        ]
    )

    # The one narrow right over an article's state (see the module docstring).
    op.execute(
        """
        CREATE FUNCTION public.pharos_worker_only_holds() RETURNS trigger
        LANGUAGE plpgsql
        SET search_path = pg_catalog, pg_temp
        AS $$
        BEGIN
            IF current_user = 'pharos_worker'
               AND (NEW.status IS DISTINCT FROM OLD.status
                    OR NEW.hold_reason IS DISTINCT FROM OLD.hold_reason
                    OR NEW.summary IS DISTINCT FROM OLD.summary
                    OR NEW.category IS DISTINCT FROM OLD.category
                    OR NEW.quality IS DISTINCT FROM OLD.quality
                    OR NEW.enriched_at IS DISTINCT FROM OLD.enriched_at
                    OR NEW.enrichment_version IS DISTINCT FROM OLD.enrichment_version) THEN
                -- Only an organization article that is new, that no person has decided about and
                -- that the model has not read yet, and then only to write the answer or to hold it
                -- as low_quality.
                IF NOT (OLD.status = 'new' AND OLD.owner_user_id IS NULL
                        AND OLD.enriched_at IS NULL AND OLD.reviewed_at IS NULL
                        AND (NEW.status = 'new'
                             OR (NEW.status = 'held' AND NEW.hold_reason = 'low_quality'))) THEN
                    RAISE EXCEPTION 'the worker may only hold a new article as low_quality, once'
                        USING ERRCODE = '23514';
                END IF;
            END IF;
            RETURN NEW;
        END
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER items_worker_only_holds BEFORE UPDATE OF status, hold_reason, summary, "
        "category, quality, enriched_at, enrichment_version ON items "
        "FOR EACH ROW EXECUTE FUNCTION public.pharos_worker_only_holds()"
    )

    op.execute(f"GRANT SELECT ON item_professions TO {APP_ROLE}")
    op.execute(f"GRANT UPDATE (reviewed_at) ON items TO {APP_ROLE}")
    op.execute(f"GRANT SELECT ON item_professions TO {WORKER_ROLE}")
    op.execute(
        "GRANT INSERT (org_id, item_id, profession_code, relevance) "
        f"ON item_professions TO {WORKER_ROLE}"
    )
    op.execute(
        "GRANT UPDATE (summary, category, quality, enriched_at, enrichment_version, status, "
        f"hold_reason) ON items TO {WORKER_ROLE}"
    )
    op.execute(f"GRANT SELECT (code, scope, owner_org_id) ON professions TO {WORKER_ROLE}")

    code, names = BASIS
    op.execute(
        sa.text(
            "INSERT INTO professions (scope, code, names) "
            "VALUES ('public', :code, CAST(:names AS jsonb))"
        ).bindparams(code=code, names=json.dumps(names, ensure_ascii=False))
    )


def downgrade() -> None:
    op.execute(
        "DELETE FROM professions WHERE scope = 'public' AND code = 'sap_basis_administrator'"
    )
    op.execute(f"REVOKE SELECT (code, scope, owner_org_id) ON professions FROM {WORKER_ROLE}")  # noqa: S608
    op.execute(
        "REVOKE UPDATE (summary, category, quality, enriched_at, enrichment_version, status, "
        f"hold_reason) ON items FROM {WORKER_ROLE}"
    )
    op.execute(f"REVOKE UPDATE (reviewed_at) ON items FROM {APP_ROLE}")
    op.execute("DROP TRIGGER items_worker_only_holds ON items")
    op.execute("DROP FUNCTION public.pharos_worker_only_holds()")
    op.drop_table("item_professions")
    op.drop_index("items_to_enrich", table_name="items")
    op.drop_constraint("items_enrichment_values", "items", type_="check")
    op.drop_constraint("items_enrichment_check", "items", type_="check")
    for column in (
        "reviewed_at",
        "enrichment_version",
        "enriched_at",
        "quality",
        "category",
        "summary",
    ):
        op.drop_column("items", column)
