"""The test-only address allowance must be impossible to switch on outside the test environment."""

import pytest
from pydantic import ValidationError

from pharos.core.config import Settings


@pytest.mark.parametrize("environment", ["production", "development"])
@pytest.mark.parametrize(
    "extra",
    [
        {"fetch_test_allowed_networks": "127.0.0.1/32"},
        {"fetch_test_allowed_ports": "9000"},
    ],
)
def test_the_allowance_is_refused_outside_the_test_environment(
    environment: str, extra: dict[str, str]
) -> None:
    with pytest.raises(ValidationError, match="only accepted when environment=test"):
        Settings(environment=environment, **extra)  # type: ignore[arg-type]


def test_the_allowance_is_read_in_the_test_environment() -> None:
    settings = Settings(
        environment="test",
        fetch_test_allowed_networks="127.0.0.1/32, 10.1.0.0/16",
        fetch_test_allowed_ports="9000, 9001",
    )
    assert settings.fetch_test_allowed_networks_list == ["127.0.0.1/32", "10.1.0.0/16"]
    assert settings.fetch_test_allowed_ports_list == [9000, 9001]


def test_by_default_nothing_is_allowed() -> None:
    settings = Settings(environment="development")
    assert settings.fetch_test_allowed_networks_list == []
    assert settings.fetch_test_allowed_ports_list == []


def test_a_malformed_network_is_refused_at_start_up() -> None:
    with pytest.raises(ValidationError):
        Settings(environment="test", fetch_test_allowed_networks="not-a-network")
