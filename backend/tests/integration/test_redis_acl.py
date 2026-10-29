"""The worker's Redis user cannot reach sign-in state (follow-up F-14, ADR-0012).

The Redis under test is started by the same script as the deployed one (deploy/redis/start.sh).
Each refusal has a positive control: the API user does the same thing successfully, so a refusal is
the rules at work and not a broken connection.
"""

from collections.abc import Iterator

import pytest
from redis import Redis
from redis.exceptions import AuthenticationError, NoPermissionError

from pharos.core.heartbeat import HEARTBEAT_KEY

pytestmark = pytest.mark.integration

SIGN_IN_KEYS = [
    "pharos:session:abc",
    "pharos:session:id:abc",
    "pharos:rl:login:1",
    "pharos:oidc:xyz",
]


@pytest.fixture
def api(redis_url: str) -> Iterator[Redis]:
    client = Redis.from_url(redis_url)
    yield client
    client.delete(*SIGN_IN_KEYS, HEARTBEAT_KEY, "celery", "unacked", "other")
    client.close()


@pytest.fixture
def worker(redis_worker_url: str) -> Iterator[Redis]:
    client = Redis.from_url(redis_worker_url)
    yield client
    client.close()


@pytest.mark.parametrize("key", SIGN_IN_KEYS)
def test_the_worker_cannot_write_sign_in_state(api: Redis, worker: Redis, key: str) -> None:
    api.set(key, "ok")  # positive control: the API user may
    with pytest.raises(NoPermissionError):
        worker.set(key, "forged")
    assert api.get(key) == b"ok"


@pytest.mark.parametrize("key", SIGN_IN_KEYS)
def test_the_worker_cannot_read_or_delete_sign_in_state(
    api: Redis, worker: Redis, key: str
) -> None:
    api.set(key, "secret-session")
    with pytest.raises(NoPermissionError):
        worker.get(key)
    with pytest.raises(NoPermissionError):
        worker.delete(key)
    with pytest.raises(NoPermissionError):
        worker.expire(key, 1)
    assert api.get(key) == b"secret-session"


def test_the_worker_cannot_run_a_script_that_touches_sign_in_state(
    api: Redis, worker: Redis
) -> None:
    api.set("pharos:session:abc", "ok")
    script = "return redis.call('GET', KEYS[1])"
    assert api.eval(script, 1, "pharos:session:abc") == b"ok"  # positive control
    with pytest.raises(NoPermissionError):
        worker.eval(script, 1, "pharos:session:abc")
    # A key chosen inside the script is checked too, not only the declared ones.
    sneaky = "return redis.call('GET', 'pharos:session:abc')"
    with pytest.raises(Exception, match=r"(?i)permission|NOPERM"):
        worker.eval(sneaky, 1, "pharos:worker:heartbeat")


def test_the_worker_cannot_list_or_scan_other_keys(api: Redis, worker: Redis) -> None:
    api.set("pharos:session:abc", "ok")
    assert api.randomkey() is not None  # positive controls: the API user may list
    assert b"pharos:session:abc" in api.scan(match="pharos:session:*", count=1000)[1]
    # SCAN would show key names even for keys the user cannot read, so it is refused too.
    for refused in (
        lambda: worker.keys("*"),
        lambda: worker.scan(match="*", count=1000),
        lambda: worker.randomkey(),
    ):
        with pytest.raises(NoPermissionError):
            refused()


def with_db(url: str, database: int) -> str:
    return url.rsplit("/", 1)[0] + f"/{database}"


def test_the_sessions_database_is_closed_to_the_worker_too(
    redis_url: str, redis_worker_url: str
) -> None:
    """Sessions live in their own numbered database (PHAROS_SESSION_REDIS_DB=1)."""
    api = Redis.from_url(with_db(redis_url, 1))
    worker = Redis.from_url(with_db(redis_worker_url, 1))
    try:
        api.set("pharos:session:id:abc", "secret")
        assert api.get("pharos:session:id:abc") == b"secret"  # positive control
        for attempt in (
            lambda: worker.get("pharos:session:id:abc"),
            lambda: worker.set("pharos:session:id:abc", "forged"),
            lambda: worker.delete("pharos:session:id:abc"),
        ):
            with pytest.raises(NoPermissionError):
                attempt()
    finally:
        api.delete("pharos:session:id:abc")
        api.close()
        worker.close()


