"""Migration 0002: identity, profiles, audit log. Row-level security, keys, grants, constraints."""

import asyncio
import uuid
from dataclasses import dataclass

import pytest
from alembic import command
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from pharos.core.db import create_session_factory
from pharos.core.rls import tenant_session

from .conftest import APP_ROLE, Orgs, TestDatabase

pytestmark = pytest.mark.integration

TENANT_TABLES = ["departments", "users", "local_credentials", "user_profiles", "audit_logs"]
NEW_TABLES = {*TENANT_TABLES, "professions"}
HASH = "$argon2id$v=19$m=65536,t=3,p=4$c29tZXNhbHQ$ZmFrZWhhc2g"


@dataclass(frozen=True)
class OrgData:
    org: uuid.UUID
    department: uuid.UUID
    user: uuid.UUID


async def make_org_data(connection: AsyncConnection, org: uuid.UUID, label: str) -> OrgData:
    """One department, user, credential, profile and audit entry for an organization (owner role)."""
    department = (
        await connection.execute(
            text("INSERT INTO departments (org_id, name) VALUES (:org, :name) RETURNING id"),
            {"org": org, "name": f"Dept {label}"},
        )
    ).scalar_one()
    user = (
        await connection.execute(
            text(
                "INSERT INTO users (org_id, issuer, subject, department_id) "
                "VALUES (:org, 'local', :subject, :department) RETURNING id"
            ),
            {"org": org, "subject": f"user-{label}", "department": department},
        )
    ).scalar_one()
    await connection.execute(
        text(
            "INSERT INTO local_credentials (user_id, org_id, password_hash) VALUES (:u, :org, :hash)"
        ),
        {"u": user, "org": org, "hash": HASH},
    )
    await connection.execute(
        text("INSERT INTO user_profiles (user_id, org_id) VALUES (:u, :org)"),
        {"u": user, "org": org},
    )
    await connection.execute(
        text(
            "INSERT INTO audit_logs (org_id, actor_type, action, outcome) "
            "VALUES (:org, 'system', 'test.event', 'success')"
        ),
        {"org": org},
    )
    return OrgData(org, department, user)


@pytest.fixture
async def data(owner_engine: AsyncEngine, orgs: Orgs) -> tuple[OrgData, OrgData]:
    async with owner_engine.begin() as connection:
        return (
            await make_org_data(connection, orgs[0], "alpha"),
            await make_org_data(connection, orgs[1], "beta"),
        )


# --- the organization lookup function ------------------------------------------------------------


@pytest.mark.anyio
async def test_slug_resolves_to_an_id_without_any_organization_context(
    app_engine: AsyncEngine, owner_engine: AsyncEngine, orgs: Orgs
) -> None:
    async with owner_engine.connect() as connection:
        slug = (
            await connection.execute(
                text("SELECT slug FROM organizations WHERE id = :id"), {"id": orgs[0]}
            )
        ).scalar_one()
    factory = create_session_factory(app_engine)
    async with factory() as session:
        # Plain session: no app.org_id, so RLS hides every organization row...
        assert (await session.execute(text("SELECT count(*) FROM organizations"))).scalar_one() == 0
        # ...but the function still answers, for exactly this question.
        found = (
            await session.execute(text("SELECT pharos_org_id_for_slug(:s)"), {"s": slug})
        ).scalar_one()
        unknown = (
            await session.execute(text("SELECT pharos_org_id_for_slug('nope')"))
        ).scalar_one()
    assert found == orgs[0]
    assert unknown is None


@pytest.mark.anyio
async def test_the_lookup_function_is_locked_down(owner_engine: AsyncEngine) -> None:
    async with owner_engine.connect() as connection:
        row = (
            await connection.execute(
                text(
                    "SELECT prosecdef, proconfig, proacl::text, has_function_privilege(:app, oid, 'EXECUTE') "
                    "FROM pg_proc WHERE proname = 'pharos_org_id_for_slug'"
                ),
                {"app": APP_ROLE},
            )
        ).one()
    security_definer, config, acl, app_can_execute = row
    assert security_definer is True
    assert config == ["search_path=pg_catalog, pg_temp"]
    assert app_can_execute is True
    assert "{=X" not in acl and ",=X" not in acl  # nothing granted to PUBLIC


# --- row-level security on every new table -------------------------------------------------------


@pytest.mark.anyio
@pytest.mark.parametrize("table", TENANT_TABLES)
async def test_each_organization_sees_only_its_own_rows(
    app_engine: AsyncEngine, data: tuple[OrgData, OrgData], table: str
) -> None:
    alpha, beta = data
    factory = create_session_factory(app_engine)
    for current, other in ((alpha, beta), (beta, alpha)):
        async with tenant_session(factory, current.org) as session:
            orgs_seen = (
                (await session.execute(text(f"SELECT DISTINCT org_id FROM {table}")))
                .scalars()
                .all()
            )
            assert orgs_seen == [current.org], f"{table} leaks rows of {other.org}"
    async with factory() as session:
        assert (await session.execute(text(f"SELECT count(*) FROM {table}"))).scalar_one() == 0


