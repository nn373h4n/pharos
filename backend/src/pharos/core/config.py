"""Application settings, read from PHAROS_* environment variables."""

import ipaddress
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


# Labels of letters, digits and hyphens; the last starts with a letter, so no IPv4 spelling fits.
_DNS_NAME = re.compile(
    r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?"
)


class Settings(BaseSettings):
    """Runtime configuration of the API and worker processes.

    Secrets are SecretStr so they never show up in repr or logs. The environment defaults to
    production on purpose: forgetting to set it must never relax the checks below.
    """

    # A wrong value must never be echoed: a validation error is logged at start-up, and the value
    # may be a password or a token that was pasted in the wrong shape.
    model_config = SettingsConfigDict(
        env_prefix="PHAROS_", extra="ignore", hide_input_in_errors=True
    )

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
    # Whether people may follow feeds of their own (M3-13). Off: the endpoints answer 404 and
    # the worker does not fetch the ones that exist. The operator's switch against abuse, as
    # nobody else, administrators included, can see a person's feeds.
    personal_feeds: bool = True

    # The model layer (M4, ADR-0002, ADR-0015, ADR-0016). Off unless a provider is named, and the
    # product works without it. The key is held by the worker only (it makes the calls).
    llm_provider: Literal["off", "fake", "anthropic", "openai", "azure"] = "off"
    llm_api_key: SecretStr | None = None
    # Only for a provider that needs a different endpoint (Azure, a gateway). https only.
    llm_base_url: str | None = None
    # The model of each stage; a stage without a model makes no call.
    llm_model_filter: str = ""
    llm_model_enrichment: str = ""
    llm_model_insight: str = ""
    # A small cap on purpose; an administrator raises it after the pilot shows the real cost.
    # In millionths of a US dollar per day: 2_000_000 is two dollars.
    llm_daily_budget_micro_usd: int = Field(default=2_000_000, ge=0)
    llm_calls_per_minute: int = Field(default=30, ge=1, le=10_000)
    llm_timeout_seconds: float = Field(default=60.0, gt=0, le=300)
    llm_max_retries: int = Field(default=2, ge=0, le=5)
    # How many unread articles one sweep (every minute) reads at most.
    llm_enrich_batch: int = Field(default=20, ge=1, le=200)
    # What a model costs, in millionths of a dollar per token (the same number as dollars per
    # million tokens), for the budget. Conservative on purpose; raise them for a dearer model,
    # or the day's cap is reached later than it should be.
    llm_price_in_micro_usd: int = Field(default=20, ge=1, le=100_000)
    llm_price_out_micro_usd: int = Field(default=100, ge=1, le=100_000)
    # Root secret. Separate keys for separate purposes are derived from it (see core/secrets.py).
    secret_key: SecretStr = SecretStr(_DEVELOPMENT_SECRET_KEY)

    # The language shown to people who have not chosen one and whose browser asks for none we offer.
    default_locale: Literal["en", "az", "tr", "ru"] = "en"
    # Networks the fetcher may reach although they are private (comma separated, for tests that need
    # a fake feed server on the loopback or a Docker address). Refused in every other environment.
    fetch_test_allowed_networks: str = ""
    # Ports besides 80 and 443 the fetcher may use, for the same tests. Same rule.
    fetch_test_allowed_ports: str = ""
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

    # A fine-grained GitHub token (no scopes) for the worker. Optional: it only raises the API rate
    # limit from 60 to 5000 requests an hour. Sent to api.github.com only (SafeHttpClient `Bearer`).
    github_token: SecretStr | None = None

    # Sessions live in their own Redis database (Celery uses database 0 of the same server).
    session_redis_db: int = Field(default=1, ge=0, le=15)
    session_idle_seconds: int = Field(default=8 * 3600, ge=60)
    session_absolute_seconds: int = Field(default=24 * 3600, ge=60)

    @field_validator(
        "oidc_issuer",
        "oidc_client_id",
        "oidc_client_secret",
        "entra_tenant_id",
        "github_token",
        mode="before",
    )
    @classmethod
    def _empty_means_unset(cls, value: object) -> object:
        """Compose passes unset variables as empty strings; they mean "not configured"."""
        return None if isinstance(value, str) and not value.strip() else value

    @field_validator("github_token")
    @classmethod
    def _github_token_is_a_plain_token(cls, value: SecretStr | None) -> SecretStr | None:
        # It goes into a request header: no space, quote or line break may get through. The message
        # names the setting and never the value.
        if value is not None and not re.fullmatch(
            r"[A-Za-z0-9_.-]{10,255}", value.get_secret_value()
        ):
            raise ValueError("github_token must be 10 to 255 letters, digits, '_', '.' or '-'")
        return value

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

    @property
    def fetch_test_allowed_networks_list(self) -> list[str]:
        return [
            part.strip() for part in self.fetch_test_allowed_networks.split(",") if part.strip()
        ]

    @property
    def fetch_test_allowed_ports_list(self) -> list[int]:
        return [int(part) for part in self.fetch_test_allowed_ports.split(",") if part.strip()]

    @field_validator("llm_api_key", "llm_base_url", mode="before")
    @classmethod
    def _llm_blank_is_unset(cls, value: object) -> object:
        # Compose passes an empty string for a variable that is not set.
        return None if isinstance(value, str) and not value.strip() else value

    @model_validator(mode="after")
    def _llm_is_consistent(self) -> Self:
        if self.llm_provider == "fake" and self.environment == "production":
            raise ValueError("the fake model provider is refused in production")
        if self.llm_base_url is not None:
            parts = urlsplit(self.llm_base_url)
            host = (parts.hostname or "").lower()
            if parts.scheme != "https" or not host or parts.username or parts.password:
                raise ValueError("llm_base_url must be an https address without credentials")
            # A provider is reached by a DNS name: its last label starts with a letter. That
            # refuses every spelling of an address (2130706433, 127.1, 0x7f.1, [::1]) and a name
            # with a trailing dot. The name is also looked up before the first call (M4-03).
            if (
                not _DNS_NAME.fullmatch(host)
                or host == "localhost"
                or host.endswith((".localhost", ".local", ".internal", ".lan", ".home.arpa"))
            ):
                raise ValueError("llm_base_url must be a public DNS name, not inside the network")
        return self

    @model_validator(mode="after")
    def _fetch_allowance_only_in_tests(self) -> Self:
        networks = self.fetch_test_allowed_networks_list
        if (networks or self.fetch_test_allowed_ports.strip()) and self.environment != "test":
            raise ValueError(
                "the fetch_test_allowed_* settings are only accepted when environment=test"
            )
        try:
            ports = self.fetch_test_allowed_ports_list
        except ValueError:
            raise ValueError("fetch_test_allowed_ports must be comma separated numbers") from None
        if any(not 1 <= port <= 65535 for port in ports):
            raise ValueError("fetch_test_allowed_ports: a port is out of range")
        for network in networks:
            try:
                ipaddress.ip_network(network, strict=True)
            except ValueError:
                raise ValueError(
                    f"fetch_test_allowed_networks: {network!r} is not a network"
                ) from None
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
        if self.github_token is not None:
            raise ValueError("only the worker may be given the GitHub token")
        if self.llm_api_key is not None:
            raise ValueError("only the worker may be given the model key")
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

    model_config = SettingsConfigDict(
        env_prefix="PHAROS_", extra="ignore", hide_input_in_errors=True
    )

    migrations_database_url: SecretStr


@lru_cache
def get_settings() -> Settings:
    return Settings()
