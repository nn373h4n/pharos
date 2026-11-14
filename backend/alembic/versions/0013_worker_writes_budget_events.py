"""The worker writes the two budget events into the audit log (F-39, ADR-0016 amendment).

Revision ID: 0013
Revises: 0012
Create Date: 2026-11-14

The day's model budget is booked by the worker, so only the worker sees the moment it crosses 80%
(`llm.budget_warning`) and the moment a call is refused at 100% (`llm.budget_exhausted`). ADR-0016
says both reach the audit log; until now they reached only the worker's log. The worker may now
insert into `audit_logs`, but only these two actions, as the system, with no actor, target, address
hash or details: a trigger refuses anything else from the worker's role, so a compromised worker
cannot forge an entry that names a person or another action. It still cannot read, change or delete
the audit log. Each event is written at most once a day by an honest worker (the budget's markers);
a compromised one could add more of these two rows, which the audit retention removes in time.
"""

from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None

WORKER_ROLE = "pharos_worker"


def upgrade() -> None:
    op.execute(
        """
        CREATE FUNCTION public.pharos_worker_audits_budget_only() RETURNS trigger
        LANGUAGE plpgsql
        SET search_path = pg_catalog, pg_temp
        AS $$
        BEGIN
            IF current_user = 'pharos_worker'
               AND NOT (NEW.action IN ('llm.budget_warning', 'llm.budget_exhausted')
                        AND NEW.actor_type = 'system' AND NEW.outcome = 'success'
                        AND NEW.actor_id IS NULL AND NEW.target IS NULL AND NEW.ip_hash IS NULL
                        AND NEW.details = '{}'::jsonb) THEN
                RAISE EXCEPTION 'the worker may only write the two budget events'
                    USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER audit_logs_worker_budget_only BEFORE INSERT ON audit_logs "
        "FOR EACH ROW EXECUTE FUNCTION public.pharos_worker_audits_budget_only()"
    )
    op.execute(f"GRANT INSERT (org_id, actor_type, action, outcome) ON audit_logs TO {WORKER_ROLE}")


def downgrade() -> None:
    op.execute(
        f"REVOKE INSERT (org_id, actor_type, action, outcome) ON audit_logs FROM {WORKER_ROLE}"
    )
    op.execute("DROP TRIGGER audit_logs_worker_budget_only ON audit_logs")
    op.execute("DROP FUNCTION public.pharos_worker_audits_budget_only()")