@pytest.mark.anyio
async def test_an_organization_cannot_write_rows_for_another(
    app_engine: AsyncEngine, data: tuple[OrgData, OrgData]
) -> None:
    alpha, beta = data
    factory = create_session_factory(app_engine)
    statements = [
        "INSERT INTO departments (org_id, name) VALUES (:other, 'Planted')",
        "INSERT INTO users (org_id, issuer, subject) VALUES (:other, 'local', 'planted')",
        "INSERT INTO audit_logs (org_id, actor_type, action, outcome) "
        "VALUES (:other, 'system', 'planted.event', 'success')",
    ]
    for statement in statements:
        with pytest.raises(DBAPIError, match="row-level security"):
            async with tenant_session(factory, alpha.org) as session:
                await session.execute(text(statement), {"other": beta.org})


# --- keys that cannot cross organizations --------------------------------------------------------


@pytest.mark.anyio
async def test_foreign_keys_cannot_point_into_another_organization(
    owner_engine: AsyncEngine, data: tuple[OrgData, OrgData]
) -> None:
    # The owner bypasses RLS, so only the keys can stop these. The target user has no credential
    # or profile yet, so a duplicate-key error cannot hide the foreign-key error.
    alpha, beta = data
    async with owner_engine.begin() as connection:
        beta_user = (
            await connection.execute(
                text(
                    "INSERT INTO users (org_id, issuer, subject) "
                    "VALUES (:org, 'local', 'bare') RETURNING id"
                ),
                {"org": beta.org},
            )
        ).scalar_one()
    cases = {
        "a user with another organization's department": (
            "INSERT INTO users (org_id, issuer, subject, department_id) VALUES (:alpha, 'local', 'x', :beta_dept)"
        ),
        "a credential for another organization's user": (
            "INSERT INTO local_credentials (user_id, org_id, password_hash) VALUES (:beta_user, :alpha, :hash)"
        ),
        "a profile for another organization's user": (
            "INSERT INTO user_profiles (user_id, org_id) VALUES (:beta_user, :alpha)"
        ),
    }
    for description, statement in cases.items():
        with pytest.raises(IntegrityError, match="foreign key"):
            async with owner_engine.begin() as connection:
                await connection.execute(
                    text(statement),
                    {
                        "alpha": alpha.org,
                        "beta_dept": beta.department,
                        "beta_user": beta_user,
                        "hash": HASH,
                    },
                )
        assert description  # keeps the failing case readable in the report


# --- what the application role may and may not do -----------------------------------------------


@pytest.mark.anyio
@pytest.mark.parametrize("column", ["org_id", "issuer", "subject"])
async def test_a_users_identity_cannot_be_changed(
    app_engine: AsyncEngine, data: tuple[OrgData, OrgData], column: str
) -> None:
    alpha, beta = data
    value = {"org_id": str(beta.org), "issuer": "https://evil.test", "subject": "taken-over"}[
        column
    ]
    cast = "uuid" if column == "org_id" else "text"
    factory = create_session_factory(app_engine)
    with pytest.raises(DBAPIError, match="permission denied"):
        async with tenant_session(factory, alpha.org) as session:
            await session.execute(
                text(f"UPDATE users SET {column} = CAST(:v AS {cast}) WHERE id = :id"),
                {"v": value, "id": alpha.user},
            )


@pytest.mark.anyio
async def test_the_application_can_manage_users_within_its_limits(
    app_engine: AsyncEngine, data: tuple[OrgData, OrgData]
) -> None:
    alpha, _ = data
    factory = create_session_factory(app_engine)
    async with tenant_session(factory, alpha.org) as session:
        new_id = (
            await session.execute(
                text(
                    "INSERT INTO users (org_id, issuer, subject, email, display_name) "
                    "VALUES (:org, 'https://idp.test', 's1', 'a@example.test', 'A') RETURNING id"
                ),
                {"org": alpha.org},
            )
        ).scalar_one()
        await session.execute(
            text("UPDATE users SET role = 'editor', status = 'disabled' WHERE id = :id"),
            {"id": new_id},
        )
        await session.execute(
            text("UPDATE local_credentials SET password_hash = :hash WHERE user_id = :id"),
            {"id": alpha.user, "hash": HASH + "2"},
        )
    async with tenant_session(factory, alpha.org) as session:
        row = (
            await session.execute(
                text("SELECT role, status FROM users WHERE id = :id"), {"id": new_id}
            )
        ).one()
    assert tuple(row) == ("editor", "disabled")


@pytest.mark.anyio
async def test_users_and_credentials_cannot_be_deleted_by_the_application(
    app_engine: AsyncEngine, data: tuple[OrgData, OrgData]
) -> None:
    alpha, _ = data
    factory = create_session_factory(app_engine)
    for statement in (
        "DELETE FROM users WHERE id = :id",
        "DELETE FROM local_credentials WHERE user_id = :id",
    ):
        with pytest.raises(DBAPIError, match="permission denied"):
            async with tenant_session(factory, alpha.org) as session:
                await session.execute(text(statement), {"id": alpha.user})


