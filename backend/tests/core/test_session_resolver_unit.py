"""The session resolver ends password sessions when password sign-in is off (unit level)."""

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from pharos.core.sessions import SessionIdentityResolver, SessionRecord

COOKIE = "__Host-pharos_session"


class _Store:
    def __init__(self, record: SessionRecord) -> None:
        self._record = record

    async def get(self, session_id: str) -> SessionRecord:
        return self._record


def _request() -> Any:
    return SimpleNamespace(cookies={COOKIE: "x" * 43}, state=SimpleNamespace())


def _record(method: str) -> SessionRecord:
    now = datetime.now(UTC)
    return SessionRecord(
        user_id=uuid.uuid4(),
        org_id=uuid.uuid4(),
        auth_time=now,
        created_at=now,
        csrf_seed="seed",
        method=method,
    )


@pytest.mark.anyio
@pytest.mark.parametrize("method", ["local", "LOCAL", "", "unknown"])
async def test_every_method_but_oidc_counts_as_password_when_it_is_switched_off(
    method: str,
) -> None:
    resolver = SessionIdentityResolver(_Store(_record(method)), COOKIE, allow_local=False)  # type: ignore[arg-type]
    assert await resolver.resolve(_request()) is None


@pytest.mark.anyio
async def test_an_sso_session_survives_password_sign_in_being_off() -> None:
    resolver = SessionIdentityResolver(_Store(_record("oidc")), COOKIE, allow_local=False)  # type: ignore[arg-type]
    assert await resolver.resolve(_request()) is not None


@pytest.mark.anyio
@pytest.mark.parametrize("method", ["local", "oidc"])
async def test_both_kinds_resolve_while_password_sign_in_is_on(method: str) -> None:
    resolver = SessionIdentityResolver(_Store(_record(method)), COOKIE, allow_local=True)  # type: ignore[arg-type]
    assert await resolver.resolve(_request()) is not None
