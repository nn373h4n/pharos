"""Sources and ingestion: sources, fetch runs, raw items, items, item sources.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-29

All five tables belong to an organization and are under ordinary tenant row-level security
(ADR-0005, ADR-0012): there are no shared `public` rows and no role that writes across
organizations. A source or an item may also belong to one person (`owner_user_id`, personal feeds);
for the application role a restrictive policy hides those rows from everybody else, administrators
included, unless the transaction names that person in `app.user_id`. The worker role is the process
that handles hostile content, so its grants are column-level and cover these tables only: it can read
nothing about people, credentials, profiles or the audit log (`tests/integration/test_worker_role.py`).
"""

import os

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

APP_ROLE = "pharos_app"
WORKER_ROLE = "pharos_worker"

TABLES = ("sources", "fetch_runs", "raw_items", "items", "item_sources")
SOURCE_TYPES = "('rss', 'github', 'hackernews')"
RUN_OUTCOMES = (
    "('running', 'ok', 'not_modified', 'blocked', 'timeout', 'too_large', "
    "'bad_status', 'bad_content', 'network', 'error')"
)
ITEM_STATUSES = "('new', 'held', 'hidden')"


# Written out here, not imported from pharos.core.rls: an applied migration must never change
# meaning when application code changes.
def tenant_policy_sql(table: str) -> list[str]:
    predicate = "org_id = pharos_current_org()"
    return [
        f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY",
        f"CREATE POLICY {table}_tenant_isolation ON {table} "
        f"USING ({predicate}) WITH CHECK ({predicate})",
    ]


def _run(statements: list[str]) -> None:
    # One statement per call: the asyncpg driver cannot prepare several commands at once.
    for statement in statements:
        op.execute(statement)


def _timestamp(name: str, *, nullable: bool = False, default: bool = True) -> sa.Column[object]:
    return sa.Column(
        name,
        sa.DateTime(timezone=True),
        nullable=nullable,
        server_default=sa.func.now() if default else None,
    )