@pytest.mark.anyio
async def test_the_audit_log_is_append_only_for_the_application(
    app_engine: AsyncEngine, data: tuple[OrgData, OrgData]
) -> None:
    alpha, _ = data
    factory = create_session_factory(app_engine)
    async with tenant_session(factory, alpha.org) as session:
        await session.execute(
            text(
                "INSERT INTO audit_logs (org_id, actor_type, actor_id, action, target, outcome, ip_hash, details) "
                "VALUES (:org, 'user', :actor, 'user.login', 'session', 'success', 'abc', CAST('{\"k\": 1}' AS jsonb))"
            ),
            {"org": alpha.org, "actor": alpha.user},
        )
    for statement in (
        "UPDATE audit_logs SET outcome = 'failure'",
        "DELETE FROM audit_logs",
        "TRUNCATE audit_logs",
    ):
        with pytest.raises(DBAPIError, match="permission denied"):
            async with tenant_session(factory, alpha.org) as session:
                await session.execute(text(statement))
    async with tenant_session(factory, alpha.org) as session:
        count = (await session.execute(text("SELECT count(*) FROM audit_logs"))).scalar_one()
    assert count == 2  # the fixture's entry and the one just written


@pytest.mark.anyio
async def test_updated_at_moves_on_update(
    app_engine: AsyncEngine, data: tuple[OrgData, OrgData]
) -> None:
    alpha, _ = data
    factory = create_session_factory(app_engine)
    async with tenant_session(factory, alpha.org) as session:
        before = (
            await session.execute(
                text("SELECT updated_at FROM users WHERE id = :id"), {"id": alpha.user}
            )
        ).scalar_one()
    await asyncio.sleep(0.05)
    async with tenant_session(factory, alpha.org) as session:
        await session.execute(
            text("UPDATE users SET display_name = 'Renamed' WHERE id = :id"), {"id": alpha.user}
        )
    async with tenant_session(factory, alpha.org) as session:
        after = (
            await session.execute(
                text("SELECT updated_at FROM users WHERE id = :id"), {"id": alpha.user}
            )
        ).scalar_one()
    assert after > before


# --- departments ---------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_department_names_that_differ_only_in_form_are_the_same_department(
    owner_engine: AsyncEngine, orgs: Orgs
) -> None:
    alpha, beta = orgs
    async with owner_engine.begin() as connection:
        await connection.execute(
            text("INSERT INTO departments (org_id, name) VALUES (:org, 'Café Team')"),
            {"org": alpha},
        )
    for variant in ("  café team ", "CAFÉ TEAM", "Café Team"):  # last: e + combining accent
        with pytest.raises(IntegrityError, match="departments_org_name_key"):
            async with owner_engine.begin() as connection:
                await connection.execute(
                    text("INSERT INTO departments (org_id, name) VALUES (:org, :name)"),
                    {"org": alpha, "name": variant},
                )
    async with owner_engine.begin() as connection:  # another organization may use the same name
        await connection.execute(
            text("INSERT INTO departments (org_id, name) VALUES (:org, 'Café Team')"), {"org": beta}
        )


@pytest.mark.anyio
async def test_department_upsert_returns_the_existing_id(
    app_engine: AsyncEngine, data: tuple[OrgData, OrgData]
) -> None:
    alpha, _ = data
    upsert = text(
        "INSERT INTO departments (org_id, name) VALUES (:org, :name) "
        "ON CONFLICT (org_id, normalized_name) DO UPDATE SET name = departments.name RETURNING id"
    )
    factory = create_session_factory(app_engine)
    async with tenant_session(factory, alpha.org) as session:
        first = (await session.execute(upsert, {"org": alpha.org, "name": "Treasury"})).scalar_one()
        again = (
            await session.execute(upsert, {"org": alpha.org, "name": " TREASURY "})
        ).scalar_one()
        existing = (
            await session.execute(upsert, {"org": alpha.org, "name": "dept alpha"})
        ).scalar_one()
    assert first == again
    assert existing == alpha.department


# --- professions ---------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_the_seeded_professions_are_global_and_have_all_four_languages(
    app_engine: AsyncEngine, orgs: Orgs
) -> None:
    factory = create_session_factory(app_engine)
    for org in (orgs[0], orgs[1]):
        async with tenant_session(factory, org) as session:
            rows = (
                await session.execute(
                    text("SELECT code, names, scope, owner_org_id FROM professions")
                )
            ).all()
        assert len(rows) >= 12
        assert len({row.code for row in rows}) == len(rows)
        for row in rows:
            assert row.scope == "public" and row.owner_org_id is None
            assert set(row.names) == {"en", "az", "tr", "ru"}
            assert all(isinstance(name, str) and name.strip() for name in row.names.values())
    async with factory() as session:  # even without an organization (the onboarding list is public)
        assert (await session.execute(text("SELECT count(*) FROM professions"))).scalar_one() >= 12


