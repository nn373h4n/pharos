"""The AI tools an administrator approves for "Open in" on a card (M5-07, ADR-0026).

Revision ID: 0010
Revises: 0009
Create Date: 2026-11-04

An administrator keeps a short list (at most ten) of AI tools, each a name and an https address. The
"Try this" panel of a card offers a link to each of them next to "Copy". The list is empty by
default, and then a card offers "Copy" only. The server never opens the address: the person's
browser does, so this is not a way for the server to reach anywhere.

Tenant isolation is the usual row-level policy. Only the application role touches the table (the
worker holds nothing on it). The checks on the name and the address are repeated here as a second
line behind the application's checks: a name of 1 to 40 characters without control characters, an
address of at most 300 characters that starts with `https://`, has no white space, control
character, fragment or `@` (no credentials), and a host that is not a bare IP address. At most ten
tools per organization, enforced by a trigger under an advisory lock so two requests at once cannot
pass it.
"""

import os

import sqlalchemy as sa
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None

APP_ROLE = "pharos_app"
MAX_TOOLS = 10


def upgrade() -> None:
    op.create_table(
        "ai_tools",
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column(
            "org_id",
            sa.Uuid(),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        # No foreign key, as for sources: the history must survive the removal of a person.
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        # Written without POSIX classes such as [:space:]: SQLAlchemy would read ":space" as a bind
        # parameter. \s, \x01-\x1f and \u200b are regular-expression escapes that Postgres understands;
        # the ranges are the control, no-break, zero-width, bidirectional and separator characters.
        sa.CheckConstraint(
            r"char_length(btrim(name)) BETWEEN 1 AND 40 AND name !~ '[\x01-\x1f\x7f-\xa0\u200b-\u200f\u2028-\u202e\u2060-\u206f\ufeff]'",
            name="ai_tools_name_check",
        ),
        sa.CheckConstraint(
            r"char_length(url) <= 300 "
            r"AND url ~ '^https://[^\s\x01-\x1f\x7f-\xa0\u200b-\u200f\u2028-\u202e\u2060-\u206f\ufeff/?#@]+([/?][^\s\x01-\x1f\x7f-\xa0\u200b-\u200f\u2028-\u202e\u2060-\u206f\ufeff#@]*)?$' "
            # A bare IP address in any common spelling (dotted; a number or hex as the LAST label,
            # which is how a browser reads it) or any bracket (an IPv6 or IPvFuture literal).
            r"AND url !~ '^https://([0-9]{1,3}[.]){3}[0-9]{1,3}([:/?]|$)' "
            r"AND url !~ '^https://([^/?:]*[.])?(0[xX][0-9a-fA-F]*|[0-9]+)[.]?([:/?]|$)' "
            r"AND url !~ '^https://[^/?]*[[]' "
            # "localhost" and every "*.localhost" are the person's own machine.
            r"AND url !~* '^https://([^/?:]*[.])?localhost[.]?([:/?]|$)'",
            name="ai_tools_url_check",
        ),
    )
    # The same tool name, in any case or Unicode form, once per organization.
    op.execute(
        "CREATE UNIQUE INDEX ai_tools_name_unique ON ai_tools "
        "(org_id, lower(normalize(btrim(name), NFC)))"
    )

    op.execute("ALTER TABLE ai_tools ENABLE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY ai_tools_tenant_isolation ON ai_tools "
        "USING (org_id = pharos_current_org()) WITH CHECK (org_id = pharos_current_org())"
    )
    op.execute(
        "CREATE TRIGGER ai_tools_set_updated_at BEFORE UPDATE ON ai_tools "
        "FOR EACH ROW EXECUTE FUNCTION pharos_set_updated_at()"
    )

    # At most ten per organization. The lock makes two parallel requests count one after the other.
    op.execute(
        """
        CREATE FUNCTION public.pharos_limit_ai_tools() RETURNS trigger
        LANGUAGE plpgsql
        SET search_path = pg_catalog, pg_temp
        AS $$
        BEGIN
            PERFORM pg_catalog.pg_advisory_xact_lock(
                pg_catalog.hashtextextended('ai_tools:' || NEW.org_id::pg_catalog.text, 0));
            IF (SELECT pg_catalog.count(*) FROM public.ai_tools t
                 WHERE t.org_id = NEW.org_id) >= __MAX__
            THEN
                RAISE EXCEPTION 'at most __MAX__ approved AI tools per organization'
                    USING ERRCODE = 'check_violation';
            END IF;
            RETURN NEW;
        END
        $$
        """.replace("__MAX__", str(MAX_TOOLS))
    )
    op.execute(
        "CREATE TRIGGER ai_tools_limit BEFORE INSERT ON ai_tools "
        "FOR EACH ROW EXECUTE FUNCTION pharos_limit_ai_tools()"
    )

    # Reading and deleting are plain; the application may change only what an administrator edits.
    op.execute(f"GRANT SELECT, INSERT, DELETE ON ai_tools TO {APP_ROLE}")
    op.execute(f"GRANT UPDATE (name, url, updated_at) ON ai_tools TO {APP_ROLE}")


def downgrade() -> None:
    has_data = op.get_bind().execute(sa.text("SELECT EXISTS (SELECT 1 FROM ai_tools)")).scalar()
    if has_data and os.environ.get("PHAROS_ALLOW_DATA_LOSS") != "yes":
        raise RuntimeError(
            "Downgrading 0010 would delete the approved AI tools. "
            "Back up first, then set PHAROS_ALLOW_DATA_LOSS=yes to proceed."
        )
    op.drop_table("ai_tools")
    op.execute("DROP FUNCTION public.pharos_limit_ai_tools()")