@pytest.mark.parametrize(
    "command",
    [
        ("COPY", "pharos:session:abc", "celery"),
        ("COPY", "celery", "pharos:session:abc"),
        ("RENAME", "pharos:session:abc", "celery"),
        ("RENAME", "celery", "pharos:session:abc"),
        ("MOVE", "pharos:session:abc", "1"),
        ("SORT", "pharos:session:abc", "BY", "nosort"),
        ("SORT", "celery", "GET", "pharos:session:*"),
        ("DUMP", "pharos:session:abc"),
        ("TYPE", "pharos:session:abc"),
        ("EXISTS", "pharos:session:abc"),
        ("OBJECT", "ENCODING", "pharos:session:abc"),
        ("DBSIZE",),
        ("SELECT", "1"),
        ("FUNCTION", "LOAD", "#!lua name=x\nredis.register_function('f', function() end)"),
        ("CLIENT", "TRACKING", "ON", "BCAST", "PREFIX", "pharos:session:"),
    ],
)
def test_the_worker_cannot_reach_sign_in_state_by_any_other_route(
    api: Redis, worker: Redis, command: tuple[str, ...]
) -> None:
    api.set("pharos:session:abc", "secret")
    api.set("celery", "queue")
    with pytest.raises(NoPermissionError):
        worker.execute_command(*command)
    assert api.get("pharos:session:abc") == b"secret"
    assert api.get("celery") == b"queue"


@pytest.mark.parametrize(
    "key",
    [
        "pharos:rl:login:x.celery.pidbox",
        "pharos:session:id:x.celery.pidbox",
        "pharos:session:id:x.reply.celery.pidbox",
        "pharos:oidc:x.reply.celery.pidbox",
    ],
)
def test_a_key_that_only_looks_like_a_pidbox_key_is_closed(worker: Redis, key: str) -> None:
    with pytest.raises(NoPermissionError):
        worker.set(key, "1")


def test_the_worker_can_use_the_queue_and_its_own_keys(worker: Redis, api: Redis) -> None:
    worker.lpush("celery", "task")
    worker.set("unacked", "1")
    worker.set(HEARTBEAT_KEY, "1.0", ex=60)
    assert api.llen("celery") == 1
    assert api.get(HEARTBEAT_KEY) == b"1.0"  # the API can read what /readyz needs
    assert worker.rpop("celery") == b"task"


def test_the_worker_cannot_use_other_keys_even_harmless_ones(worker: Redis) -> None:
    with pytest.raises(NoPermissionError):
        worker.set("other", "1")


@pytest.mark.parametrize(
    "command",
    [
        ("CONFIG", "GET", "*"),
        ("CONFIG", "SET", "maxmemory", "1"),
        ("ACL", "SETUSER", "pharos_worker", "+@all", "~*"),
        ("ACL", "LIST"),
        ("FLUSHALL",),
        ("FLUSHDB",),
        ("SHUTDOWN", "NOSAVE"),
        ("MONITOR",),
        ("SLAVEOF", "10.0.0.1", "6379"),
        ("MODULE", "LIST"),
    ],
)
def test_neither_user_can_run_dangerous_commands(
    api: Redis, worker: Redis, command: tuple[str, ...]
) -> None:
    for client in (api, worker):
        with pytest.raises(NoPermissionError):
            client.execute_command(*command)


def test_the_default_user_is_off(redis_url: str) -> None:
    host_port = redis_url.rsplit("@", 1)[1]
    anonymous = Redis.from_url(f"redis://{host_port}")
    with pytest.raises((AuthenticationError, NoPermissionError)):
        anonymous.ping()
    with pytest.raises(AuthenticationError):
        Redis.from_url(f"redis://default:anything@{host_port}").ping()
    with pytest.raises(AuthenticationError):
        Redis.from_url(f"redis://nobody:anything@{host_port}").ping()
    anonymous.close()


def test_the_worker_password_does_not_open_the_api_user(
    redis_worker_url: str, redis_url: str
) -> None:
    worker_password = redis_worker_url.split(":")[2].split("@")[0]
    host_port = redis_url.rsplit("@", 1)[1]
    with pytest.raises(AuthenticationError):
        Redis.from_url(f"redis://pharos_api:{worker_password}@{host_port}").ping()
