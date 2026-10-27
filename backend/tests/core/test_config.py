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
