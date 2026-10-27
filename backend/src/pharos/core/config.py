"""Application settings, read from PHAROS_* environment variables."""

import re
from functools import lru_cache
from pathlib import Path
from typing import Literal, Self
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# backend/alembic, resolved from src/pharos/core/config.py
DEFAULT_ALEMBIC_DIR = Path(__file__).resolve().parents[3] / "alembic"

_PLACEHOLDER_PASSWORDS = {"changeme", "password", "secret", "pharos", "postgres", "example", "test"}


_SLUG = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
_DEVELOPMENT_SECRET_KEY = "development-only-secret-key-never-use-in-production"  # noqa: S105  (the published development default; production refuses it)
MIN_SECRET_KEY_BYTES = 32


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

    # The address users open, as an origin. Redirects and the CSRF Origin check compare against it;
    # the Host header is never trusted for that because the proxy rewrites it.
    public_url: str = "http://localhost:3000"
    # The one organization of this installation (ADR-0005). It is looked up lazily, so the stack
    # can start before the operator creates it.
    org_slug: str = "default"
    # Root secret. Separate keys for separate purposes are derived from it (see core/secrets.py).
    secret_key: SecretStr = SecretStr(_DEVELOPMENT_SECRET_KEY)

    # Sessions live in their own Redis database (Celery uses database 0 of the same server).
    session_redis_db: int = Field(default=1, ge=0, le=15)
    session_idle_seconds: int = Field(default=8 * 3600, ge=60)
    session_absolute_seconds: int = Field(default=24 * 3600, ge=60)

    @field_validator("public_url")
    @classmethod
    def _origin_only(cls, value: str) -> str:
        parts = urlsplit(value)
        try:
            parts.port  # noqa: B018  (raises ValueError for a malformed port)
        except ValueError:
            raise ValueError("public_url has an invalid port") from None
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError("public_url must be an http(s) address such as https://pharos.example")
        if (
            parts.username
            or parts.password
            or parts.path not in ("", "/")
            or parts.query
            or parts.fragment
        ):
            raise ValueError(
                "public_url must be an origin: no credentials, path, query or fragment"
            )
        # Browsers send the Origin header lower-case and without the default port; compare like it.
        host = f"[{parts.hostname}]" if ":" in parts.hostname else parts.hostname
        default_port = 443 if parts.scheme == "https" else 80
        port = f":{parts.port}" if parts.port and parts.port != default_port else ""
        return f"{parts.scheme}://{host}{port}"

    @field_validator("org_slug")
    @classmethod
    def _slug(cls, value: str) -> str:
        if not _SLUG.fullmatch(value):
            raise ValueError("org_slug may contain lower-case letters, digits and hyphens only")
        return value

    @model_validator(mode="after")
    def _idle_is_not_longer_than_absolute(self) -> Self:
        if self.session_idle_seconds > self.session_absolute_seconds:
            raise ValueError("session_idle_seconds cannot exceed session_absolute_seconds")
        return self

    @model_validator(mode="after")
    def _require_real_secrets_in_production(self) -> Self:
        if self.environment != "production":
            return self
        if _is_placeholder(_password(self.database_url)):
            raise ValueError("database_url needs a real password in production")
        if not _password(self.redis_url):
            raise ValueError("redis_url needs a password in production")
        if not self.public_url.startswith("https://"):
            raise ValueError("public_url must use https in production")
        key = self.secret_key.get_secret_value()
        if (
            key == _DEVELOPMENT_SECRET_KEY
            or len(key.encode()) < MIN_SECRET_KEY_BYTES
            or len(set(key)) < 8
        ):
            raise ValueError(
                f"secret_key needs at least {MIN_SECRET_KEY_BYTES} random bytes in production"
            )
        return self


class MigrationSettings(BaseSettings):
    """Used only by Alembic. The application process never needs the table owner's credentials."""

    model_config = SettingsConfigDict(env_prefix="PHAROS_", extra="ignore")

    migrations_database_url: SecretStr


@lru_cache
def get_settings() -> Settings:
    return Settings()