@pytest.mark.anyio
async def test_the_application_cannot_change_the_global_professions(
    app_engine: AsyncEngine, orgs: Orgs
) -> None:
    factory = create_session_factory(app_engine)
    names = '{"en": "x", "az": "x", "tr": "x", "ru": "x"}'
    # The application role has no INSERT privilege on professions at all (stronger than RLS alone).
    with pytest.raises(DBAPIError, match="permission denied"):
        async with tenant_session(factory, orgs[0]) as session:
            await session.execute(
                text(
                    "INSERT INTO professions (scope, code, names) "
                    "VALUES ('public', 'planted', CAST(:n AS jsonb))"
                ),
                {"n": names},
            )
    with pytest.raises(DBAPIError, match="permission denied"):
        async with tenant_session(factory, orgs[0]) as session:
            await session.execute(text("UPDATE professions SET code = 'hijacked'"))
    with pytest.raises(DBAPIError, match="permission denied"):
        async with tenant_session(factory, orgs[0]) as session:
            await session.execute(text("DELETE FROM professions"))


@pytest.mark.anyio
async def test_a_profile_may_only_use_global_or_own_organization_professions(
    app_engine: AsyncEngine, owner_engine: AsyncEngine, data: tuple[OrgData, OrgData]
) -> None:
    alpha, beta = data
    async with owner_engine.begin() as connection:
        alpha_only = (
            await connection.execute(
                text(
                    "INSERT INTO professions (scope, owner_org_id, code, names) VALUES "
                    "('org', :org, 'alpha_only', CAST(:n AS jsonb)) RETURNING id"
                ),
                {"org": alpha.org, "n": '{"en": "A", "az": "A", "tr": "A", "ru": "A"}'},
            )
        ).scalar_one()
        global_id = (
            await connection.execute(text("SELECT id FROM professions WHERE code = 'accountant'"))
        ).scalar_one()
    factory = create_session_factory(app_engine)

    async with tenant_session(factory, beta.org) as session:  # invisible to the other organization
        assert (
            await session.execute(
                text("SELECT count(*) FROM professions WHERE code = 'alpha_only'")
            )
        ).scalar_one() == 0
    async with tenant_session(factory, alpha.org) as session:
        assert (
            await session.execute(
                text("SELECT count(*) FROM professions WHERE code = 'alpha_only'")
            )
        ).scalar_one() == 1

    update = text("UPDATE user_profiles SET profession_id = :p WHERE user_id = :u")
    with pytest.raises(DBAPIError, match="not available"):
        async with tenant_session(factory, beta.org) as session:
            await session.execute(update, {"p": alpha_only, "u": beta.user})
    # The application role is stopped by RLS before the trigger matters. The owner is not subject
    # to RLS, so here only the trigger can refuse another organization's profession.
    with pytest.raises(DBAPIError, match="not available"):
        async with owner_engine.begin() as connection:
            await connection.execute(update, {"p": alpha_only, "u": beta.user})
    async with tenant_session(factory, beta.org) as session:
        await session.execute(update, {"p": global_id, "u": beta.user})
    async with tenant_session(factory, alpha.org) as session:
        await session.execute(update, {"p": alpha_only, "u": alpha.user})
        await session.execute(
            text("UPDATE user_profiles SET profession_id = NULL WHERE user_id = :u"),
            {"u": alpha.user},
        )


# --- value constraints ---------------------------------------------------------------------------

LONG = "a" * 10


