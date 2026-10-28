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
_GUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
ENTRA_ISSUER = "https://login.microsoftonline.com/{tenant}/v2.0"
_DEFAULT_SCOPES = "openid profile email"
# The directory behind the "consumers" alias: personal Microsoft accounts, never an organization.
_ENTRA_CONSUMERS_TENANT = "9188040d-6c67-4c5b-b112-36a304b66dad"
_DEFAULT_PROVIDER_NAME = "Single sign-on"


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
    # Which kind of process this is. A worker never signs anybody in, so it is given no root key and
    # no identity-provider secret (see deploy/docker-compose.yml) and is not asked to have them.
    process: Literal["api", "worker"] = "api"

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

    # The language shown to people who have not chosen one and whose browser asks for none we offer.
    default_locale: Literal["en", "az", "tr", "ru"] = "en"
    # Local (password) sign-in: "off", "break-glass" (administrators only) or "on" (everyone).
    local_login: Literal["off", "break-glass", "on"] = "break-glass"
    # Argon2id cost. The defaults follow OWASP's guidance with a margin; production refuses less.
    password_hash_memory_kib: int = Field(default=65_536, ge=8)
    password_hash_time_cost: int = Field(default=3, ge=1)

    # Single sign-on through any OpenID Connect provider (ADR-0003, ADR-0011). Off unless both the
    # issuer and the client id are set.
    #
    # `oidc_preset="entra"` is Microsoft Entra ID: set `entra_tenant_id` (a directory GUID) and the
    # issuer is derived from it. The multi-tenant aliases (common, organizations, consumers) are
    # refused, so only people of that one directory can sign in.
    oidc_preset: Literal["generic", "entra"] = "generic"
    entra_tenant_id: str | None = None
    # Guests (B2B users invited from another directory) are refused unless this is set.
    entra_allow_guests: bool = False
    oidc_issuer: str | None = None
    oidc_client_id: str | None = Field(default=None, max_length=256)
    oidc_client_secret: SecretStr | None = None
    oidc_scopes: str = _DEFAULT_SCOPES
    # Shown on the sign-in button.
    oidc_provider_name: str = Field(default=_DEFAULT_PROVIDER_NAME, min_length=1, max_length=60)
    # Which ID-token claim holds the department, if the provider sends one.
    oidc_department_claim: str | None = Field(default="department", max_length=64)

    # Sessions live in their own Redis database (Celery uses database 0 of the same server).
    session_redis_db: int = Field(default=1, ge=0, le=15)
    session_idle_seconds: int = Field(default=8 * 3600, ge=60)
    session_absolute_seconds: int = Field(default=24 * 3600, ge=60)

    @field_validator(
        "oidc_issuer", "oidc_client_id", "oidc_client_secret", "entra_tenant_id", mode="before"
    )
    @classmethod
    def _empty_means_unset(cls, value: object) -> object:
        """Compose passes unset variables as empty strings; they mean "not configured"."""
        return None if isinstance(value, str) and not value.strip() else value

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
    def _entra_preset(self) -> Self:
        if self.oidc_preset != "entra":
            if self.entra_tenant_id is not None:
                raise ValueError("entra_tenant_id needs oidc_preset=entra")
            return self
        tenant = (self.entra_tenant_id or "").lower()
        if not _GUID.fullmatch(tenant):
            raise ValueError(
                "entra_tenant_id must be the directory (tenant) id, a GUID; "
                "'common', 'organizations', 'consumers' and domain names are refused"
            )
        if tenant == _ENTRA_CONSUMERS_TENANT:
            raise ValueError("entra_tenant_id is the tenant of personal Microsoft accounts")
        issuer = ENTRA_ISSUER.format(tenant=tenant)
        if self.oidc_issuer is not None and self.oidc_issuer != issuer:
            raise ValueError("oidc_issuer does not match entra_tenant_id; leave it unset")
        if self.oidc_client_id is None:
            raise ValueError("oidc_client_id is required with oidc_preset=entra")
        self.entra_tenant_id = tenant
        self.oidc_issuer = issuer
        if self.oidc_scopes == _DEFAULT_SCOPES:
            self.oidc_scopes = f"{_DEFAULT_SCOPES} User.Read"  # User.Read: department from Graph
        if self.oidc_provider_name == _DEFAULT_PROVIDER_NAME:  # Compose passes the default along
            self.oidc_provider_name = "Microsoft"
        self.oidc_department_claim = None  # Entra does not put it in the ID token
        return self

    @model_validator(mode="after")
    def _oidc_is_all_or_nothing(self) -> Self:
        if (self.oidc_issuer is None) != (self.oidc_client_id is None):
            raise ValueError("oidc_issuer and oidc_client_id must be set together")
        if self.oidc_issuer is not None:
            parts = urlsplit(self.oidc_issuer)
            if parts.scheme not in ("http", "https") or not parts.hostname:
                raise ValueError("oidc_issuer must be an http(s) URL")
            if parts.query or parts.fragment or parts.username or parts.password:
                raise ValueError("oidc_issuer cannot contain credentials, a query or a fragment")
            if "openid" not in self.oidc_scopes.split():
                raise ValueError("oidc_scopes must include openid")
        return self

    @property
    def oidc_enabled(self) -> bool:
        return self.oidc_issuer is not None

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
        if self.password_hash_memory_kib < 19_456 or self.password_hash_time_cost < 2:
            raise ValueError(
                "password hashing is too cheap for production (Argon2id needs at least "
                "19456 KiB and 2 iterations)"
            )
        if self.process == "worker":
            if self.oidc_issuer is not None or self.oidc_client_secret is not None:
                raise ValueError("a worker must not be given identity-provider settings")
            if self.secret_key.get_secret_value() != _DEVELOPMENT_SECRET_KEY:
                raise ValueError("a worker must not be given the root key")
            return self
        if self.oidc_issuer is not None:
            if not self.oidc_issuer.startswith("https://"):
                raise ValueError("oidc_issuer must use https in production")
            if self.oidc_client_secret is None or not self.oidc_client_secret.get_secret_value():
                raise ValueError("oidc_client_secret is required in production")
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
