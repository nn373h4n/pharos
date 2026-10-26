import pytest
from pydantic import ValidationError

from pharos.core.config import MigrationSettings, Settings

GOOD_DB = "postgresql+asyncpg://pharos_app:Zx9-example-only@db:5432/pharos"
GOOD_REDIS = "redis://:Qw7-example-only@cache:6379/0"


def production(**overrides: str) -> Settings:
    values = {"environment": "production", "database_url": GOOD_DB, "redis_url": GOOD_REDIS}
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
    for secret in ("Zx9-example-only", "Qw7-example-only"):
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
