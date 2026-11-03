"""Embeddings of public articles and clusters of near-duplicates (M4-05, ADR-0007, ADR-0021).

Revision ID: 0009
Revises: 0008
Create Date: 2026-11-03

`item_embeddings` holds one vector per (article, model). The column has no fixed dimension, because
the model is chosen by the operator (ADR-0007) and a model change is a recompute: each row carries
the model and its dimension, and a check keeps the two honest. Queries compare only vectors of the
same model and dimension. Only the worker reads and writes the table (the API never needs a
vector), and only for public articles.

`items.cluster_id` groups near-duplicates. Articles stay separate rows, so a wrong match never
loses content; the Home feed shows one member of a cluster.

`llm_usage` gains the stage `embedding`.

The `vector` extension needs a superuser and is created when the database is created
(deploy/postgres/init/01-roles.sh). If it is missing, this migration stops and says so.
"""

import sqlalchemy as sa
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None

WORKER_ROLE = "pharos_worker"

OLD_STAGES = "('filter', 'enrichment', 'insight', 'key_test')"
NEW_STAGES = "('filter', 'enrichment', 'insight', 'key_test', 'embedding')"


def _run(statements: list[str]) -> None:
    # One statement per call: the asyncpg driver cannot prepare several commands at once.
    for statement in statements:
        op.execute(statement)


def upgrade() -> None:
    present = op.get_bind().execute(sa.text("SELECT 1 FROM pg_extension WHERE extname = 'vector'"))
    if present.first() is None:
        raise RuntimeError(
            "the 'vector' extension is missing. Create it once as the database superuser: "
            "CREATE EXTENSION IF NOT EXISTS vector (deploy/postgres/init/01-roles.sh does this "
            "when a database is first created)"
        )

    op.drop_constraint("llm_usage_stage_check", "llm_usage", type_="check")
    op.create_check_constraint("llm_usage_stage_check", "llm_usage", f"stage IN {NEW_STAGES}")

    op.add_column("items", sa.Column("cluster_id", sa.Uuid(), nullable=True))
    op.create_index(
        "items_by_cluster",
        "items",
        ["org_id", "cluster_id"],
        postgresql_where=sa.text("cluster_id IS NOT NULL"),
    )

    _run(
        [
            "CREATE TABLE item_embeddings ("
            "org_id uuid NOT NULL REFERENCES organizations(id), "
            "item_id uuid NOT NULL, "
            "model text NOT NULL, "
            "dim smallint NOT NULL, "
            "embedding vector NOT NULL, "
            "created_at timestamptz NOT NULL DEFAULT now(), "
            "CONSTRAINT item_embeddings_pkey PRIMARY KEY (org_id, item_id, model), "
            "CONSTRAINT item_embeddings_item_fkey FOREIGN KEY (org_id, item_id) "
            "REFERENCES items (org_id, id) ON DELETE CASCADE, "
            "CONSTRAINT item_embeddings_dim_check CHECK (dim BETWEEN 8 AND 4096), "
            "CONSTRAINT item_embeddings_dims_agree CHECK (vector_dims(embedding) = dim), "
            # pgvector keeps 32-bit floats: a vector whose values are too small for that is stored
            # as zeros, and every similarity to it is "not a number". Such a row is refused.
            "CONSTRAINT item_embeddings_not_zero CHECK (vector_norm(embedding) > 0), "
            "CONSTRAINT item_embeddings_model_check "
            "CHECK (model ~ '^[A-Za-z0-9][A-Za-z0-9._:/@-]{0,99}$'))",
            "CREATE INDEX item_embeddings_by_model ON item_embeddings (org_id, model, created_at)",
        ]
    )

    predicate = "org_id = pharos_current_org()"
    _run(
        [
            "ALTER TABLE item_embeddings ENABLE ROW LEVEL SECURITY",
            "CREATE POLICY item_embeddings_tenant_isolation ON item_embeddings "
            f"USING ({predicate}) WITH CHECK ({predicate})",
        ]
    )

    # Only the worker touches vectors: it reads them to find a neighbour, writes each once and
    # deletes them when a recompute is asked for.
    op.execute(f"GRANT SELECT ON item_embeddings TO {WORKER_ROLE}")
    op.execute(
        f"GRANT INSERT (org_id, item_id, model, dim, embedding) ON item_embeddings TO {WORKER_ROLE}"
    )
    # Deleting is how a recompute starts (a new model, or a new threshold): ADR-0007, ADR-0021.
    op.execute(f"GRANT DELETE ON item_embeddings TO {WORKER_ROLE}")
    op.execute(f"GRANT UPDATE (cluster_id) ON items TO {WORKER_ROLE}")


def downgrade() -> None:
    op.execute(f"REVOKE UPDATE (cluster_id) ON items FROM {WORKER_ROLE}")
    op.execute("DROP TABLE item_embeddings")
    op.drop_index("items_by_cluster", table_name="items")
    op.drop_column("items", "cluster_id")
    # Rows of the stage that is going away cannot stay under the older check.
    op.execute("DELETE FROM llm_usage WHERE stage = 'embedding'")
    op.drop_constraint("llm_usage_stage_check", "llm_usage", type_="check")
    op.create_check_constraint("llm_usage_stage_check", "llm_usage", f"stage IN {OLD_STAGES}")
