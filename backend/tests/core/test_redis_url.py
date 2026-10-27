import pytest

from pharos.core.redis import url_for_database


def test_the_database_number_is_replaced_and_credentials_are_kept() -> None:
    assert url_for_database("redis://:pw@cache:6379/0", 1) == "redis://:pw@cache:6379/1"
    assert url_for_database("redis://:pw@cache:6379", 3) == "redis://:pw@cache:6379/3"
    assert url_for_database("rediss://:pw@cache:6380/5", 1) == "rediss://:pw@cache:6380/1"


@pytest.mark.parametrize("query", ["?db=0", "?DB=0", "?db=0&health_check_interval=5"])
def test_a_db_query_parameter_cannot_move_sessions_to_another_database(query: str) -> None:
    result = url_for_database(f"redis://:pw@cache:6379/0{query}", 1)
    assert "db=" not in result.lower()
    assert result.startswith("redis://:pw@cache:6379/1")


def test_other_query_options_survive() -> None:
    result = url_for_database("redis://:pw@cache:6379/0?health_check_interval=5&db=0", 1)
    assert result == "redis://:pw@cache:6379/1?health_check_interval=5"


@pytest.mark.parametrize("url", ["unix:///var/run/redis.sock?db=0", "http://cache/0", "cache:6379"])
def test_only_network_redis_urls_are_accepted(url: str) -> None:
    with pytest.raises(ValueError, match="redis://"):
        url_for_database(url, 1)
