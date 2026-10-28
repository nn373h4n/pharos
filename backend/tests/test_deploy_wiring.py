"""Every setting the API reads must be reachable from `deploy/.env` (it was forgotten twice).

A setting that exists in `Settings` but is not passed by Compose cannot be set by an operator at
all; one that is not in `.env.example` cannot be found. Both are bugs, so this test fails for them.
"""

import re
from pathlib import Path

import pytest

from pharos.core.config import Settings

DEPLOY = Path(__file__).resolve().parents[2] / "deploy"

# Settings an operator never sets through Compose.
NOT_PASSED_BY_COMPOSE = {
    # Fixed inside the image.
    "PHAROS_ALEMBIC_DIR",
    # Built from the passwords by Compose (postgres/redis host names are Compose's business).
    "PHAROS_DATABASE_URL",
    "PHAROS_REDIS_URL",
    # Chosen by the session cookie rules, not by an operator.
    "PHAROS_SESSION_REDIS_DB",
    # Tuning knobs that have safe defaults and no deployment reason to change.
    "PHAROS_SESSION_IDLE_SECONDS",
    "PHAROS_SESSION_ABSOLUTE_SECONDS",
    "PHAROS_PASSWORD_HASH_MEMORY_KIB",
    "PHAROS_PASSWORD_HASH_TIME_COST",
}

# Settings that are documented in .env.example only as a commented example, or not at all.
NOT_IN_ENV_EXAMPLE = NOT_PASSED_BY_COMPOSE


def setting_names() -> list[str]:
    return sorted(f"PHAROS_{name.upper()}" for name in Settings.model_fields)


def backend_environment_block() -> str:
    compose = (DEPLOY / "docker-compose.yml").read_text()
    match = re.search(r"x-backend-env: &backend-env\n(.*?)\n\nservices:", compose, re.DOTALL)
    assert match, "x-backend-env block not found in docker-compose.yml"
    return match.group(1)


@pytest.mark.parametrize("name", [n for n in setting_names() if n not in NOT_PASSED_BY_COMPOSE])
def test_every_operator_setting_is_passed_to_the_backend_by_compose(name: str) -> None:
    assert re.search(rf"^\s+{name}:", backend_environment_block(), re.MULTILINE), (
        f"{name} is a setting of the API but docker-compose.yml does not pass it "
        "(add it to x-backend-env)"
    )


@pytest.mark.parametrize("name", [n for n in setting_names() if n not in NOT_IN_ENV_EXAMPLE])
def test_every_operator_setting_is_documented_in_the_env_example(name: str) -> None:
    example = (DEPLOY / ".env.example").read_text()
    assert re.search(rf"^#?\s*{name}=", example, re.MULTILINE), (
        f"{name} is not in deploy/.env.example (a commented example line is enough)"
    )


def test_compose_does_not_pass_settings_the_api_does_not_know() -> None:
    known = set(setting_names()) | {"PHAROS_MIGRATE_ON_START", "PHAROS_MIGRATIONS_DATABASE_URL"}
    passed = set(re.findall(r"^\s+(PHAROS_[A-Z_]+):", backend_environment_block(), re.MULTILINE))
    assert passed <= known, f"unknown settings in x-backend-env: {sorted(passed - known)}"


def test_the_exceptions_above_are_real_settings() -> None:
    names = set(setting_names())
    assert names >= NOT_PASSED_BY_COMPOSE