def upgrade() -> None:
    # --- Preconditions ---------------------------------------------------------------------------
    op.execute(
        f"""
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '{WORKER_ROLE}') THEN
                RAISE EXCEPTION 'role % must exist before migrating (see %)',
                    '{WORKER_ROLE}', 'docs/DEPLOYMENT.md, Upgrading to M3';
            END IF;
            IF EXISTS (
                SELECT FROM pg_roles
                WHERE rolname = '{WORKER_ROLE}'
                  AND (rolsuper OR rolbypassrls OR rolcreaterole OR rolcreatedb)
            ) THEN
                RAISE EXCEPTION 'role {WORKER_ROLE} must have no special attributes';
            END IF;
            IF current_user = '{WORKER_ROLE}' THEN
                RAISE EXCEPTION 'migrations must run as the table owner, not as {WORKER_ROLE}';
            END IF;
        END
        $$
        """  # noqa: S608 - interpolates module constants only
    )

    # The person a personal row belongs to, named by the application for the transaction. Unset
    # means "nobody": personal rows stay hidden.
    op.execute(
        """
        CREATE FUNCTION public.pharos_current_user() RETURNS uuid
        LANGUAGE sql STABLE
        AS $$ SELECT NULLIF(pg_catalog.current_setting('app.user_id', true), '')::pg_catalog.uuid $$
        """
    )
    _run(
        [
            f"GRANT EXECUTE ON FUNCTION pharos_current_user() TO {APP_ROLE}",
            # The tenant policies call this one; do not depend on the default PUBLIC execute right.
            f"GRANT EXECUTE ON FUNCTION pharos_current_org() TO {WORKER_ROLE}",
            # The worker finds its organization from the configured slug and reads nothing else.
            f"GRANT EXECUTE ON FUNCTION pharos_org_id_for_slug(text) TO {WORKER_ROLE}",
        ]
    )

    # --- sources ---------------------------------------------------------------------------------
    op.create_table(
        "sources",
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("org_id", sa.Uuid(), sa.ForeignKey("organizations.id"), nullable=False),
        # Set for a personal feed; NULL for a source the organization's editors manage.
        sa.Column("owner_user_id", sa.Uuid(), nullable=True),
        sa.Column("type", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column(
            "config", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        # Same type and configuration, same key: adding a source twice is refused.
        sa.Column("dedupe_key", sa.Text(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("interval_seconds", sa.Integer(), nullable=False, server_default="3600"),
        _timestamp("next_fetch_at"),
        # A lease says "this fetch is running"; it expires on its own if the worker dies.
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_token", sa.Uuid(), nullable=True),
        sa.Column("etag", sa.Text(), nullable=True),
        sa.Column("last_modified", sa.Text(), nullable=True),
        sa.Column("consecutive_failures", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error_code", sa.Text(), nullable=True),
        _timestamp("last_fetched_at", nullable=True, default=False),
        _timestamp("last_success_at", nullable=True, default=False),
        # Who created it. Not a foreign key: the history must survive the removal of a user.
        sa.Column("created_by", sa.Uuid(), nullable=True),
        _timestamp("created_at"),
        _timestamp("updated_at"),
        sa.UniqueConstraint("org_id", "id", name="sources_org_id_id_key"),
        sa.ForeignKeyConstraint(
            ["org_id", "owner_user_id"],
            ["users.org_id", "users.id"],
            name="sources_owner_fkey",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(f"type IN {SOURCE_TYPES}", name="sources_type_check"),
        sa.CheckConstraint("char_length(name) BETWEEN 1 AND 200", name="sources_name_check"),
        sa.CheckConstraint(
            "jsonb_typeof(config) = 'object' AND pg_column_size(config) <= 8192",
            name="sources_config_check",
        ),
        sa.CheckConstraint("char_length(dedupe_key) BETWEEN 1 AND 128", name="sources_key_check"),
        sa.CheckConstraint(
            "interval_seconds BETWEEN 300 AND 604800", name="sources_interval_check"
        ),
        # A person's own feed is fetched at most hourly, whatever the application says.
        sa.CheckConstraint(
            "owner_user_id IS NULL OR interval_seconds >= 3600",
            name="sources_personal_interval_check",
        ),
        sa.CheckConstraint("consecutive_failures >= 0", name="sources_failures_check"),
        sa.CheckConstraint(
            "(lease_until IS NULL) = (lease_token IS NULL)", name="sources_lease_check"
        ),
        sa.CheckConstraint(
            "(etag IS NULL OR char_length(etag) <= 512) "
            "AND (last_modified IS NULL OR char_length(last_modified) <= 128)",
            name="sources_validators_check",
        ),
        sa.CheckConstraint(
            "last_error_code IS NULL OR last_error_code ~ '^[a-z_]{1,64}$'",
            name="sources_error_code_check",
        ),
    )
    op.create_index(
        "sources_dedupe_key",
        "sources",
        ["org_id", "owner_user_id", "dedupe_key"],
        unique=True,
        postgresql_nulls_not_distinct=True,
    )
    op.create_index(
        "sources_due", "sources", ["next_fetch_at"], postgresql_where=sa.text("enabled")
    )

    # --- fetch_runs ------------------------------------------------------------------------------
    op.create_table(
        "fetch_runs",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column("org_id", sa.Uuid(), sa.ForeignKey("organizations.id"), nullable=False),
        sa.Column("source_id", sa.Uuid(), nullable=False),
        _timestamp("started_at"),
        _timestamp("finished_at", nullable=True, default=False),
        sa.Column("outcome", sa.Text(), nullable=False, server_default="running"),
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.Column("http_status", sa.SmallInteger(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("bytes_fetched", sa.Integer(), nullable=True),
        sa.Column("items_seen", sa.Integer(), nullable=True),
        sa.Column("items_new", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(
            ["org_id", "source_id"],
            ["sources.org_id", "sources.id"],
            name="fetch_runs_source_fkey",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(f"outcome IN {RUN_OUTCOMES}", name="fetch_runs_outcome_check"),
        sa.CheckConstraint(
            "error_code IS NULL OR error_code ~ '^[a-z_]{1,64}$'", name="fetch_runs_error_check"
        ),
        sa.CheckConstraint(
            "http_status IS NULL OR http_status BETWEEN 100 AND 599", name="fetch_runs_status_check"
        ),
        sa.CheckConstraint(
            "coalesce(duration_ms, 0) >= 0 AND coalesce(bytes_fetched, 0) >= 0 "
            "AND coalesce(items_seen, 0) >= 0 AND coalesce(items_new, 0) >= 0",
            name="fetch_runs_counts_check",
        ),
    )
    op.create_index("fetch_runs_by_source", "fetch_runs", ["org_id", "source_id", "started_at"])
    op.create_index("fetch_runs_by_age", "fetch_runs", ["started_at"])

    # --- raw_items -------------------------------------------------------------------------------
    # What a source said, after cleaning and truncation, kept briefly for the pipeline and for
    # debugging. Never the article page.
    op.create_table(
        "raw_items",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column("org_id", sa.Uuid(), sa.ForeignKey("organizations.id"), nullable=False),
        sa.Column("source_id", sa.Uuid(), nullable=False),
        sa.Column("external_id", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False, server_default=""),
        sa.Column("excerpt", sa.Text(), nullable=False, server_default=""),
        sa.Column("url", sa.Text(), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        _timestamp("fetched_at"),
        sa.Column("content_sha256", sa.Text(), nullable=False),
        _timestamp("processed_at", nullable=True, default=False),
        sa.UniqueConstraint("org_id", "source_id", "external_id", name="raw_items_external_key"),
        sa.ForeignKeyConstraint(
            ["org_id", "source_id"],
            ["sources.org_id", "sources.id"],
            name="raw_items_source_fkey",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "char_length(external_id) BETWEEN 1 AND 512", name="raw_items_external_check"
        ),
        sa.CheckConstraint("char_length(title) <= 300", name="raw_items_title_check"),
        sa.CheckConstraint("char_length(excerpt) <= 500", name="raw_items_excerpt_check"),
        sa.CheckConstraint(
            "url IS NULL OR (char_length(url) <= 2048 "
            "AND url ~* '^https?://[^[:space:][:cntrl:]<>\"]+$')",
            name="raw_items_url_check",
        ),
        sa.CheckConstraint("content_sha256 ~ '^[0-9a-f]{64}$'", name="raw_items_hash_check"),
    )
    op.create_index("raw_items_by_age", "raw_items", ["fetched_at"])

    # --- items -----------------------------------------------------------------------------------
    op.create_table(
        "items",
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("org_id", sa.Uuid(), sa.ForeignKey("organizations.id"), nullable=False),
        # Set for an item that came from a personal feed: never merged into an organization item.
        sa.Column("owner_user_id", sa.Uuid(), nullable=True),
        sa.Column("canonical_url", sa.Text(), nullable=False),
        sa.Column("url_hash", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("excerpt", sa.Text(), nullable=False, server_default=""),
        sa.Column("lang", sa.Text(), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        _timestamp("fetched_at"),
        sa.Column("content_sha256", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="new"),
        sa.Column("hold_reason", sa.Text(), nullable=True),
        _timestamp("created_at"),
        _timestamp("updated_at"),
        sa.UniqueConstraint("org_id", "id", name="items_org_id_id_key"),
        sa.ForeignKeyConstraint(
            ["org_id", "owner_user_id"],
            ["users.org_id", "users.id"],
            name="items_owner_fkey",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "char_length(canonical_url) <= 2048 "
            "AND canonical_url ~* '^https?://[^[:space:][:cntrl:]<>\"]+$'",
            name="items_url_check",
        ),
        sa.CheckConstraint("url_hash ~ '^[0-9a-f]{64}$'", name="items_hash_check"),
        sa.CheckConstraint("content_sha256 ~ '^[0-9a-f]{64}$'", name="items_content_hash_check"),
        sa.CheckConstraint("char_length(title) BETWEEN 1 AND 300", name="items_title_check"),
        sa.CheckConstraint("char_length(excerpt) <= 500", name="items_excerpt_check"),
        sa.CheckConstraint("lang IS NULL OR lang ~ '^[a-z]{2,3}$'", name="items_lang_check"),
        sa.CheckConstraint(f"status IN {ITEM_STATUSES}", name="items_status_check"),
        sa.CheckConstraint(
            "(status = 'held') = (hold_reason IS NOT NULL) "
            "AND (hold_reason IS NULL OR hold_reason ~ '^[a-z_]{1,64}$')",
            name="items_hold_check",
        ),
    )
    op.create_index(
        "items_url_key",
        "items",
        ["org_id", "owner_user_id", "url_hash"],
        unique=True,
        postgresql_nulls_not_distinct=True,
    )
    op.create_index("items_listing", "items", ["org_id", "status", "published_at"])
    op.create_index("items_by_age", "items", ["fetched_at"])

    # --- item_sources ----------------------------------------------------------------------------
    # One article seen in several sources is one item with several rows here.
    op.create_table(
        "item_sources",
        sa.Column("org_id", sa.Uuid(), sa.ForeignKey("organizations.id"), nullable=False),
        sa.Column("item_id", sa.Uuid(), nullable=False),
        sa.Column("source_id", sa.Uuid(), nullable=False),
        _timestamp("first_seen_at"),
        sa.PrimaryKeyConstraint("org_id", "item_id", "source_id", name="item_sources_pkey"),
        sa.ForeignKeyConstraint(
            ["org_id", "item_id"],
            ["items.org_id", "items.id"],
            name="item_sources_item_fkey",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "source_id"],
            ["sources.org_id", "sources.id"],
            name="item_sources_source_fkey",
            ondelete="CASCADE",
        ),
    )
    op.create_index("item_sources_by_source", "item_sources", ["org_id", "source_id"])

    # A personal item may only be linked to a source of the same person, and an organization item
    # only to an organization source: otherwise what somebody follows could leak into shared items.
    op.execute(
        """
        CREATE FUNCTION public.pharos_check_item_source_owner() RETURNS trigger
        LANGUAGE plpgsql
        SET search_path = pg_catalog, pg_temp
        AS $$
        BEGIN
            IF (SELECT i.owner_user_id FROM public.items i
                 WHERE i.org_id = NEW.org_id AND i.id = NEW.item_id)
               IS DISTINCT FROM
               (SELECT s.owner_user_id FROM public.sources s
                 WHERE s.org_id = NEW.org_id AND s.id = NEW.source_id)
            THEN
                RAISE EXCEPTION 'item and source must belong to the same owner'
                    USING ERRCODE = 'check_violation';
            END IF;
            RETURN NEW;
        END
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER item_sources_owner_check BEFORE INSERT OR UPDATE ON item_sources "
        "FOR EACH ROW EXECUTE FUNCTION pharos_check_item_source_owner()"
    )

    for table in ("sources", "items"):
        op.execute(
            f"CREATE TRIGGER {table}_set_updated_at BEFORE UPDATE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION pharos_set_updated_at()"
        )

    # --- Row-level security ----------------------------------------------------------------------
    for table in TABLES:
        _run(tenant_policy_sql(table))
    # Personal rows are the business of one person. A restrictive policy (it narrows what the tenant
    # policy allows) shows them only to the person named in app.user_id. It applies to every role,
    # so a role added later does not see them by accident; the worker is exempt because it must
    # process every feed.
    visible = (
        "owner_user_id IS NULL OR owner_user_id = pharos_current_user() "
        f"OR current_user = '{WORKER_ROLE}'"
    )
    for table in ("sources", "items"):
        op.execute(
            f"CREATE POLICY {table}_personal_rows ON {table} AS RESTRICTIVE FOR ALL "
            f"USING ({visible}) WITH CHECK ({visible})"
        )
    # Rows that hang off a source or an item inherit its visibility: the subquery runs under the
    # same restrictive policy, so a personal feed's runs and links are hidden with it.
    for table, parent, key in (
        ("fetch_runs", "sources", "source_id"),
        ("item_sources", "items", "item_id"),
    ):
        op.execute(
            f"CREATE POLICY {table}_personal_rows ON {table} AS RESTRICTIVE FOR SELECT "  # noqa: S608
            f"USING (current_user = '{WORKER_ROLE}' OR EXISTS (SELECT 1 FROM {parent} p "
            f"WHERE p.org_id = {table}.org_id AND p.id = {table}.{key}))"
        )
    # Attribution cannot be forged: a source is credited to nobody or to the person making the
    # request.
    op.execute(
        "CREATE POLICY sources_created_by ON sources AS RESTRICTIVE FOR INSERT "
        "WITH CHECK (created_by IS NULL OR created_by = pharos_current_user())"
    )
    # The worker cannot undo a person's decision: a hidden item stays (it also remembers that the
    # article was rejected), and a fetch never creates an item that is already hidden.
    op.execute(
        f"CREATE POLICY items_worker_keeps_hidden_delete ON items AS RESTRICTIVE FOR DELETE "
        f"USING (current_user <> '{WORKER_ROLE}' OR status <> 'hidden')"
    )
    op.execute(
        f"CREATE POLICY items_worker_keeps_hidden_insert ON items AS RESTRICTIVE FOR INSERT "
        f"WITH CHECK (current_user <> '{WORKER_ROLE}' OR status IN ('new', 'held'))"
    )

    # A person may follow at most five feeds of their own. The lock makes two parallel requests
    # count one after the other.
    op.execute(
        """
        CREATE FUNCTION public.pharos_limit_personal_sources() RETURNS trigger
        LANGUAGE plpgsql
        SET search_path = pg_catalog, pg_temp
        AS $$
        BEGIN
            IF NEW.owner_user_id IS NOT NULL THEN
                PERFORM pg_catalog.pg_advisory_xact_lock(
                    pg_catalog.hashtextextended(NEW.owner_user_id::pg_catalog.text, 3));
                IF (SELECT pg_catalog.count(*) FROM public.sources s
                     WHERE s.org_id = NEW.org_id AND s.owner_user_id = NEW.owner_user_id) >= 5
                THEN
                    RAISE EXCEPTION 'at most five personal sources per person'
                        USING ERRCODE = 'check_violation';
                END IF;
            END IF;
            RETURN NEW;
        END
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER sources_limit_personal BEFORE INSERT ON sources "
        "FOR EACH ROW EXECUTE FUNCTION pharos_limit_personal_sources()"
    )

    # --- Privileges, by column -------------------------------------------------------------------
    source_columns_for_worker = (
        "id, org_id, owner_user_id, type, name, config, dedupe_key, enabled, interval_seconds, "
        "next_fetch_at, lease_until, lease_token, etag, last_modified, consecutive_failures, "
        "last_error_code, last_fetched_at, last_success_at, created_at, updated_at"
    )  # everything but created_by: the pipeline has no use for who set a source up
    _run(
        [
            # The application (administrators and editors through the API).
            f"GRANT SELECT ON sources, fetch_runs, items, item_sources TO {APP_ROLE}",
            f"GRANT INSERT (org_id, owner_user_id, type, name, config, dedupe_key, enabled, "
            f"interval_seconds, created_by) ON sources TO {APP_ROLE}",
            # "Fetch now" is a worker task started by the API, not a write of next_fetch_at: a
            # person must not be able to make the worker fetch their URL as often as they like.
            f"GRANT UPDATE (name, config, dedupe_key, enabled, interval_seconds) "
            f"ON sources TO {APP_ROLE}",
            f"GRANT DELETE ON sources TO {APP_ROLE}",
            f"GRANT UPDATE (status, hold_reason) ON items TO {APP_ROLE}",
            # The worker: what the pipeline needs and nothing about people.
            f"GRANT SELECT ({source_columns_for_worker}) ON sources TO {WORKER_ROLE}",
            f"GRANT SELECT ON fetch_runs, raw_items, items, item_sources TO {WORKER_ROLE}",
            f"GRANT UPDATE (next_fetch_at, lease_until, lease_token, etag, last_modified, "
            f"consecutive_failures, last_error_code, last_fetched_at, last_success_at) "
            f"ON sources TO {WORKER_ROLE}",
            f"GRANT INSERT (org_id, source_id, outcome) ON fetch_runs TO {WORKER_ROLE}",
            f"GRANT UPDATE (finished_at, outcome, error_code, http_status, duration_ms, "
            f"bytes_fetched, items_seen, items_new) ON fetch_runs TO {WORKER_ROLE}",
            f"GRANT DELETE ON fetch_runs, raw_items, items TO {WORKER_ROLE}",
            f"GRANT INSERT (org_id, source_id, external_id, title, excerpt, url, published_at, "
            f"content_sha256) ON raw_items TO {WORKER_ROLE}",
            f"GRANT UPDATE (processed_at) ON raw_items TO {WORKER_ROLE}",
            f"GRANT INSERT (org_id, owner_user_id, canonical_url, url_hash, title, excerpt, lang, "
            f"published_at, content_sha256, status, hold_reason) ON items TO {WORKER_ROLE}",
            # A person's decision (hidden, approved) is never overwritten by a later fetch.
            f"GRANT UPDATE (title, excerpt, lang, published_at, content_sha256) "
            f"ON items TO {WORKER_ROLE}",
            f"GRANT INSERT (org_id, item_id, source_id) ON item_sources TO {WORKER_ROLE}",
        ]
    )


def downgrade() -> None:
    has_data = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT EXISTS (SELECT 1 FROM sources) OR EXISTS (SELECT 1 FROM items) "
                "OR EXISTS (SELECT 1 FROM raw_items)"
            )
        )
        .scalar()
    )
    if has_data and os.environ.get("PHAROS_ALLOW_DATA_LOSS") != "yes":
        raise RuntimeError(
            "Downgrading 0003 would delete all sources, fetched items and fetch history. "
            "Back up first, then set PHAROS_ALLOW_DATA_LOSS=yes to proceed."
        )
    op.drop_table("item_sources")
    op.execute("DROP FUNCTION pharos_check_item_source_owner()")
    op.drop_table("items")
    op.drop_table("raw_items")
    op.drop_table("fetch_runs")
    op.drop_table("sources")
    op.execute("DROP FUNCTION pharos_limit_personal_sources()")
    op.execute("DROP FUNCTION pharos_current_user()")
    op.execute(f"REVOKE EXECUTE ON FUNCTION pharos_org_id_for_slug(text) FROM {WORKER_ROLE}")
    op.execute(f"REVOKE EXECUTE ON FUNCTION pharos_current_org() FROM {WORKER_ROLE}")
