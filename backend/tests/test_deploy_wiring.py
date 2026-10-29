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
    # Set by Compose per service (api or worker), never by an operator.
    "PHAROS_PROCESS",
    # Test-only address allowance of SafeHttpClient: Settings refuses it outside environment=test,
    # and Compose must never offer a way to set it (see the test below).
    "PHAROS_FETCH_TEST_ALLOWED_NETWORKS",
    "PHAROS_FETCH_TEST_ALLOWED_PORTS",
}

# Settings only the worker is given: the API has no use for them and must not hold them. They are
# still documented in .env.example, and a test below checks which service receives them.
WORKER_ONLY = {"PHAROS_GITHUB_TOKEN"}

# Settings that are documented in .env.example only as a commented example, or not at all.
NOT_IN_ENV_EXAMPLE = NOT_PASSED_BY_COMPOSE


def setting_names() -> list[str]:
    return sorted(f"PHAROS_{name.upper()}" for name in Settings.model_fields)


def environment_block(anchor: str) -> str:
    compose = (DEPLOY / "docker-compose.yml").read_text()
    match = re.search(rf"^{anchor}: &[\w-]+\n(.*?)(?=\n\S)", compose, re.DOTALL | re.MULTILINE)
    assert match, f"{anchor} block not found in docker-compose.yml"
    return match.group(1)


def worker_environment_block() -> str:
    return environment_block("x-worker-env")


def backend_environment_block() -> str:
    """What the API gets: its own lines plus the worker's (merged with `<<:` in Compose)."""
    return environment_block("x-backend-env") + "\n" + worker_environment_block()


@pytest.mark.parametrize(
    "name", [n for n in setting_names() if n not in NOT_PASSED_BY_COMPOSE | WORKER_ONLY]
)
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


def test_compose_never_passes_the_test_only_fetch_allowance() -> None:
    compose = (DEPLOY / "docker-compose.yml").read_text()
    example = (DEPLOY / ".env.example").read_text()
    assert "FETCH_TEST_ALLOWED" not in compose
    assert "FETCH_TEST_ALLOWED" not in example


def test_the_exceptions_above_are_real_settings() -> None:
    names = set(setting_names())
    assert names >= NOT_PASSED_BY_COMPOSE | WORKER_ONLY


def test_redis_has_a_memory_limit_below_the_containers_so_it_refuses_writes_instead_of_dying() -> (
    None
):
    compose = (DEPLOY / "docker-compose.yml").read_text()
    redis = compose[compose.index("\n  redis:\n") :]
    start = (DEPLOY / "redis" / "start.sh").read_text()
    limit = re.search(r"--maxmemory (\d+)mb", start)
    container = re.search(r"mem_limit: (\d+)m", redis)
    assert limit, "redis-server needs --maxmemory (noeviction only works with a limit)"
    assert container
    assert int(limit.group(1)) < int(container.group(1))
    assert "--maxmemory-policy noeviction" in start
    assert "/redis/start.sh" in redis


def test_the_worker_and_the_api_connect_to_the_database_as_different_roles() -> None:
    worker = worker_environment_block()
    api = environment_block("x-backend-env")
    worker_url = re.search(r"^\s+PHAROS_DATABASE_URL: (\S+)", worker, re.MULTILINE)
    api_url = re.search(r"^\s+PHAROS_DATABASE_URL: (\S+)", api, re.MULTILINE)
    assert worker_url and api_url
    assert worker_url.group(1).startswith("postgresql+asyncpg://pharos_worker:${PHAROS_WORKER_DB_")
    assert api_url.group(1).startswith("postgresql+asyncpg://pharos_app:${PHAROS_APP_PASSWORD")
    assert "PHAROS_APP_PASSWORD" not in worker  # the worker never receives the application password
    compose = (DEPLOY / "docker-compose.yml").read_text()
    postgres = compose[compose.index("\n  postgres:\n") : compose.index("\n  redis:\n")]
    assert "PHAROS_WORKER_DB_PASSWORD: ${PHAROS_WORKER_DB_PASSWORD" in postgres
    init = (DEPLOY / "postgres" / "init" / "01-roles.sh").read_text()
    assert "CREATE ROLE pharos_worker LOGIN NOSUPERUSER NOBYPASSRLS" in init


def test_the_scheduler_has_no_route_to_the_internet() -> None:
    compose = (DEPLOY / "docker-compose.yml").read_text()

    def networks_of(service: str, end: str) -> str:
        section = compose[compose.index(f"\n  {service}:\n") : compose.index(end)]
        match = re.search(r"^\s+networks: (\[.*\])$", section, re.MULTILINE)
        assert match, f"{service} has no networks line"
        return match.group(1)

    assert networks_of("beat", "# One-shot job") == "[data]"
    assert "egress" in networks_of("worker", "\n  beat:\n")  # positive control


