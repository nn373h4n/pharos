"""Identity and profiles: departments, professions, users, local credentials, profiles, audit log.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-27

Every table is under row-level security (ADR-0005). Rows that point at each other carry the
organization in a composite foreign key, because foreign key checks ignore RLS and a plain
reference could point into another organization. The application role gets only the privileges it
needs, by column where it matters: it cannot change a user's organization, issuer or subject, and
the audit log is append-only for it.
"""

import json
import os

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

APP_ROLE = "pharos_app"


# The policy statements are written out here instead of imported from pharos.core.rls: a migration
# that has been applied must never change meaning when application code changes.
def tenant_policy_sql(table: str) -> list[str]:
    predicate = "org_id = pharos_current_org()"
    return [
        f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY",
        f"CREATE POLICY {table}_tenant_isolation ON {table} "
        f"USING ({predicate}) WITH CHECK ({predicate})",
    ]


def content_policy_sql(table: str) -> list[str]:
    owned = "owner_org_id = pharos_current_org()"
    return [
        f"ALTER TABLE {table} ADD CONSTRAINT {table}_scope_owner_check CHECK ("
        "(scope = 'public' AND owner_org_id IS NULL) "
        "OR (scope = 'org' AND owner_org_id IS NOT NULL))",
        f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY",
        f"CREATE POLICY {table}_content_select ON {table} FOR SELECT "
        f"USING (scope = 'public' OR {owned})",
        f"CREATE POLICY {table}_content_insert ON {table} FOR INSERT WITH CHECK ({owned})",
        f"CREATE POLICY {table}_content_update ON {table} FOR UPDATE "
        f"USING ({owned}) WITH CHECK ({owned})",
        f"CREATE POLICY {table}_content_delete ON {table} FOR DELETE USING ({owned})",
    ]


# Generic, widely used professions (names in the four supported languages). A starting taxonomy;
# organizations can add their own later. No company or pilot specifics.
PROFESSIONS: list[tuple[str, dict[str, str]]] = [
    ("accountant", {"en": "Accountant", "az": "Mühasib", "tr": "Muhasebeci", "ru": "Бухгалтер"}),
    (
        "payroll_specialist",
        {
            "en": "Payroll specialist",
            "az": "Əmək haqqı mütəxəssisi",
            "tr": "Bordro uzmanı",
            "ru": "Специалист по расчёту заработной платы",
        },
    ),
    (
        "financial_analyst",
        {
            "en": "Financial analyst",
            "az": "Maliyyə analitiki",
            "tr": "Finansal analist",
            "ru": "Финансовый аналитик",
        },
    ),
    (
        "hr_specialist",
        {
            "en": "HR specialist",
            "az": "İnsan resursları mütəxəssisi",
            "tr": "İnsan kaynakları uzmanı",
            "ru": "Специалист по управлению персоналом",
        },
    ),
    (
        "recruiter",
        {
            "en": "Recruiter",
            "az": "İşə qəbul mütəxəssisi",
            "tr": "İşe alım uzmanı",
            "ru": "Рекрутер",
        },
    ),
    (
        "sap_consultant",
        {
            "en": "SAP consultant",
            "az": "SAP məsləhətçisi",
            "tr": "SAP danışmanı",
            "ru": "SAP-консультант",
        },
    ),
    (
        "software_developer",
        {
            "en": "Software developer",
            "az": "Proqram təminatı tərtibatçısı",
            "tr": "Yazılım geliştirici",
            "ru": "Разработчик программного обеспечения",
        },
    ),
    (
        "data_analyst",
        {
            "en": "Data analyst",
            "az": "Məlumat analitiki",
            "tr": "Veri analisti",
            "ru": "Аналитик данных",
        },
    ),
    (
        "project_manager",
        {
            "en": "Project manager",
            "az": "Layihə meneceri",
            "tr": "Proje yöneticisi",
            "ru": "Менеджер проектов",
        },
    ),
    (
        "team_manager",
        {
            "en": "Team manager",
            "az": "Komanda rəhbəri",
            "tr": "Ekip yöneticisi",
            "ru": "Руководитель команды",
        },
    ),
    (
        "customer_support",
        {
            "en": "Customer support specialist",
            "az": "Müştəri dəstəyi mütəxəssisi",
            "tr": "Müşteri destek uzmanı",
            "ru": "Специалист службы поддержки клиентов",
        },
    ),
    ("other", {"en": "Other", "az": "Digər", "tr": "Diğer", "ru": "Другое"}),
]