def ascii_url(length: int) -> str:
    return "https://" + "a" * (length - len("https://"))


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("statement", "constraint"),
    [
        ("UPDATE users SET role = 'owner'", "users_role_check"),
        ("UPDATE users SET status = 'banned'", "users_status_check"),
        ("UPDATE users SET locale = 'de'", "users_locale_check"),
        ("UPDATE users SET department_source = 'guess'", "users_department_source_check"),
        ("UPDATE users SET email = repeat('a', 321)", "users_email_check"),
        ("UPDATE users SET issuer = ''", "users_issuer_check"),
        ("UPDATE users SET issuer = 'ldap://directory'", "users_issuer_check"),
        ("UPDATE users SET issuer = 'LOCAL'", "users_issuer_check"),
        ("UPDATE users SET subject = ''", "users_subject_check"),
        ("UPDATE user_profiles SET ai_level = 'expert'", "user_profiles_ai_level_check"),
        (
            "UPDATE user_profiles SET interests = '{\"a\": 1}'::jsonb",
            "user_profiles_interests_check",
        ),
        ("UPDATE user_profiles SET interests = '[1, 2]'::jsonb", "user_profiles_interests_check"),
        (
            "UPDATE user_profiles SET interests = '[[\"x\"]]'::jsonb",
            "user_profiles_interests_check",
        ),
        (
            "UPDATE user_profiles SET interests = (SELECT jsonb_agg(i::text) FROM generate_series(1, 13) i)",
            "user_profiles_interests_check",
        ),
        (
            "UPDATE user_profiles SET interests = jsonb_build_array(repeat('x', 2000))",
            "user_profiles_interests_check",
        ),
        (
            "UPDATE user_profiles SET onboarding_completed_at = now()",
            "user_profiles_onboarding_check",
        ),
        (
            "UPDATE user_profiles SET onboarding_started_at = now(), "
            "onboarding_completed_at = now() - interval '1 hour'",
            "user_profiles_onboarding_check",
        ),
        (
            "UPDATE local_credentials SET password_hash = 'plaintext-password'",
            "local_credentials_hash_check",
        ),
        (
            "UPDATE local_credentials SET password_hash = '$argon2i$v=19$x'",
            "local_credentials_hash_check",
        ),
        ("UPDATE audit_logs SET actor_type = 'robot'", "audit_logs_actor_type_check"),
        ("UPDATE audit_logs SET action = 'Bad Action'", "audit_logs_action_check"),
        ("UPDATE audit_logs SET action = 'ab'", "audit_logs_action_check"),
        ("UPDATE audit_logs SET outcome = 'maybe'", "audit_logs_outcome_check"),
        ("UPDATE audit_logs SET details = '[]'::jsonb", "audit_logs_details_check"),
        (
            "UPDATE audit_logs SET details = jsonb_build_object('k', repeat('x', 5000))",
            "audit_logs_details_check",
        ),
        ("UPDATE audit_logs SET target = repeat('t', 201)", "audit_logs_target_check"),
        ("UPDATE audit_logs SET ip_hash = repeat('h', 65)", "audit_logs_ip_hash_check"),
        ("UPDATE departments SET name = '   '", "departments_name_check"),
        ("UPDATE departments SET name = E'\\t\\n'", "departments_name_check"),
        ("UPDATE departments SET name = repeat('d', 201)", "departments_name_check"),
        (
            'UPDATE professions SET names = \'{"en": "only english"}\'::jsonb',
            "professions_names_check",
        ),
        (
            'UPDATE professions SET names = \'{"en": "a", "az": "a", "tr": "a", "ru": ""}\'::jsonb',
            "professions_names_check",
        ),
        (
            'UPDATE professions SET names = \'{"en": "a", "az": "a", "tr": "a", "ru": 1}\'::jsonb',
            "professions_names_check",
        ),
        (
            "UPDATE professions SET names = "
            '\'{"en": "a", "az": "a", "tr": "a", "ru": ["nested"]}\'::jsonb',
            "professions_names_check",
        ),
        ("UPDATE professions SET code = 'Bad Code'", "professions_code_check"),
    ],
)
async def test_invalid_values_are_rejected_by_the_named_constraint(
    owner_engine: AsyncEngine, data: tuple[OrgData, OrgData], statement: str, constraint: str
) -> None:
    with pytest.raises(IntegrityError, match=constraint):
        async with owner_engine.begin() as connection:
            await connection.execute(text(statement))


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("column", "limit"),
    [("issuer", 512), ("subject", 256), ("email", 320), ("display_name", 200)],
)
async def test_user_text_limits_accept_the_limit_and_reject_one_more(
    owner_engine: AsyncEngine, data: tuple[OrgData, OrgData], column: str, limit: int
) -> None:
    alpha, _ = data

    def value(length: int) -> str:
        return ascii_url(length) if column == "issuer" else "a" * length

    async with owner_engine.begin() as connection:
        await connection.execute(
            text(f"UPDATE users SET {column} = :v WHERE id = :id"),
            {"v": value(limit), "id": alpha.user},
        )
    with pytest.raises(IntegrityError, match=f"users_{column}_check"):
        async with owner_engine.begin() as connection:
            await connection.execute(
                text(f"UPDATE users SET {column} = :v WHERE id = :id"),
                {"v": value(limit + 1), "id": alpha.user},
            )


@pytest.mark.anyio
async def test_department_names_ignore_all_surrounding_white_space_and_have_a_length_limit(
    owner_engine: AsyncEngine, orgs: Orgs
) -> None:
    alpha, _ = orgs
    async with owner_engine.begin() as connection:
        await connection.execute(
            text("INSERT INTO departments (org_id, name) VALUES (:org, 'Finance')"), {"org": alpha}
        )
        await connection.execute(
            text("INSERT INTO departments (org_id, name) VALUES (:org, :name)"),
            {"org": alpha, "name": "d" * 200},
        )
    for variant in ("Finance\t", "\nFinance\r\n", "\u00a0Finance\u00a0", " \tFINANCE\t "):
        with pytest.raises(IntegrityError, match="departments_org_name_key"):
            async with owner_engine.begin() as connection:
                await connection.execute(
                    text("INSERT INTO departments (org_id, name) VALUES (:org, :name)"),
                    {"org": alpha, "name": variant},
                )
    with pytest.raises(IntegrityError, match="departments_name_check"):
        async with owner_engine.begin() as connection:
            await connection.execute(
                text("INSERT INTO departments (org_id, name) VALUES (:org, :name)"),
                {"org": alpha, "name": "Finance" + " " * 10_000_000},
            )


# --- upserts, races and the existence oracle ----------------------------------------------------


