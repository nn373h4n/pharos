import pytest
from pydantic import ValidationError

from pharos.core.config import MigrationSettings, Settings

from ..constants import FAKE_SECRET_KEY

GOOD_DB = "postgresql+asyncpg://pharos_app:Zx9-example-only@db:5432/pharos"
GOOD_REDIS = "redis://:Qw7-example-only@cache:6379/0"
GOOD_SECRET_KEY = FAKE_SECRET_KEY


def production(**overrides: str) -> Settings:
    values = {
        "environment": "production",
        "database_url": GOOD_DB,
        "redis_url": GOOD_REDIS,
        "public_url": "https://pharos.example",
        "secret_key": GOOD_SECRET_KEY,
    }
    return Settings(**{**values, **overrides})  # type: ignore[arg-type]


def test_environment_defaults_to_production() -> None:
    # A forgotten PHAROS_ENVIRONMENT must never relax the checks.
    with pytest.raises(ValidationError, match="real password"):
        Settings()


def test_development_accepts_placeholder_secrets() -> None:
    settings = Settings(environment="development")
    assert settings.log_level == "INFO"
    assert "change-me" in settings.database_url.get_secret_value()


def test_values_come_from_pharos_prefixed_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PHAROS_ENVIRONMENT", "development")
    monkeypatch.setenv("PHAROS_LOG_LEVEL", "DEBUG")
    monkeypatch.setenv("PHAROS_REDIS_URL", "redis://cache:6379/1")
    settings = Settings()
    assert settings.log_level == "DEBUG"
    assert settings.redis_url.get_secret_value() == "redis://cache:6379/1"


def test_unprefixed_variables_are_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    assert Settings(environment="development").log_level == "INFO"


def test_secrets_do_not_appear_in_repr() -> None:
    settings = production()
    for secret in ("Zx9-example-only", "Qw7-example-only", GOOD_SECRET_KEY):
        assert secret not in repr(settings)
        assert secret not in str(settings)


def test_production_accepts_real_credentials() -> None:
    assert production().environment == "production"


@pytest.mark.parametrize(
    "database_url",
    [
        "postgresql+asyncpg://pharos_app:change-me@db:5432/pharos",
        "postgresql+asyncpg://pharos_app:changeme@db:5432/pharos",
        "postgresql+asyncpg://pharos_app:Change_Me@db:5432/pharos",
        "postgresql+asyncpg://pharos_app:password@db:5432/pharos",
        "postgresql+asyncpg://pharos_app:@db:5432/pharos",
        "postgresql+asyncpg://pharos_app@db:5432/pharos",
    ],
)
def test_production_rejects_placeholder_or_missing_database_passwords(database_url: str) -> None:
    with pytest.raises(ValidationError, match="real password"):
        production(database_url=database_url)


@pytest.mark.parametrize("redis_url", ["redis://cache:6379/0", "redis://:@cache:6379/0"])
def test_production_requires_a_redis_password(redis_url: str) -> None:
    with pytest.raises(ValidationError, match="redis_url needs a password"):
        production(redis_url=redis_url)


def test_application_settings_never_hold_the_owner_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PHAROS_MIGRATIONS_DATABASE_URL", "postgresql+asyncpg://owner:x@db/pharos")
    assert not hasattr(production(), "migrations_database_url")


def test_migration_settings_require_the_migration_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PHAROS_MIGRATIONS_DATABASE_URL", raising=False)
    with pytest.raises(ValidationError, match="migrations_database_url"):
        MigrationSettings()  # type: ignore[call-arg]
    monkeypatch.setenv("PHAROS_MIGRATIONS_DATABASE_URL", "postgresql+asyncpg://owner:x@db/pharos")
    assert MigrationSettings().migrations_database_url.get_secret_value().endswith("/pharos")  # type: ignore[call-arg]


@pytest.mark.parametrize(
    "public_url",
    ["http://pharos.example", "pharos.example", "ftp://pharos.example", "https://"],
)
def test_production_requires_an_https_public_url(public_url: str) -> None:
    with pytest.raises(ValidationError, match="public_url"):
        production(public_url=public_url)


@pytest.mark.parametrize(
    "public_url",
    [
        "https://pharos.example/app",
        "https://pharos.example?x=1",
        "https://pharos.example#frag",
        "https://user:pw@pharos.example",
        "https://pharos.example:notaport",
    ],
)
def test_public_url_must_be_a_bare_origin(public_url: str) -> None:
    with pytest.raises(ValidationError, match="public_url"):
        production(public_url=public_url)


