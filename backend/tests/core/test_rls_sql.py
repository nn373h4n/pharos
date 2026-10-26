import pytest

from pharos.core.rls import content_policy_sql, tenant_policy_sql


def test_tenant_policy_enables_rls_and_filters_on_the_org_column() -> None:
    statements = tenant_policy_sql("interactions")
    assert statements[0] == "ALTER TABLE interactions ENABLE ROW LEVEL SECURITY"
    policy = statements[1]
    assert "CREATE POLICY interactions_tenant_isolation ON interactions" in policy
    assert "USING (org_id = pharos_current_org())" in policy
    assert "WITH CHECK (org_id = pharos_current_org())" in policy


def test_tenant_policy_accepts_a_custom_org_column() -> None:
    assert "owner_org = pharos_current_org()" in tenant_policy_sql("t", "owner_org")[1]


def test_content_policy_ties_scope_to_owner() -> None:
    check = content_policy_sql("items")[0]
    assert "scope = 'public' AND owner_org_id IS NULL" in check
    assert "scope = 'org' AND owner_org_id IS NOT NULL" in check
    assert content_policy_sql("items")[1] == "ALTER TABLE items ENABLE ROW LEVEL SECURITY"


def test_content_policy_lets_everyone_read_public_rows_but_nobody_write_them() -> None:
    policies = {s.split()[2]: s for s in content_policy_sql("items") if s.startswith("CREATE")}
    assert set(policies) == {
        "items_content_select",
        "items_content_insert",
        "items_content_update",
        "items_content_delete",
    }
    assert (
        "scope = 'public' OR owner_org_id = pharos_current_org()"
        in policies["items_content_select"]
    )
    for name in ("insert", "update", "delete"):
        policy = policies[f"items_content_{name}"]
        assert "owner_org_id = pharos_current_org()" in policy
        assert "public" not in policy  # only organization-owned rows are writable


def test_content_policy_adds_a_separate_writer_for_public_rows_on_request() -> None:
    statements = content_policy_sql("items", public_writer_role="pharos_worker")
    writer = statements[-1]
    assert "FOR ALL TO pharos_worker" in writer
    assert "WITH CHECK (scope = 'public' AND owner_org_id IS NULL)" in writer
    assert len(content_policy_sql("items")) == len(statements) - 1


@pytest.mark.parametrize(
    "name", ["items; DROP TABLE users", "Items", "1items", "it ems", "items--", ""]
)
def test_unsafe_identifiers_are_rejected(name: str) -> None:
    with pytest.raises(ValueError, match="unsafe SQL identifier"):
        tenant_policy_sql(name)
    with pytest.raises(ValueError, match="unsafe SQL identifier"):
        content_policy_sql(name)


def test_unsafe_org_column_and_writer_role_are_rejected() -> None:
    with pytest.raises(ValueError, match="unsafe SQL identifier"):
        tenant_policy_sql("items", "org_id; --")
    with pytest.raises(ValueError, match="unsafe SQL identifier"):
        content_policy_sql("items", public_writer_role="worker; DROP ROLE x")