def _run(statements: list[str]) -> None:
    # One statement per call: the asyncpg driver cannot prepare several commands at once.
    for statement in statements:
        op.execute(statement)


def upgrade() -> None:
    # --- Preconditions ---------------------------------------------------------------------------
    # The definer function below runs with its owner's rights, so the owner must be the role that
    # owns the tables and not a superuser. The department names are folded with lower(), which only
    # works for non-ASCII letters when the database is not in the C locale.
    op.execute(
        """
        DO $$
        BEGIN
            IF (SELECT rolsuper FROM pg_roles WHERE rolname = current_user) THEN
                RAISE EXCEPTION 'run this migration as the table owner role, not as a superuser';
            END IF;
            IF (SELECT tableowner FROM pg_tables
                WHERE schemaname = 'public' AND tablename = 'organizations') <> current_user THEN
                RAISE EXCEPTION 'run this migration as the owner of public.organizations';
            END IF;
            IF (SELECT datctype FROM pg_database WHERE datname = current_database())
                IN ('C', 'POSIX') THEN
                RAISE EXCEPTION 'the database uses the C locale; use a UTF-8 locale such as en_US.utf8';
            END IF;
        END
        $$
        """
    )

    # --- Organization lookup before sign-in ------------------------------------------------------
    # At sign-in the application does not know the organization yet, and RLS hides every row until
    # `app.org_id` is set. This function answers one question, "which id has this slug?", and
    # nothing else. A view would bypass RLS for every column and every organization.
    op.execute(
        """
        CREATE FUNCTION public.pharos_org_id_for_slug(p_slug text) RETURNS uuid
        LANGUAGE sql STABLE SECURITY DEFINER
        SET search_path = pg_catalog, pg_temp
        AS $$ SELECT id FROM public.organizations WHERE slug = p_slug $$
        """
    )
    op.execute("REVOKE EXECUTE ON FUNCTION pharos_org_id_for_slug(text) FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION pharos_org_id_for_slug(text) TO {APP_ROLE}")

    op.execute(
        """
        CREATE FUNCTION public.pharos_set_updated_at() RETURNS trigger
        LANGUAGE plpgsql
        SET search_path = pg_catalog, pg_temp
        AS $$ BEGIN NEW.updated_at = pg_catalog.now(); RETURN NEW; END $$
        """
    )

    # --- departments -----------------------------------------------------------------------------
    op.create_table(
        "departments",
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column(
            "org_id",
            sa.Uuid(),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.Text(), nullable=False),
        # Names that differ only in case, surrounding white space (space, tab, line breaks and the
        # no-break space) or Unicode form are the same department.
        sa.Column(
            "normalized_name",
            sa.Text(),
            sa.Computed(
                "lower(normalize(btrim(name, ' ' || chr(9) || chr(10) || chr(13) || chr(160)), NFC))",
                persisted=True,
            ),
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint(
            "length(name) <= 200 AND "
            "length(btrim(name, ' ' || chr(9) || chr(10) || chr(13) || chr(160))) >= 1",
            name="departments_name_check",
        ),
        sa.UniqueConstraint("org_id", "normalized_name", name="departments_org_name_key"),
        sa.UniqueConstraint("org_id", "id", name="departments_org_id_id_key"),
    )

    # --- professions (content plane: global rows plus organization-owned rows) -------------------
    op.create_table(
        "professions",
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("scope", sa.Text(), nullable=False, server_default="public"),
        sa.Column(
            "owner_org_id",
            sa.Uuid(),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("code", sa.Text(), nullable=False),
        sa.Column("names", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint("code ~ '^[a-z][a-z0-9_]{1,63}$'", name="professions_code_check"),
        sa.CheckConstraint(
            "jsonb_typeof(names) = 'object' AND jsonb_exists_all(names, ARRAY['en','az','tr','ru']) "
            'AND NOT jsonb_path_exists(names, \'strict $.* ? (@.type() != "string" || @ == "")\')',
            name="professions_names_check",
        ),
        sa.UniqueConstraint(
            "owner_org_id",
            "code",
            name="professions_owner_code_key",
            postgresql_nulls_not_distinct=True,
        ),
    )
    _run(content_policy_sql("professions"))
    # A profession must not move between the global set and an organization after profiles refer
    # to it, so its scope and owner are frozen.
    op.execute(
        """
        CREATE FUNCTION public.pharos_freeze_profession_scope() RETURNS trigger
        LANGUAGE plpgsql
        SET search_path = pg_catalog, pg_temp
        AS $$
        BEGIN
            IF NEW.scope IS DISTINCT FROM OLD.scope OR NEW.owner_org_id IS DISTINCT FROM OLD.owner_org_id THEN
                RAISE EXCEPTION 'the scope and owner of a profession cannot change' USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER professions_freeze_scope BEFORE UPDATE OF scope, owner_org_id ON professions "
        "FOR EACH ROW EXECUTE FUNCTION public.pharos_freeze_profession_scope()"
    )
    # Reading only: global professions are seeded below, organization-owned ones come with the
    # admin panel.
    op.execute(f"GRANT SELECT ON professions TO {APP_ROLE}")
    for code, names in PROFESSIONS:
        op.execute(
            sa.text(
                "INSERT INTO professions (scope, code, names) VALUES ('public', :code, CAST(:names AS jsonb))"
            ).bindparams(code=code, names=json.dumps(names, ensure_ascii=False))
        )

    # --- users -----------------------------------------------------------------------------------
    op.create_table(
        "users",
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column(
            "org_id",
            sa.Uuid(),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # A user is identified by (organization, issuer, subject) and nothing else (ADR-0011).
        # Local accounts use the issuer "local". E-mail is information, never an identity key.
        sa.Column("issuer", sa.Text(), nullable=False),
        sa.Column("subject", sa.Text(), nullable=False),
        sa.Column("email", sa.Text(), nullable=True),
        sa.Column("display_name", sa.Text(), nullable=False, server_default=""),
        sa.Column("role", sa.Text(), nullable=False, server_default="member"),
        sa.Column("status", sa.Text(), nullable=False, server_default="active"),
        sa.Column("department_id", sa.Uuid(), nullable=True),
        sa.Column("department_source", sa.Text(), nullable=True),
        sa.Column("locale", sa.Text(), nullable=True),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        # An OIDC issuer is a URL, so it can never collide with the issuer "local".
        sa.CheckConstraint(
            "length(issuer) BETWEEN 1 AND 512 AND (issuer = 'local' OR issuer ~ '^https?://')",
            name="users_issuer_check",
        ),
        sa.CheckConstraint("length(subject) BETWEEN 1 AND 256", name="users_subject_check"),
        sa.CheckConstraint("email IS NULL OR length(email) <= 320", name="users_email_check"),
        sa.CheckConstraint("length(display_name) <= 200", name="users_display_name_check"),
        sa.CheckConstraint("role IN ('admin', 'editor', 'member')", name="users_role_check"),
        sa.CheckConstraint(
            "status IN ('active', 'disabled', 'deprovisioned')", name="users_status_check"
        ),
        sa.CheckConstraint(
            "department_source IS NULL OR department_source IN ('idp', 'user')",
            name="users_department_source_check",
        ),
        sa.CheckConstraint(
            "locale IS NULL OR locale IN ('en', 'az', 'tr', 'ru')", name="users_locale_check"
        ),
        sa.UniqueConstraint("org_id", "issuer", "subject", name="users_identity_key"),
        sa.UniqueConstraint("org_id", "id", name="users_org_id_id_key"),
        # A user's department must belong to the same organization. (Not enforced while NULL.)
        sa.ForeignKeyConstraint(
            ["org_id", "department_id"],
            ["departments.org_id", "departments.id"],
            name="users_department_fkey",
        ),
    )

    # --- local credentials -----------------------------------------------------------------------
    op.create_table(
        "local_credentials",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column(
            "password_changed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        # The key includes the organization, so a duplicate-key error cannot reveal that a user id
        # exists in another organization.
        sa.PrimaryKeyConstraint("org_id", "user_id", name="local_credentials_pkey"),
        # Only an Argon2id hash may be stored, never a password by mistake.
        sa.CheckConstraint(
            "password_hash LIKE '$argon2id$%' AND length(password_hash) <= 512",
            name="local_credentials_hash_check",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "user_id"],
            ["users.org_id", "users.id"],
            name="local_credentials_user_fkey",
            ondelete="CASCADE",
        ),
    )

    # --- profiles --------------------------------------------------------------------------------
    op.create_table(
        "user_profiles",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column(
            "profession_id",
            sa.Uuid(),
            sa.ForeignKey("professions.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("ai_level", sa.Text(), nullable=True),
        sa.Column(
            "interests",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("onboarding_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("onboarding_completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint(
            "ai_level IS NULL OR ai_level IN ('beginner', 'intermediate', 'advanced')",
            name="user_profiles_ai_level_check",
        ),
        sa.PrimaryKeyConstraint("org_id", "user_id", name="user_profiles_pkey"),
        sa.CheckConstraint(
            "jsonb_typeof(interests) = 'array' AND jsonb_array_length(interests) <= 12 "
            "AND pg_column_size(interests) <= 1024 "
            "AND NOT jsonb_path_exists(interests, 'strict $[*] ? (@.type() != \"string\")')",
            name="user_profiles_interests_check",
        ),
        sa.CheckConstraint(
            "onboarding_completed_at IS NULL OR (onboarding_started_at IS NOT NULL "
            "AND onboarding_completed_at >= onboarding_started_at)",
            name="user_profiles_onboarding_check",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "user_id"],
            ["users.org_id", "users.id"],
            name="user_profiles_user_fkey",
            ondelete="CASCADE",
        ),
    )
    # The profession reference ignores RLS, so a trigger checks that the profession is global or
    # belongs to the profile's own organization.
    op.execute(
        """
        CREATE FUNCTION public.pharos_check_profession_scope() RETURNS trigger
        LANGUAGE plpgsql
        SET search_path = pg_catalog, pg_temp
        AS $$
        BEGIN
            IF NEW.profession_id IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM public.professions p
                WHERE p.id = NEW.profession_id
                  AND (p.scope = 'public' OR p.owner_org_id = NEW.org_id)
            ) THEN
                RAISE EXCEPTION 'profession % is not available to this organization', NEW.profession_id
                    USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER user_profiles_profession_scope BEFORE INSERT OR UPDATE OF profession_id, org_id "
        "ON user_profiles FOR EACH ROW EXECUTE FUNCTION pharos_check_profession_scope()"
    )

    # --- audit log (append-only for the application) ---------------------------------------------
    op.create_table(
        "audit_logs",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column("org_id", sa.Uuid(), sa.ForeignKey("organizations.id"), nullable=False),
        sa.Column(
            "occurred_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("actor_type", sa.Text(), nullable=False),
        # Not a foreign key: the history must survive the removal of a user.
        sa.Column("actor_id", sa.Uuid(), nullable=True),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("target", sa.Text(), nullable=True),
        sa.Column("outcome", sa.Text(), nullable=False),
        sa.Column("ip_hash", sa.Text(), nullable=True),
        sa.Column(
            "details",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.CheckConstraint(
            "actor_type IN ('user', 'local_admin', 'operator_cli', 'system')",
            name="audit_logs_actor_type_check",
        ),
        sa.CheckConstraint("action ~ '^[a-z][a-z0-9_.]{2,63}$'", name="audit_logs_action_check"),
        sa.CheckConstraint(
            "target IS NULL OR length(target) <= 200", name="audit_logs_target_check"
        ),
        sa.CheckConstraint("outcome IN ('success', 'failure')", name="audit_logs_outcome_check"),
        sa.CheckConstraint(
            "ip_hash IS NULL OR length(ip_hash) <= 64", name="audit_logs_ip_hash_check"
        ),
        sa.CheckConstraint(
            "jsonb_typeof(details) = 'object' AND pg_column_size(details) <= 4096",
            name="audit_logs_details_check",
        ),
    )
    op.execute("CREATE INDEX audit_logs_org_time_idx ON audit_logs (org_id, occurred_at DESC)")

    # --- row-level security, updated_at triggers and grants --------------------------------------
    for table in ("departments", "users", "local_credentials", "user_profiles", "audit_logs"):
        _run(tenant_policy_sql(table))
    for table in ("departments", "professions", "users", "user_profiles"):
        op.execute(
            f"CREATE TRIGGER {table}_set_updated_at BEFORE UPDATE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION pharos_set_updated_at()"
        )

    # Column-level privileges (the application cannot touch what it does not need to):
    _run(
        [
            f"GRANT SELECT ON departments, users, local_credentials, user_profiles, audit_logs TO {APP_ROLE}",
            f"GRANT INSERT (org_id, name) ON departments TO {APP_ROLE}",
            # Updating the name to itself lets "INSERT ... ON CONFLICT DO UPDATE ... RETURNING"
            # return the id of an existing department.
            f"GRANT UPDATE (name) ON departments TO {APP_ROLE}",
            f"GRANT INSERT (org_id, issuer, subject, email, display_name, role, status, "
            f"department_id, department_source, locale, last_login_at) ON users TO {APP_ROLE}",
            # Not org_id, issuer or subject: the identity of a user never changes.
            f"GRANT UPDATE (email, display_name, role, status, department_id, department_source, "
            f"locale, last_login_at) ON users TO {APP_ROLE}",
            f"GRANT INSERT (user_id, org_id, password_hash) ON local_credentials TO {APP_ROLE}",
            f"GRANT UPDATE (password_hash, password_changed_at) ON local_credentials TO {APP_ROLE}",
            f"GRANT INSERT (user_id, org_id, profession_id, ai_level, interests, "
            f"onboarding_started_at, onboarding_completed_at) ON user_profiles TO {APP_ROLE}",
            f"GRANT UPDATE (profession_id, ai_level, interests, onboarding_started_at, "
            f"onboarding_completed_at) ON user_profiles TO {APP_ROLE}",
            # Append-only: no UPDATE and no DELETE.
            f"GRANT INSERT (org_id, actor_type, actor_id, action, target, outcome, ip_hash, details) "
            f"ON audit_logs TO {APP_ROLE}",
        ]
    )


def downgrade() -> None:
    # Going back removes every user, credential, profile and the whole audit history. Refuse to do
    # that silently: the operator must say so.
    has_data = (
        op.get_bind()
        .execute(
            sa.text("SELECT EXISTS (SELECT 1 FROM users) OR EXISTS (SELECT 1 FROM audit_logs)")
        )
        .scalar()
    )
    if has_data and os.environ.get("PHAROS_ALLOW_DATA_LOSS") != "yes":
        raise RuntimeError(
            "Downgrading 0002 would delete all users, credentials, profiles and audit entries. "
            "Back up first, then set PHAROS_ALLOW_DATA_LOSS=yes to proceed."
        )
    op.drop_table("audit_logs")
    op.drop_table("user_profiles")
    op.execute("DROP FUNCTION pharos_check_profession_scope()")
    op.drop_table("local_credentials")
    op.drop_table("users")
    op.drop_table("professions")
    op.execute("DROP FUNCTION pharos_freeze_profession_scope()")
    op.drop_table("departments")
    op.execute("DROP FUNCTION pharos_set_updated_at()")
    op.execute("DROP FUNCTION pharos_org_id_for_slug(text)")
