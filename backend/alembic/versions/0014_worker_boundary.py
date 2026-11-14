"""The worker cannot release a held article, and a new personal feed waits an hour (M5-05).

Revision ID: 0014
Revises: 0013
Create Date: 2026-11-14

F-33 (ADR-0017 amendment). Migration 0003 let the worker delete any article that is not hidden
(retention) and insert one as `new` or as `held` with any reason (ingestion), so a taken-over worker
could delete a held article and insert it again as `new`, releasing what a rule or a person held.
Two restrictive policies close that:

* the worker deletes only `new` articles: retention never deletes a held one, and nothing else of
  the worker deletes articles;
* an article the worker inserts is `new`, or `held` with one of the three reasons of the ingestion
  rules (`link_ip_host`, `link_odd_port`, `instruction_phrase`): never with the reason of an editor
  or of the model, which only a person or the enrichment update may set.

F-31. A new personal feed was due at once (the default of `next_fetch_at`), so only the limit of ten
new addresses an hour bounded how fast a person could make the worker fetch addresses of their
choice. The application role cannot write `next_fetch_at`, so a trigger does it: a personal feed
the application creates, or whose address it changes, or that it switches on, is first due an hour
later (or later still, if it was already due later). The owner role (the operator, the tests) and
the worker are not affected.
"""

from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None

WORKER_ROLE = "pharos_worker"
APP_ROLE = "pharos_app"
RULE_REASONS = "('link_ip_host', 'link_odd_port', 'instruction_phrase')"


def upgrade() -> None:
    op.execute(
        f"CREATE POLICY items_worker_deletes_only_new ON items AS RESTRICTIVE FOR DELETE "
        f"USING (current_user <> '{WORKER_ROLE}' OR status = 'new')"
    )
    op.execute(
        f"CREATE POLICY items_worker_inserts_new_or_rule_held ON items AS RESTRICTIVE FOR INSERT "
        f"WITH CHECK (current_user <> '{WORKER_ROLE}' OR status = 'new' "
        f"OR (status = 'held' AND hold_reason IN {RULE_REASONS}))"
    )
    op.execute(
        f"""
        CREATE FUNCTION public.pharos_personal_feed_waits() RETURNS trigger
        LANGUAGE plpgsql
        SET search_path = pg_catalog, pg_temp
        AS $$
        BEGIN
            IF current_user = '{APP_ROLE}' AND NEW.owner_user_id IS NOT NULL THEN
                IF TG_OP = 'INSERT' THEN
                    NEW.next_fetch_at := now() + interval '1 hour';
                ELSIF NEW.config IS DISTINCT FROM OLD.config
                      OR (NEW.enabled AND NOT OLD.enabled) THEN
                    NEW.next_fetch_at := greatest(OLD.next_fetch_at, now() + interval '1 hour');
                END IF;
            END IF;
            RETURN NEW;
        END
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER sources_personal_feed_waits BEFORE INSERT OR UPDATE OF config, enabled "
        "ON sources FOR EACH ROW EXECUTE FUNCTION public.pharos_personal_feed_waits()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER sources_personal_feed_waits ON sources")
    op.execute("DROP FUNCTION public.pharos_personal_feed_waits()")
    op.execute("DROP POLICY items_worker_inserts_new_or_rule_held ON items")
    op.execute("DROP POLICY items_worker_deletes_only_new ON items")