def test_public_url_is_normalized_to_an_origin() -> None:
    assert production(public_url="https://Pharos.example:8443/").public_url == (
        "https://pharos.example:8443"
    )
    # The browser leaves the default port out of the Origin header.
    assert (
        production(public_url="https://pharos.example:443").public_url == "https://pharos.example"
    )
    assert Settings(environment="development", public_url="http://localhost:80").public_url == (
        "http://localhost"
    )
    assert Settings(environment="development").public_url == "http://localhost:3000"


@pytest.mark.parametrize(
    "secret_key",
    [
        "development-only-secret-key-never-use-in-production",
        "short",
        "a" * 40,
        "ab" * 20,
    ],
)
def test_production_rejects_a_weak_secret_key(secret_key: str) -> None:
    with pytest.raises(ValidationError, match="secret_key"):
        production(secret_key=secret_key)


def test_development_may_use_the_default_secret_key() -> None:
    assert Settings(environment="development").secret_key.get_secret_value()


@pytest.mark.parametrize("slug", ["", "Acme", "acme corp", "-acme", "acme-", "a_b", "x" * 64])
def test_org_slug_must_be_a_plain_slug(slug: str) -> None:
    with pytest.raises(ValidationError, match="org_slug"):
        production(org_slug=slug)


def test_org_slug_accepts_a_plain_slug() -> None:
    assert production(org_slug="acme-2").org_slug == "acme-2"


def test_idle_timeout_cannot_exceed_the_absolute_limit() -> None:
    with pytest.raises(ValidationError, match="session_idle_seconds"):
        production(session_idle_seconds=100, session_absolute_seconds=60)


def test_local_login_defaults_to_break_glass_and_accepts_only_known_modes() -> None:
    assert production().local_login == "break-glass"
    assert production(local_login="on").local_login == "on"
    with pytest.raises(ValidationError, match="local_login"):
        production(local_login="yes")


@pytest.mark.parametrize(
    "overrides",
    [{"password_hash_memory_kib": 1024}, {"password_hash_time_cost": 1}],
)
def test_production_refuses_cheap_password_hashing(overrides: dict[str, int]) -> None:
    with pytest.raises(ValidationError, match="too cheap"):
        production(**overrides)  # type: ignore[arg-type]


def test_tests_may_use_cheap_password_hashing() -> None:
    settings = Settings(environment="test", password_hash_memory_kib=64, password_hash_time_cost=1)
    assert settings.password_hash_memory_kib == 64


# --- single sign-on settings -------------------------------------------------------------------

OIDC = {
    "oidc_issuer": "https://login.example/realms/acme",
    "oidc_client_id": "pharos",
    "oidc_client_secret": "a-real-looking-client-secret-1234",
}


def test_single_sign_on_is_off_by_default() -> None:
    settings = production()
    assert settings.oidc_enabled is False
    assert settings.oidc_issuer is None


def test_single_sign_on_is_on_when_issuer_and_client_are_set() -> None:
    settings = production(**OIDC)
    assert settings.oidc_enabled is True
    assert settings.oidc_scopes == "openid profile email"


@pytest.mark.parametrize("missing", ["oidc_issuer", "oidc_client_id"])
def test_issuer_and_client_id_come_together(missing: str) -> None:
    values = {k: v for k, v in OIDC.items() if k != missing}
    with pytest.raises(ValidationError, match="set together"):
        production(**values)


@pytest.mark.parametrize(
    "issuer",
    [
        "ftp://login.example",
        "login.example",
        "https://",
        "https://user:pw@login.example",
        "https://login.example/realm?x=1",
        "https://login.example/realm#frag",
    ],
)
def test_the_issuer_must_be_a_plain_http_url(issuer: str) -> None:
    with pytest.raises(ValidationError, match="oidc_issuer"):
        production(**{**OIDC, "oidc_issuer": issuer})


def test_production_needs_an_https_issuer_and_a_client_secret() -> None:
    with pytest.raises(ValidationError, match="https in production"):
        production(**{**OIDC, "oidc_issuer": "http://login.example/realms/acme"})
    without_secret = {k: v for k, v in OIDC.items() if k != "oidc_client_secret"}
    with pytest.raises(ValidationError, match="oidc_client_secret"):
        production(**without_secret)
    with pytest.raises(ValidationError, match="oidc_client_secret"):
        production(**{**OIDC, "oidc_client_secret": ""})


def test_development_may_use_plain_http_and_no_secret() -> None:
    settings = Settings(
        environment="development",
        oidc_issuer="http://localhost:8080/realms/dev",
        oidc_client_id="pharos",
    )
    assert settings.oidc_enabled


def test_the_scopes_must_include_openid() -> None:
    with pytest.raises(ValidationError, match="openid"):
        production(**{**OIDC, "oidc_scopes": "profile email"})


def test_the_client_secret_does_not_appear_in_repr() -> None:
    assert OIDC["oidc_client_secret"] not in repr(production(**OIDC))