@pytest.mark.anyio
async def test_user_upsert_by_identity_returns_one_user_and_keeps_role_and_status(
    app_engine: AsyncEngine, owner_engine: AsyncEngine, data: tuple[OrgData, OrgData]
) -> None:
    alpha, _ = data
    upsert = text(
        "INSERT INTO users (org_id, issuer, subject, email, display_name) "
        "VALUES (:org, 'https://idp.test', 'sub-1', :email, :name) "
        "ON CONFLICT (org_id, issuer, subject) DO UPDATE "
        "SET email = EXCLUDED.email, display_name = EXCLUDED.display_name, last_login_at = now() "
        "RETURNING id"
    )
    factory = create_session_factory(app_engine)
    async with tenant_session(factory, alpha.org) as session:
        first = (
            await session.execute(upsert, {"org": alpha.org, "email": "a@x.test", "name": "A"})
        ).scalar_one()
    async with owner_engine.begin() as connection:
        await connection.execute(
            text("UPDATE users SET role = 'editor', status = 'disabled' WHERE id = :id"),
            {"id": first},
        )
    async with tenant_session(factory, alpha.org) as session:
        again = (
            await session.execute(upsert, {"org": alpha.org, "email": "b@x.test", "name": "B"})
        ).scalar_one()
        row = (
            await session.execute(
                text("SELECT email, display_name, role, status FROM users WHERE id = :id"),
                {"id": first},
            )
        ).one()
    assert again == first
    assert tuple(row) == (
        "b@x.test",
        "B",
        "editor",
        "disabled",
    )  # the upsert did not touch role or status


@pytest.mark.anyio
async def test_parallel_first_sign_ins_create_one_user_and_one_department(
    app_engine: AsyncEngine, data: tuple[OrgData, OrgData]
) -> None:
    alpha, _ = data
    factory = create_session_factory(app_engine)

    async def sign_in() -> tuple[uuid.UUID, uuid.UUID]:
        async with tenant_session(factory, alpha.org) as session:
            department = (
                await session.execute(
                    text(
                        "INSERT INTO departments (org_id, name) VALUES (:org, 'Parallel Dept') "
                        "ON CONFLICT (org_id, normalized_name) DO UPDATE SET name = departments.name RETURNING id"
                    ),
                    {"org": alpha.org},
                )
            ).scalar_one()
            user = (
                await session.execute(
                    text(
                        "INSERT INTO users (org_id, issuer, subject, department_id) "
                        "VALUES (:org, 'https://idp.test', 'parallel', :dept) "
                        "ON CONFLICT (org_id, issuer, subject) DO UPDATE SET last_login_at = now() RETURNING id"
                    ),
                    {"org": alpha.org, "dept": department},
                )
            ).scalar_one()
            return department, user

    results = await asyncio.gather(*(sign_in() for _ in range(8)))
    assert len({department for department, _ in results}) == 1
    assert len({user for _, user in results}) == 1


@pytest.mark.anyio
async def test_foreign_key_errors_do_not_reveal_whether_an_id_exists_in_another_organization(
    app_engine: AsyncEngine, data: tuple[OrgData, OrgData]
) -> None:
    alpha, beta = data
    factory = create_session_factory(app_engine)
    messages = []
    for user_id in (beta.user, uuid.uuid4()):  # a real user of another organization, a random id
        with pytest.raises(DBAPIError, match="foreign key") as caught:
            async with tenant_session(factory, alpha.org) as session:
                await session.execute(
                    text(
                        "INSERT INTO local_credentials (user_id, org_id, password_hash) "
                        "VALUES (:u, :org, :hash)"
                    ),
                    {"u": user_id, "org": alpha.org, "hash": HASH},
                )
        messages.append(str(caught.value.orig).split("DETAIL")[0])
    assert messages[0] == messages[1]


@pytest.mark.anyio
async def test_a_profile_cannot_be_created_with_a_profession_of_another_organization(
    app_engine: AsyncEngine, owner_engine: AsyncEngine, data: tuple[OrgData, OrgData]
) -> None:
    alpha, beta = data
    async with owner_engine.begin() as connection:
        alpha_only = (
            await connection.execute(
                text(
                    "INSERT INTO professions (scope, owner_org_id, code, names) VALUES "
                    "('org', :org, 'alpha_private', CAST(:n AS jsonb)) RETURNING id"
                ),
                {"org": alpha.org, "n": '{"en": "A", "az": "A", "tr": "A", "ru": "A"}'},
            )
        ).scalar_one()
        bare = (
            await connection.execute(
                text(
                    "INSERT INTO users (org_id, issuer, subject) VALUES (:org, 'local', 'bare2') RETURNING id"
                ),
                {"org": beta.org},
            )
        ).scalar_one()
    factory = create_session_factory(app_engine)
    errors = []
    for profession in (
        alpha_only,
        uuid.uuid4(),
    ):  # another organization's, and one that does not exist
        with pytest.raises(DBAPIError, match="not available") as caught:
            async with tenant_session(factory, beta.org) as session:
                await session.execute(
                    text(
                        "INSERT INTO user_profiles (user_id, org_id, profession_id) VALUES (:u, :org, :p)"
                    ),
                    {"u": bare, "org": beta.org, "p": profession},
                )
        errors.append(str(caught.value.orig).split(" is not available")[1])
    assert errors[0] == errors[1]


