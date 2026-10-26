from typing import ClassVar

import pytest

from pharos.core.config import Settings
from pharos.core.heartbeat import HEARTBEAT_KEY
from pharos.workers import tasks

NOW = 1_800_000_000.0


class FakeRedis:
    instances: ClassVar[list["FakeRedis"]] = []

    def __init__(self, url: str) -> None:
        self.url = url
        self.stored: dict[str, str] = {}
        self.closed = False
        FakeRedis.instances.append(self)

    @classmethod
    def from_url(cls, url: str) -> "FakeRedis":
        return cls(url)

    def set(self, name: str, value: str, ex: int) -> None:
        self.stored[name] = value

    def close(self) -> None:
        self.closed = True


@pytest.fixture(autouse=True)
def fake_redis(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeRedis.instances = []
    monkeypatch.setattr(tasks, "Redis", FakeRedis)
    monkeypatch.setattr(
        tasks,
        "get_settings",
        lambda: Settings(environment="test", redis_url="redis://cache:6379/1"),  # type: ignore[arg-type]
    )
    monkeypatch.setattr(tasks.time, "time", lambda: NOW)


def test_write_heartbeat_stores_the_time_and_closes_the_connection() -> None:
    tasks.write_heartbeat()
    [client] = FakeRedis.instances
    assert client.url == "redis://cache:6379/1"
    assert float(client.stored[HEARTBEAT_KEY]) == NOW
    assert client.closed


def test_write_heartbeat_is_idempotent() -> None:
    tasks.write_heartbeat()
    tasks.write_heartbeat()
    assert [float(c.stored[HEARTBEAT_KEY]) for c in FakeRedis.instances] == [NOW, NOW]


def test_the_heartbeat_task_writes_a_heartbeat() -> None:
    tasks.heartbeat()
    assert len(FakeRedis.instances) == 1


def test_the_task_has_a_stable_name_and_ignores_its_result() -> None:
    assert tasks.heartbeat.name == "pharos.heartbeat"
    assert tasks.heartbeat.ignore_result is True