def test_empty_oidc_values_from_compose_mean_not_configured() -> None:
    settings = production(oidc_issuer="", oidc_client_id="", oidc_client_secret="")
    assert settings.oidc_enabled is False
    assert settings.oidc_client_secret is None


# --- Microsoft Entra ID preset -----------------------------------------------------------------

TENANT = "0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
ENTRA = {
    "oidc_preset": "entra",
    "entra_tenant_id": TENANT,
    "oidc_client_id": "11111111-2222-4333-8444-555555555555",
    "oidc_client_secret": "a-real-looking-client-secret-1234",
}


def test_the_entra_preset_derives_everything_from_the_tenant() -> None:
    settings = production(**ENTRA)
    assert settings.oidc_enabled is True
    assert settings.oidc_issuer == f"https://login.microsoftonline.com/{TENANT}/v2.0"
    assert settings.oidc_scopes == "openid profile email User.Read"
    assert settings.oidc_department_claim is None  # Graph supplies it
    assert settings.oidc_provider_name == "Microsoft"


def test_the_tenant_id_is_normalized_to_lower_case() -> None:
    assert production(**{**ENTRA, "entra_tenant_id": TENANT.upper()}).entra_tenant_id == TENANT


def test_the_defaults_compose_passes_along_do_not_hide_the_preset() -> None:
    settings = production(
        **ENTRA,
        oidc_scopes="openid profile email",
        oidc_provider_name="Single sign-on",
        oidc_department_claim="department",
        oidc_issuer="",
    )
    assert settings.oidc_provider_name == "Microsoft"
    assert settings.oidc_scopes.endswith("User.Read")
    assert settings.oidc_department_claim is None


def test_explicit_scopes_and_name_are_kept() -> None:
    settings = production(**ENTRA, oidc_scopes="openid email", oidc_provider_name="Contoso")
    assert settings.oidc_scopes == "openid email"
    assert settings.oidc_provider_name == "Contoso"


@pytest.mark.parametrize(
    "tenant",
    [
        "common",
        "organizations",
        "consumers",
        "contoso.onmicrosoft.com",
        "7c6b5a49-3827-4615-9a03-b1c2d3e4f506/../common",
        "0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4",
        "{tenantid}",
    ],
)
def test_only_one_directory_by_its_guid_is_accepted(tenant: str) -> None:
    with pytest.raises(ValidationError, match="entra_tenant_id"):
        production(**{**ENTRA, "entra_tenant_id": tenant})


def test_the_tenant_of_personal_microsoft_accounts_is_refused_by_its_guid_too() -> None:
    with pytest.raises(ValidationError, match="personal Microsoft accounts"):
        production(**{**ENTRA, "entra_tenant_id": "9188040d-6c67-4c5b-b112-36a304b66dad"})


def test_guests_are_refused_unless_allowed() -> None:
    assert production(**ENTRA).entra_allow_guests is False
    assert production(**ENTRA, entra_allow_guests="true").entra_allow_guests is True


def test_the_entra_preset_needs_a_tenant_and_a_client_id() -> None:
    without_tenant = {k: v for k, v in ENTRA.items() if k != "entra_tenant_id"}
    with pytest.raises(ValidationError, match="entra_tenant_id"):
        production(**without_tenant)
    without_client = {k: v for k, v in ENTRA.items() if k != "oidc_client_id"}
    with pytest.raises(ValidationError, match="oidc_client_id"):
        production(**without_client)


def test_an_issuer_that_disagrees_with_the_tenant_is_refused() -> None:
    other = "https://login.microsoftonline.com/7c6b5a49-3827-4615-9a03-b1c2d3e4f506/v2.0"
    with pytest.raises(ValidationError, match="does not match"):
        production(**ENTRA, oidc_issuer=other)
    with pytest.raises(ValidationError, match="does not match"):
        production(**ENTRA, oidc_issuer="https://login.microsoftonline.com/common/v2.0")
    same = f"https://login.microsoftonline.com/{TENANT}/v2.0"
    assert production(**ENTRA, oidc_issuer=same).oidc_issuer == same


def test_a_tenant_without_the_preset_is_a_mistake() -> None:
    with pytest.raises(ValidationError, match="oidc_preset"):
        production(**OIDC, entra_tenant_id=TENANT)


def test_production_still_wants_the_client_secret_with_the_preset() -> None:
    without = {k: v for k, v in ENTRA.items() if k != "oidc_client_secret"}
    with pytest.raises(ValidationError, match="oidc_client_secret"):
        production(**without)


def test_an_empty_tenant_from_compose_means_not_configured() -> None:
    settings = production(entra_tenant_id="")
    assert settings.entra_tenant_id is None and settings.oidc_enabled is False
