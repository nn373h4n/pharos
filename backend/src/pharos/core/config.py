"""Application settings, read from PHAROS_* environment variables."""

import re
from functools import lru_cache
from pathlib import Path
from typing import Literal, Self
from urllib.parse import urlsplit

from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# backend/alembic, resolved from src/pharos/core/config.py
DEFAULT_ALEMBIC_DIR = Path(__file__).resolve().parents[3] / "alembic"

_PLACEHOLDER_PASSWORDS = {"changeme", "password", "secret", "pharos", "postgres", "example", "test"}


def _password(url: SecretStr) -> str:
    return urlsplit(url.get_secret_value()).password or ""


def _is_placeholder(password: str) -> bool:
    return re.sub(r"[^a-z]", "", password.lower()) in _PLACEHOLDER_PASSWORDS or password == ""


class Settings(BaseSettings):
    """Runtime configuration of the API and worker processes.

    Secrets are SecretStr so they never show up in repr or logs. The environment defaults to
    production on purpose: forgetting to set it must never relax the checks below.
    """

    model_config = SettingsConfigDict(env_prefix="PHAROS_", extra="ignore")

    environment: Literal["development", "test", "production"] = "production"
    log_level: str = "INFO"

    # The application connects as a role that does not own the tables, so RLS always applies.
    # The owner role's URL is deliberately not part of these settings (see MigrationSettings).
    database_url: SecretStr = SecretStr(
        "postgresql+asyncpg://pharos_app:change-me@localhost:5432/pharos"
    )
    redis_url: SecretStr = SecretStr("redis://localhost:6379/0")

    alembic_dir: Path = DEFAULT_ALEMBIC_DIR

    @model_validator(mode="after")
    def _require_real_secrets_in_production(self) -> Self:
        if self.environment != "production":
            return self
        if _is_placeholder(_password(self.database_url)):
            raise ValueError("database_url needs a real password in production")
        if not _password(self.redis_url):
            raise ValueError("redis_url needs a password in production")
        return self


class MigrationSettings(BaseSettings):
    """Used only by Alembic. The application process never needs the table owner's credentials."""

    model_config = SettingsConfigDict(env_prefix="PHAROS_", extra="ignore")

    migrations_database_url: SecretStr


@lru_cache
def get_settings() -> Settings:
    return Settings()