def test_the_worker_and_the_api_connect_to_redis_as_different_users() -> None:
    worker = worker_environment_block()
    api = environment_block("x-backend-env")
    worker_url = re.search(r"^\s+PHAROS_REDIS_URL: (\S+)", worker, re.MULTILINE)
    api_url = re.search(r"^\s+PHAROS_REDIS_URL: (\S+)", api, re.MULTILINE)
    assert worker_url and api_url
    assert worker_url.group(1).startswith("redis://pharos_worker:${PHAROS_REDIS_WORKER_PASSWORD")
    assert api_url.group(1).startswith("redis://pharos_api:${PHAROS_REDIS_PASSWORD")
    # The worker never receives the API's Redis password, in any variable.
    assert "PHAROS_REDIS_PASSWORD" not in worker
    # Redis itself is told both passwords.
    compose = (DEPLOY / "docker-compose.yml").read_text()
    redis = compose[compose.index("\n  redis:\n") :]
    assert "REDIS_API_PASSWORD: ${PHAROS_REDIS_PASSWORD" in redis
    assert "REDIS_WORKER_PASSWORD: ${PHAROS_REDIS_WORKER_PASSWORD" in redis


def test_the_development_keycloak_answers_only_private_and_loopback_clients() -> None:
    site = (DEPLOY / "caddy" / "sso-dev.caddy").read_text()
    assert "not remote_ip private_ranges" in site
    assert "respond @outside 403" in site
    # The guard comes before the proxy line, so a refused client never reaches Keycloak.
    assert site.index("respond @outside 403") < site.index("reverse_proxy")


def test_the_worker_and_the_scheduler_get_no_secrets_of_signing_in() -> None:
    worker = worker_environment_block()
    names = set(re.findall(r"^\s+(PHAROS_[A-Z_]+):", worker, re.MULTILINE))
    forbidden = {
        n for n in names if any(w in n for w in ("SECRET", "OIDC", "ENTRA", "LOCAL_LOGIN"))
    }
    assert forbidden == set(), f"the worker environment holds {sorted(forbidden)}"
    assert "PHAROS_PROCESS: worker" in worker
    api_only = environment_block("x-backend-env")
    assert "PHAROS_PROCESS: api" in api_only
    assert "PHAROS_SECRET_KEY" in api_only


def test_only_the_api_service_uses_the_full_environment() -> None:
    compose = (DEPLOY / "docker-compose.yml").read_text()
    beat = re.search(r"^  beat:\n(.*?)(?=^  \w)", compose, re.DOTALL | re.MULTILINE)
    assert beat
    assert "environment: *worker-env" in beat.group(1)  # exactly the worker's, nothing added
    # The worker is the worker's environment plus the optional GitHub token, and nothing else.
    worker = re.search(
        r"^  worker:\n.*?^    environment:\n((?:      [^\n]*\n)+)",
        compose,
        re.DOTALL | re.MULTILINE,
    )
    assert worker
    assert "<<: *worker-env" in worker.group(1)
    assert re.findall(r"^\s+(PHAROS_[A-Z_]+):", worker.group(1), re.MULTILINE) == [
        "PHAROS_GITHUB_TOKEN"
    ]
    assert "backend-env" not in worker.group(1)
    api = re.search(r"^  api:\n(.*?)(?=^  \w)", compose, re.DOTALL | re.MULTILINE)
    assert api
    assert "environment: *backend-env" in api.group(1)


def service_block(name: str) -> str:
    compose = (DEPLOY / "docker-compose.yml").read_text()
    match = re.search(rf"^  {name}:\n(.*?)(?=^  \S|\Z)", compose, re.DOTALL | re.MULTILINE)
    assert match, f"service {name} not found"
    return match.group(1)


def test_only_the_worker_receives_the_github_token() -> None:
    assert "PHAROS_GITHUB_TOKEN" in service_block("worker")
    for other in ("beat", "api", "web", "caddy", "migrate", "postgres", "redis"):
        assert "PHAROS_GITHUB_TOKEN" not in service_block(other), other
    # the shared blocks that the API and the scheduler build on must not carry it either
    assert "PHAROS_GITHUB_TOKEN" not in worker_environment_block()
    assert "PHAROS_GITHUB_TOKEN" not in backend_environment_block()


def test_the_github_token_is_optional_and_documented_with_no_value() -> None:
    example = (DEPLOY / ".env.example").read_text()
    assert re.search(r"^PHAROS_GITHUB_TOKEN=$", example, re.MULTILINE)
    assert "PHAROS_GITHUB_TOKEN: ${PHAROS_GITHUB_TOKEN:-}" in service_block(
        "worker"
    )  # not required