@pytest.mark.anyio
async def test_a_profession_cannot_change_scope_and_deleting_it_clears_profiles(
    owner_engine: AsyncEngine, data: tuple[OrgData, OrgData]
) -> None:
    alpha, _ = data
    async with owner_engine.begin() as connection:
        profession = (
            await connection.execute(
                text(
                    "INSERT INTO professions (scope, owner_org_id, code, names) VALUES "
                    "('org', :org, 'temp_role', CAST(:n AS jsonb)) RETURNING id"
                ),
                {"org": alpha.org, "n": '{"en": "T", "az": "T", "tr": "T", "ru": "T"}'},
            )
        ).scalar_one()
        await connection.execute(
            text("UPDATE user_profiles SET profession_id = :p WHERE user_id = :u"),
            {"p": profession, "u": alpha.user},
        )
    for statement in (
        "UPDATE professions SET scope = 'public', owner_org_id = NULL WHERE code = 'temp_role'",
        "UPDATE professions SET owner_org_id = (SELECT id FROM organizations WHERE id <> :org LIMIT 1) WHERE code = 'temp_role'",
        "UPDATE professions SET scope = 'org', owner_org_id = :org WHERE code = 'accountant'",
    ):
        with pytest.raises(DBAPIError, match="cannot change"):
            async with owner_engine.begin() as connection:
                await connection.execute(text(statement), {"org": alpha.org})
    async with owner_engine.begin() as connection:
        await connection.execute(text("DELETE FROM professions WHERE id = :p"), {"p": profession})
        cleared = (
            await connection.execute(
                text("SELECT profession_id FROM user_profiles WHERE user_id = :u"),
                {"u": alpha.user},
            )
        ).scalar_one()
    assert cleared is None


# --- privileges, functions and the migration itself ---------------------------------------------


async def privilege_snapshot(engine: AsyncEngine) -> dict[str, set[tuple[str, ...]]]:
    """Everything the application role may do in the public schema, as plain data."""
    app = "(SELECT oid FROM pg_roles WHERE rolname = :app)"
    async with engine.connect() as connection:
        tables = (
            await connection.execute(
                text(
                    "SELECT c.relname, c.relkind::text, x.privilege_type FROM pg_class c "
                    "CROSS JOIN LATERAL aclexplode(c.relacl) x "
                    f"WHERE c.relnamespace = 'public'::regnamespace AND x.grantee = {app}"
                ),
                {"app": APP_ROLE},
            )
        ).all()
        columns = (
            await connection.execute(
                text(
                    "SELECT c.relname, a.attname, x.privilege_type FROM pg_attribute a "
                    "JOIN pg_class c ON c.oid = a.attrelid "
                    "CROSS JOIN LATERAL aclexplode(a.attacl) x "
                    f"WHERE c.relnamespace = 'public'::regnamespace AND x.grantee = {app}"
                ),
                {"app": APP_ROLE},
            )
        ).all()
        functions = (
            await connection.execute(
                text(
                    # Functions the application may call although PUBLIC may not. (A NULL ACL means
                    # the default, which lets PUBLIC execute, as trigger functions do.)
                    "SELECT p.proname FROM pg_proc p "
                    "WHERE p.pronamespace = 'public'::regnamespace "
                    "AND p.proacl IS NOT NULL "
                    "AND has_function_privilege(:app, p.oid, 'EXECUTE') "
                    "AND NOT EXISTS (SELECT 1 FROM aclexplode(p.proacl) x WHERE x.grantee = 0)"
                ),
                {"app": APP_ROLE},
            )
        ).all()
    return {
        "tables": {tuple(row) for row in tables},
        "columns": {tuple(row) for row in columns},
        "functions": {(row[0],) for row in functions},
    }


EXPECTED_TABLE_PRIVILEGES = {
    (table, "r", "SELECT")
    for table in (
        "organizations",
        "alembic_version",
        "departments",
        "professions",
        "users",
        "local_credentials",
        "user_profiles",
        "audit_logs",
        # Migration 0003: the sources the organization follows and what came from them.
        "sources",
        "fetch_runs",
        "items",
        "item_sources",
        # Migration 0005: the record of model calls, read for the budget page.
        "llm_usage",
        # Migration 0006: how relevant an article is to each profession.
        "item_professions",
        # Migration 0007: how an article applies to a profession, in a language.
        "item_insights",
    )
} | {("sources", "r", "DELETE")}
EXPECTED_COLUMN_PRIVILEGES = (
    {("organizations", c, "UPDATE") for c in ("name", "settings", "updated_at")}
    | {("departments", c, "INSERT") for c in ("org_id", "name")}
    | {("departments", "name", "UPDATE")}
    | {
        ("users", c, "INSERT")
        for c in (
            "org_id",
            "issuer",
            "subject",
            "email",
            "display_name",
            "role",
            "status",
            "department_id",
            "department_source",
            "locale",
            "last_login_at",
        )
    }
    | {
        ("users", c, "UPDATE")
        for c in (
            "email",
            "display_name",
            "role",
            "status",
            "department_id",
            "department_source",
            "locale",
            "last_login_at",
        )
    }
    | {("local_credentials", c, "INSERT") for c in ("user_id", "org_id", "password_hash")}
    | {("local_credentials", c, "UPDATE") for c in ("password_hash", "password_changed_at")}
    | {
        ("user_profiles", c, "INSERT")
        for c in (
            "user_id",
            "org_id",
            "profession_id",
            "ai_level",
            "interests",
            "onboarding_started_at",
            "onboarding_completed_at",
        )
    }
    | {
        ("user_profiles", c, "UPDATE")
        for c in (
            "profession_id",
            "ai_level",
            "interests",
            "onboarding_started_at",
            "onboarding_completed_at",
        )
    }
    | {
        ("sources", c, "INSERT")
        for c in (
            "org_id",
            "owner_user_id",
            "type",
            "name",
            "config",
            "dedupe_key",
            "enabled",
            "interval_seconds",
            "created_by",
        )
    }
    | {
        ("sources", c, "UPDATE")
        for c in ("name", "config", "dedupe_key", "enabled", "interval_seconds")
    }
    | {("items", c, "UPDATE") for c in ("status", "hold_reason", "reviewed_at")}
    | {
        ("audit_logs", c, "INSERT")
        for c in (
            "org_id",
            "actor_type",
            "actor_id",
            "action",
            "target",
            "outcome",
            "ip_hash",
            "details",
        )
    }
)


