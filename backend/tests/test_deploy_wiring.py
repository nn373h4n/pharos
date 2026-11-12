"""Every setting the API reads must be reachable from `deploy/.env` (it was forgotten twice).

A setting that exists in `Settings` but is not passed by Compose cannot be set by an operator at
all; one that is not in `.env.example` cannot be found. Both are bugs, so this test fails for them.
"""

import os
import re
import stat
import subprocess
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
WORKER_ONLY = {"PHAROS_GITHUB_TOKEN", "PHAROS_LLM_API_KEY"}

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


def test_the_test_only_overlay_is_used_by_the_stack_test_scripts_and_nothing_else() -> None:
    """The overlay runs the worker with environment=test; no real installation may include it."""
    root = DEPLOY.parent
    overlay = "docker-compose.stack-e2e.yml"
    assert (DEPLOY / overlay).is_file()
    users = [
        str(path.relative_to(root))
        for path in list(root.glob("scripts/*"))
        + list(root.glob("deploy/**/*"))
        + [root / "Makefile"]
        + list(root.glob(".github/workflows/*"))
        if path.is_file() and path.name != overlay and overlay in path.read_text(errors="ignore")
    ]
    assert sorted(users) == [
        "scripts/loadtest.sh",
        "scripts/resilience-check.sh",
        "scripts/stack-e2e.sh",
    ], users
    text = (DEPLOY / overlay).read_text()
    # exactly one address and one port may be reached, and only by the worker
    assert "PHAROS_FETCH_TEST_ALLOWED_NETWORKS: 172.28.77.10/32" in text
    assert 'PHAROS_FETCH_TEST_ALLOWED_PORTS: "8080"' in text
    assert text.count("PHAROS_ENVIRONMENT: test") == 1
    assert "api:" not in text and "caddy:" not in text and "web:" not in text


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
    # The worker is the worker's environment plus the optional GitHub token and model key.
    worker = re.search(
        r"^  worker:\n.*?^    environment:\n((?:      [^\n]*\n)+)",
        compose,
        re.DOTALL | re.MULTILINE,
    )
    assert worker
    assert "<<: *worker-env" in worker.group(1)
    assert re.findall(r"^\s+(PHAROS_[A-Z_]+):", worker.group(1), re.MULTILINE) == [
        "PHAROS_GITHUB_TOKEN",
        "PHAROS_LLM_API_KEY",
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


# --- the local model profile (M4-10, ADR-0020) -----------------------------------------------


def networks_of(block: str) -> list[str]:
    """The networks of a service, written inline (`[a, b]`) or as a block (`a:` with options)."""
    inline = re.search(r"^    networks: \[(.*?)\]", block, re.MULTILINE)
    if inline:
        return [part.strip() for part in inline.group(1).split(",")]
    found = re.search(r"^    networks:\n((?:      .*\n?)+)", block, re.MULTILINE)
    assert found, "no networks"
    return re.findall(r"^      ([a-z][\w-]*):", found.group(1), re.MULTILINE)


def test_the_local_model_server_is_off_by_default_and_has_no_route_out() -> None:
    block = service_block("ollama")
    assert 'profiles: ["local-llm"]' in block
    assert networks_of(block) == ["llm"]  # not data, not egress, not app
    assert "ports:" not in block and "network_mode" not in block and "aliases" not in block
    # Hardened like the rest, pinned by digest, and not root.
    assert "<<: *hardening" in block
    assert re.search(r"image: ollama/ollama:\d+\.\d+\.\d+@sha256:[0-9a-f]{64}", block)
    assert 'user: "10001:10001"' in block
    assert "privileged" not in block and "cap_add" not in block
    # A fixed address on its own network, so the worker never depends on a name.
    assert "ipv4_address: ${PHAROS_OLLAMA_IP:-172.28.30.10}" in block
    # It only reads the models, and its memory, processor and models in use are bounded.
    assert "ollama_models:/models:ro" in block
    assert "mem_limit:" in block and "cpus:" in block
    assert 'OLLAMA_MAX_LOADED_MODELS: "1"' in block and 'OLLAMA_NUM_PARALLEL: "1"' in block


def test_the_llm_network_is_internal_and_only_the_worker_and_the_model_share_it() -> None:
    compose = (DEPLOY / "docker-compose.yml").read_text()
    assert re.search(r"^  llm:\n    internal: true\n    ipam:", compose, re.MULTILINE)
    assert "subnet: ${PHAROS_OLLAMA_SUBNET:-172.28.30.0/24}" in compose
    services = compose.split("\nservices:\n", 1)[1]
    on_llm = []
    for name in re.findall(r"^  ([a-z][\w-]*):\n", services, re.MULTILINE):
        block = service_block(name)
        if "network_mode: none" in block:
            continue
        if "llm" in networks_of(block):
            on_llm.append(name)
    assert sorted(on_llm) == ["ollama", "worker"]
    # The worker is told the fixed address (the API and the scheduler get it too, and never use it).
    assert "PHAROS_OLLAMA_IP: ${PHAROS_OLLAMA_IP:-172.28.30.10}" in worker_environment_block()


def test_the_model_download_is_a_separate_profile_with_a_route_out_and_no_data_access() -> None:
    block = service_block("ollama-pull")
    assert 'profiles: ["local-llm-pull"]' in block
    # Its own network: the only thing that may reach the internet here, and it shares nothing with
    # the API (api:8000 directly) or the worker while it runs.
    assert networks_of(block) == ["pull"]
    compose = (DEPLOY / "docker-compose.yml").read_text()
    assert re.search(r"^  pull: \{\}", compose, re.MULTILINE)
    others = [n for n in ("api", "worker", "beat", "web", "caddy", "postgres", "redis", "migrate")]
    for name in others:
        assert "pull" not in networks_of(service_block(name)), name
    assert "ports:" not in block
    assert "<<: *hardening" in block and 'user: "10001:10001"' in block
    # The server and the download share the one image, so they cannot drift apart.
    every = block + service_block("ollama") + service_block("ollama-init")
    pins = re.findall(r"image: (ollama/ollama:\S+)", every)
    assert len(pins) == 3 and len(set(pins)) == 1


def test_the_volume_helper_has_no_network_and_only_the_capability_chown_needs() -> None:
    block = service_block("ollama-init")
    assert "network_mode: none" in block and "networks:" not in block
    assert "cap_add: [CHOWN]" in block and "<<: *hardening" in block
    assert "ports:" not in block
    # Both model services wait for it, so the volume is theirs before either starts.
    assert "ollama-init" in service_block("ollama") and "ollama-init" in service_block(
        "ollama-pull"
    )


def test_nothing_waits_for_the_local_model_so_the_default_stack_starts_without_it() -> None:
    compose = (DEPLOY / "docker-compose.yml").read_text()
    services = compose.split("\nservices:\n", 1)[1]
    for name in re.findall(r"^  ([a-z][\w-]*):\n", services, re.MULTILINE):
        if name in {"ollama", "ollama-pull", "ollama-init"}:
            continue
        assert "ollama" not in service_block(name), name


def test_the_local_model_is_documented_for_operators() -> None:
    example = (DEPLOY / ".env.example").read_text()
    assert "ollama" in example and "PHAROS_OLLAMA_MEMORY" in example


# --- retention and the operator service (M5-09, ADR-0025) ---------------------------------------


def test_the_operator_service_is_started_on_demand_and_holds_what_it_needs_only_then() -> None:
    block = service_block("operator")
    assert re.search(r"^    profiles: \[operator\]$", block, re.MULTILINE)  # never by `up`
    assert re.search(r'^    restart: "no"$', block, re.MULTILINE)
    assert "ports:" not in block
    assert re.search(r"^    networks: \[data\]$", block, re.MULTILINE)  # no route out
    assert "<<: *hardening" in block
    assert "PHAROS_MIGRATIONS_DATABASE_URL" in block  # the owner: that is what it is for


def test_the_operator_service_has_its_own_narrow_environment() -> None:
    """It holds the owner's password, so it must not also hold the root key, the identity
    provider's secret, the application's database password, a model key or a GitHub token."""
    block = service_block("operator")
    assert "<<: *backend-env" not in block
    assert "<<: *worker-env" not in block
    names = set(re.findall(r"^      (PHAROS_\w+):", block, re.MULTILINE))
    assert names == {
        "PHAROS_ENVIRONMENT",
        "PHAROS_LOG_LEVEL",
        "PHAROS_ORG_SLUG",
        "PHAROS_MIGRATIONS_DATABASE_URL",
        "PHAROS_REDIS_URL",
        "PHAROS_AUDIT_RETENTION_DAYS",
        "PHAROS_INTERACTIONS_RETENTION_MONTHS",
    }
    for forbidden in (
        "SECRET_KEY",
        "OIDC",
        "DATABASE_URL: postgresql+asyncpg://pharos_app",
        "LLM",
        "GITHUB",
    ):
        assert forbidden not in block.replace("PHAROS_MIGRATIONS_DATABASE_URL", ""), forbidden
    # The Redis user is the API's (it must end sessions), never the worker's.
    assert "pharos_api:" in block and "pharos_worker" not in block


def test_only_the_migration_job_and_the_operator_service_hold_the_owners_credentials() -> None:
    compose = (DEPLOY / "docker-compose.yml").read_text()
    holders = {
        name
        for name in re.findall(r"^  (\w[\w-]*):\n", compose, re.MULTILINE)
        if "PHAROS_MIGRATIONS_DATABASE_URL" in service_block(name)
    }
    assert holders == {"migrate", "operator"}
    # And not through a shared environment anchor either.
    assert "PHAROS_MIGRATIONS_DATABASE_URL" not in environment_block("x-backend-env")
    assert "PHAROS_MIGRATIONS_DATABASE_URL" not in worker_environment_block()


def test_every_service_with_the_hardening_block_rotates_its_logs() -> None:
    compose = (DEPLOY / "docker-compose.yml").read_text()
    anchor = re.search(
        r"^x-hardening: &hardening\n(.*?)(?=\n\S)", compose, re.DOTALL | re.MULTILINE
    )
    assert anchor
    assert "driver: json-file" in anchor.group(1)
    assert re.search(r"max-size: \$\{PHAROS_LOG_MAX_SIZE:-\d+[kmg]\}", anchor.group(1))
    assert re.search(r'max-file: "\$\{PHAROS_LOG_MAX_FILE:-\d+\}"', anchor.group(1))


def test_the_log_and_retention_settings_are_documented_for_operators() -> None:
    example = (DEPLOY / ".env.example").read_text()
    for name in (
        "PHAROS_AUDIT_RETENTION_DAYS",
        "PHAROS_INTERACTIONS_RETENTION_MONTHS",
        "PHAROS_LOG_MAX_SIZE",
        "PHAROS_LOG_MAX_FILE",
    ):
        assert re.search(rf"^{name}=", example, re.MULTILINE), name


def test_the_retention_defaults_are_the_documented_ones_and_cannot_be_set_to_nothing() -> None:
    from pharos.core.config import Settings

    found = Settings(environment="test")  # type: ignore[arg-type]
    assert found.audit_retention_days == 365 and found.interactions_retention_months == 12
    for field, value in (
        ("audit_retention_days", 29),
        ("audit_retention_days", 0),
        ("interactions_retention_months", 2),
        ("interactions_retention_months", 0),
    ):
        with pytest.raises(ValueError, match=field):
            Settings(environment="test", **{field: value})  # type: ignore[arg-type]


def test_the_operator_settings_need_only_the_owner_url_and_refuse_a_short_retention() -> None:
    from pharos.core.config import OperatorSettings

    only = {"migrations_database_url": "postgresql+asyncpg://o:p@h/d"}
    found = OperatorSettings(**only)  # type: ignore[arg-type]
    assert found.audit_retention_days == 365 and found.interactions_retention_months == 12
    for field, value in (("audit_retention_days", 29), ("interactions_retention_months", 2)):
        with pytest.raises(ValueError, match=field):
            OperatorSettings(**only, **{field: value})  # type: ignore[arg-type]


# --- HSTS and the body limit at the proxy (M5-04) --------------------------------------------


def caddyfile_site() -> str:
    text = (DEPLOY / "Caddyfile").read_text()
    match = re.search(
        r"^\{\$PHAROS_DOMAIN:localhost\} \{\n(.*?)^\}", text, re.DOTALL | re.MULTILINE
    )
    assert match, "the main site block was not found"
    return match.group(1)


def test_caddy_sends_hsts_with_a_year_by_default_and_no_domain_wide_promises() -> None:
    site = caddyfile_site()
    line = re.search(r'Strict-Transport-Security "([^"]+)"', site)
    assert line, "no Strict-Transport-Security header"
    assert line.group(1) == "max-age={$PHAROS_HSTS_MAX_AGE:31536000}"
    directives = "\n".join(text for text in site.splitlines() if not text.strip().startswith("#"))
    assert "includeSubDomains" not in directives and "preload" not in directives


def test_caddy_refuses_request_bodies_over_one_mebibyte_for_the_whole_site() -> None:
    site = caddyfile_site()
    block = re.search(r"^\trequest_body \{\n\t\tmax_size (\S+)\n\t\}", site, re.MULTILINE)
    assert block and block.group(1) == "1MiB"
    # It sits at the top level of the site, before the handlers, so it covers /api and the web app.
    assert site.index("request_body") < site.index("handle /api/*")
    from pharos.core.bodylimit import MAX_BODY_BYTES

    assert MAX_BODY_BYTES == 1024 * 1024  # the API's own limit is the same size


def test_the_hsts_setting_reaches_caddy_and_is_documented() -> None:
    assert re.search(
        r"PHAROS_HSTS_MAX_AGE: \$\{PHAROS_HSTS_MAX_AGE:-31536000\}", service_block("caddy")
    )
    for other in ("api", "worker", "web"):
        assert "PHAROS_HSTS_MAX_AGE" not in service_block(other), other
    assert re.search(r"^PHAROS_HSTS_MAX_AGE=\d+$", (DEPLOY / ".env.example").read_text(), re.M)
    doc = (DEPLOY.parent / "docs" / "DEPLOYMENT.md").read_text()
    assert "`PHAROS_HSTS_MAX_AGE`" in doc


ROLES_SCRIPT = DEPLOY / "postgres" / "init" / "01-roles.sh"


def run_roles_script(tmp_path: Path, **passwords: str) -> subprocess.CompletedProcess[str]:
    """01-roles.sh with a stand-in psql that only says it ran."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    psql = bin_dir / "psql"
    psql.write_text("#!/bin/sh\ncat >/dev/null\necho PSQL-RAN\n")
    psql.chmod(psql.stat().st_mode | stat.S_IEXEC)
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    (data / "pg_hba.conf").write_text("local all all trust\n")
    env = {
        "PGDATA": str(data),
        "PATH": f"{bin_dir}:{os.environ.get('PATH', '/usr/bin:/bin')}",
        "POSTGRES_USER": "postgres",
        "POSTGRES_DB": "pharos",
        "PHAROS_OWNER_PASSWORD": "owner-0123abcd",
        "PHAROS_APP_PASSWORD": "app-0123abcd",
        "PHAROS_WORKER_DB_PASSWORD": "worker-0123abcd",
        **passwords,
    }
    return subprocess.run(  # noqa: S603
        ["sh", str(ROLES_SCRIPT)],  # noqa: S607
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_the_roles_script_makes_the_roles_with_plain_passwords(tmp_path: Path) -> None:
    done = run_roles_script(tmp_path)
    assert done.returncode == 0, done.stderr
    assert "PSQL-RAN" in done.stdout
    assert "host all postgres all reject" in (tmp_path / "data" / "pg_hba.conf").read_text()


@pytest.mark.parametrize(
    "name", ["PHAROS_OWNER_PASSWORD", "PHAROS_APP_PASSWORD", "PHAROS_WORKER_DB_PASSWORD"]
)
@pytest.mark.parametrize(
    "password", ["", "ab#Sentinel", "a/b", "a%41b", "a@b", "a b", "a$(touch x)b"]
)
def test_the_roles_script_refuses_a_password_unsafe_in_an_address_before_any_role(
    tmp_path: Path, name: str, password: str
) -> None:
    """F-09: the services put these passwords into their addresses as they are."""
    done = run_roles_script(tmp_path, **{name: password})
    assert done.returncode != 0
    assert f"{name} must be set and may contain only" in done.stderr
    assert "PSQL-RAN" not in done.stdout  # nothing was made
    assert "reject" not in (tmp_path / "data" / "pg_hba.conf").read_text()
    assert "Sentinel" not in done.stderr
    assert not (tmp_path / "x").exists()  # never run as a command
