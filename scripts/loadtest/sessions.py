"""Opens one session per synthetic person and prints the session ids (M5-08).

Run inside the `api` container of the throwaway load stack, with the organization id and the user
ids on standard input (one per line, the organization first):

    docker compose ... exec -T api python -c "$(cat scripts/loadtest/sessions.py)" < ids.txt

It uses the application's own session store, so the sessions are what a single sign-on makes (the
break-glass mode accepts the password sessions of administrators only). The sign-in itself is not
what this run measures. The ids go into a file that only the k6 container reads.
"""

import asyncio
import sys
import uuid

from pharos.core.clock import SystemClock
from pharos.core.config import get_settings
from pharos.core.redis import url_for_database
from pharos.core.sessions import SessionStore
from redis.asyncio import Redis


async def main() -> None:
    lines = [line.strip() for line in sys.stdin if line.strip()]
    org = uuid.UUID(lines[0])
    people = [uuid.UUID(line) for line in lines[1:]]
    settings = get_settings()
    redis = Redis.from_url(
        url_for_database(settings.redis_url.get_secret_value(), settings.session_redis_db)
    )
    store = SessionStore(
        redis,
        SystemClock(),
        idle_seconds=settings.session_idle_seconds,
        absolute_seconds=settings.session_absolute_seconds,
    )
    for person in people:
        session_id, _ = await store.create(person, org, method="oidc")  # as after a single sign-on
        print(session_id)
    await redis.aclose()


asyncio.run(main())