@pytest.mark.anyio
async def test_the_application_role_has_exactly_the_intended_privileges(
    owner_engine: AsyncEngine,
) -> None:
    snapshot = await privilege_snapshot(owner_engine)
    assert snapshot["tables"] == EXPECTED_TABLE_PRIVILEGES  # no privilege on any sequence either
    assert snapshot["columns"] == EXPECTED_COLUMN_PRIVILEGES
    assert snapshot["functions"] == {("pharos_org_id_for_slug",)}


@pytest.mark.anyio
async def test_the_helper_functions_live_in_public_and_belong_to_the_table_owner(
    owner_engine: AsyncEngine,
) -> None:
    async with owner_engine.connect() as connection:
        rows = (
            await connection.execute(
                text(
                    "SELECT p.proname, n.nspname, r.rolname, r.rolsuper FROM pg_proc p "
                    "JOIN pg_namespace n ON n.oid = p.pronamespace "
                    "JOIN pg_roles r ON r.oid = p.proowner WHERE p.proname LIKE 'pharos\\_%'"
                )
            )
        ).all()
        table_owner = (
            await connection.execute(
                text("SELECT tableowner FROM pg_tables WHERE tablename = 'organizations'")
            )
        ).scalar_one()
    assert {row.proname for row in rows} >= {
        "pharos_current_org",
        "pharos_org_id_for_slug",
        "pharos_set_updated_at",
        "pharos_check_profession_scope",
        "pharos_freeze_profession_scope",
    }
    for row in rows:
        assert (row.nspname, row.rolname, row.rolsuper) == ("public", table_owner, False)


@pytest.mark.anyio
async def test_downgrade_refuses_to_delete_data_unless_told_to(
    database: TestDatabase,
    owner_engine: AsyncEngine,
    data: tuple[OrgData, OrgData],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("PHAROS_ALLOW_DATA_LOSS", raising=False)
    with pytest.raises(RuntimeError, match="PHAROS_ALLOW_DATA_LOSS"):
        await asyncio.to_thread(command.downgrade, database.alembic, "0001")
    async with owner_engine.connect() as connection:
        remaining = (await connection.execute(text("SELECT count(*) FROM users"))).scalar_one()
        revision = (
            await connection.execute(text("SELECT version_num FROM alembic_version"))
        ).scalar_one()
    assert remaining == 2 and revision == "0007"

    monkeypatch.setenv("PHAROS_ALLOW_DATA_LOSS", "yes")
    try:
        await asyncio.to_thread(command.downgrade, database.alembic, "0001")
    finally:
        monkeypatch.delenv("PHAROS_ALLOW_DATA_LOSS")
        await asyncio.to_thread(command.upgrade, database.alembic, "head")
    async with owner_engine.connect() as connection:
        assert (await connection.execute(text("SELECT count(*) FROM users"))).scalar_one() == 0


@pytest.mark.anyio
async def test_migration_0002_can_be_reverted_and_applied_again(
    database: TestDatabase, owner_engine: AsyncEngine
) -> None:
    async def tables() -> set[str]:
        async with owner_engine.connect() as connection:
            rows = await connection.execute(
                text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
            )
            return {row[0] for row in rows}

    async def functions() -> set[str]:
        async with owner_engine.connect() as connection:
            rows = await connection.execute(
                text("SELECT proname FROM pg_proc WHERE proname LIKE 'pharos\\_%'")
            )
            return {row[0] for row in rows}

    before = await privilege_snapshot(owner_engine)
    assert await tables() >= NEW_TABLES
    try:
        await asyncio.to_thread(command.downgrade, database.alembic, "0001")
        remaining = await tables()
        assert not (NEW_TABLES & remaining) and "organizations" in remaining
        assert await functions() == {"pharos_current_org"}
    finally:
        await asyncio.to_thread(command.upgrade, database.alembic, "head")
    assert await tables() >= NEW_TABLES
    assert (
        await privilege_snapshot(owner_engine) == before
    )  # policies and grants come back identical
    async with owner_engine.connect() as connection:
        seeded = (await connection.execute(text("SELECT count(*) FROM professions"))).scalar_one()
    assert seeded >= 12
