"""Baseline: RLS helper function and the organizations table.

Revision ID: 0001
Revises:
Create Date: 2026-10-26
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

APP_ROLE = "pharos_app"


def upgrade() -> None:
    # The application role is provisioned by the deployment (it needs a password from the
    # environment), not by a migration. Fail early with a clear message instead of granting
    # nothing and breaking at the first request. A role that can bypass RLS, or a migration run
    # as the application role itself, would make every policy below meaningless.
    op.execute(
        f"""
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '{APP_ROLE}') THEN
                RAISE EXCEPTION 'role % must exist before migrating (see %)',
                    '{APP_ROLE}', 'docs/DATA_MODEL.md#database-roles';
            END IF;
            IF EXISTS (
                SELECT FROM pg_roles
                WHERE rolname = '{APP_ROLE}' AND (rolsuper OR rolbypassrls)
            ) THEN
                RAISE EXCEPTION
                    'role {APP_ROLE} must not be a superuser or have BYPASSRLS';
            END IF;
            IF current_user = '{APP_ROLE}' THEN
                RAISE EXCEPTION
                    'migrations must run as the table owner, not as {APP_ROLE}';
            END IF;
        END
        $$
        """  # noqa: S608 - interpolates a module constant only
    )

    # An unset or emptied app.org_id yields NULL, so no row matches and nothing raises.
    # Names are schema-qualified so a search_path change cannot redirect them.
    op.execute(
        """
        CREATE FUNCTION pharos_current_org() RETURNS uuid
        LANGUAGE sql STABLE
        AS $$ SELECT NULLIF(pg_catalog.current_setting('app.org_id', true), '')::pg_catalog.uuid $$
        """
    )
    op.execute(f"GRANT EXECUTE ON FUNCTION pharos_current_org() TO {APP_ROLE}")

    op.create_table(
        "organizations",
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("slug", sa.Text(), nullable=False, unique=True),
        sa.Column(
            "settings", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.execute("ALTER TABLE organizations ENABLE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY organizations_isolation ON organizations "
        "USING (id = pharos_current_org()) WITH CHECK (id = pharos_current_org())"
    )
    # The application may read its own organization and change only its mutable columns; creating
    # one, or changing its id, slug or creation time, is an operator task.
    op.execute(f"GRANT SELECT ON organizations TO {APP_ROLE}")
    op.execute(f"GRANT UPDATE (name, settings, updated_at) ON organizations TO {APP_ROLE}")
    # /readyz compares the stored revision with the expected head.
    op.execute(f"GRANT SELECT ON alembic_version TO {APP_ROLE}")


def downgrade() -> None:
    op.drop_table("organizations")
    op.execute("DROP FUNCTION pharos_current_org()")
