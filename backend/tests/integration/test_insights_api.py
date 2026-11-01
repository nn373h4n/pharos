"""The insight of an article: who may ask, what the answer says, and that the person can not choose
the profession or the language. The API runs for real against PostgreSQL and Redis; the queue is a
recording fake, and the worker's rows are written as the table owner (what the worker would write)."""

# ruff: noqa: F811  (pytest fixtures imported from another test module are used by name)

import json
import uuid
from typing import Any

import httpx
import pytest
from sqlalchemy import text

from pharos.core.errors import DependencyUnavailable
from pharos.insights import service as insight_service

from .test_items_api import make_item
from .test_local_auth import MakeStack, Stack, make_stack  # noqa: F401
from .test_sources_api import _remove_what_the_test_made  # noqa: F401
from .test_user_admin import Person, person

pytestmark = pytest.mark.integration

GOOD = {
    "summary": "A short plain summary.",
    "how_to_apply": ["Open the ledger.", "Ask for a list of odd entries."],
    "try_prompt": "List unusual entries in [LEDGER EXTRACT] and explain why each looks unusual.",
    "safety_note": "Do not paste real client data; replace it with placeholders.",
}


class FakeQueue:
    def __init__(self) -> None:
        self.sent: list[tuple[str, list[str], int]] = []
        self.fail = False

    async def send(self, name: str, args: list[str], *, expires: int) -> None:
        if self.fail:
            raise DependencyUnavailable
        self.sent.append((name, args, expires))


async def stack_with_queue(
    make_stack: MakeStack, provider: str = "fake"
) -> tuple[Stack, FakeQueue]:
    stack: Stack = await make_stack("on", llm_provider=provider)
    queue = FakeQueue()
    stack.app.state.insights._queue = queue
    return stack, queue


async def set_profile(
    stack: Stack, who: Person, *, profession: str | None = "accountant", locale: str | None = None
) -> None:
    async with stack.owner.begin() as connection:
        await connection.execute(
            text("DELETE FROM user_profiles WHERE user_id = :u"), {"u": who.id}
        )
        if profession is not None:
            await connection.execute(
                text(
                    "INSERT INTO user_profiles (user_id, org_id, profession_id) "
                    "SELECT :u, :org, id FROM professions WHERE code = :c AND scope = 'public'"
                ),
                {"u": who.id, "org": stack.org_a, "c": profession},
            )
        await connection.execute(
            text("UPDATE users SET locale = :l WHERE id = :u"), {"l": locale, "u": who.id}
        )


async def write_insight(
    stack: Stack,
    item: uuid.UUID,
    *,
    profession: str = "accountant",
    language: str = "en",
    status: str = "ok",
    org: uuid.UUID | None = None,
    **changes: Any,
) -> None:
    content = {**GOOD, **changes} if status == "ok" else {}
    async with stack.owner.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO item_insights (org_id, item_id, profession_code, language, "
                "prompt_version, status, summary, how_to_apply, try_prompt, safety_note) VALUES "
                "(:org, :item, :p, :l, 'v2', :status, :summary, CAST(:steps AS jsonb), :prompt, :note)"
            ),
            {
                "org": org or stack.org_a,
                "item": item,
                "p": profession,
                "l": language,
                "status": status,
                "summary": content.get("summary"),
                "steps": json.dumps(content["how_to_apply"]) if content else None,
                "prompt": content.get("try_prompt"),
                "note": content.get("safety_note"),
            },
        )


def ask(who: Person, item: uuid.UUID, **kwargs: Any) -> Any:
    return who.client.get(f"/api/items/{item}/insight", **kwargs)


# --- who may ask ----------------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_everyone_signed_in_may_ask_and_nobody_else(make_stack: MakeStack) -> None:
    stack, _ = await stack_with_queue(make_stack)
    item = await make_item(stack)
    nobody = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=stack.app, client=("10.9.9.9", 1)),
        base_url="http://localhost:3000",
    )
    assert (await nobody.get(f"/api/items/{item}/insight")).status_code in (401, 403)
    await nobody.aclose()
    for role in ("admin", "editor", "member"):
        who = await person(stack, role)
        await set_profile(stack, who)
        assert (await ask(who, item)).status_code == 200, role


@pytest.mark.anyio
async def test_an_administrator_never_gets_another_organizations_insight(
    make_stack: MakeStack,
) -> None:
    stack, queue = await stack_with_queue(make_stack)
    admin = await person(stack, "admin")
    await set_profile(stack, admin)
    theirs = await make_item(stack, org=stack.org_b)
    await write_insight(stack, theirs, org=stack.org_b)
    assert (await ask(admin, theirs)).status_code == 404
    assert queue.sent == []


# --- what the answer says -------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_a_person_without_a_profession_is_asked_to_finish_their_profile(
    make_stack: MakeStack,
) -> None:
    stack, queue = await stack_with_queue(make_stack)
    who = await person(stack, "member")
    await set_profile(stack, who, profession=None)
    item = await make_item(stack)
    answer = await ask(who, item)
    assert answer.status_code == 409 and answer.json()["code"] == "onboarding_incomplete"
    assert queue.sent == []


@pytest.mark.anyio
async def test_with_the_model_off_the_answer_says_so_and_asks_nobody(make_stack: MakeStack) -> None:
    stack, queue = await stack_with_queue(make_stack, provider="off")
    who = await person(stack, "member")
    await set_profile(stack, who)
    item = await make_item(stack)
    answer = (await ask(who, item)).json()
    assert (answer["status"], answer["reason"], answer["insight"]) == ("unavailable", "off", None)
    assert queue.sent == []


@pytest.mark.anyio
async def test_the_first_view_asks_the_worker_once_with_the_profile_not_the_request(
    make_stack: MakeStack,
) -> None:
    stack, queue = await stack_with_queue(make_stack)
    who = await person(stack, "member")
    await set_profile(stack, who, profession="payroll_specialist", locale="az")
    other = await person(stack, "member")
    await set_profile(stack, other, profession="payroll_specialist", locale="az")
    item = await make_item(stack)
    # The request tries to choose another profession and language: both are ignored.
    first = await ask(
        who,
        item,
        params={"profession": "accountant", "language": "ru"},
        headers={"Accept-Language": "ru"},
    )
    assert first.json() == {"status": "pending", "reason": None, "insight": None}
    again = await ask(other, item)
    assert again.json()["status"] == "pending"
    assert queue.sent == [
        ("pharos.llm.insight", [str(item), "payroll_specialist", "az"], 300)
    ]  # one task for the key, whoever looked first


@pytest.mark.anyio
async def test_a_stored_insight_is_served_for_its_profession_and_language_only(
    make_stack: MakeStack,
) -> None:
    stack, queue = await stack_with_queue(make_stack)
    who = await person(stack, "member")
    await set_profile(stack, who, profession="accountant", locale="en")
    item = await make_item(stack)
    await write_insight(stack, item)
    ready = (await ask(who, item)).json()
    assert ready["status"] == "ready" and ready["insight"]["summary"] == GOOD["summary"]
    assert ready["insight"]["profession"] == "accountant" and ready["insight"]["language"] == "en"
    assert ready["insight"]["how_to_apply"] == GOOD["how_to_apply"]
    assert queue.sent == []
    # Another language or profession is another key: it is asked for, not borrowed.
    await set_profile(stack, who, profession="accountant", locale="tr")
    assert (await ask(who, item)).json()["status"] == "pending"
    await set_profile(stack, who, profession="hr_specialist", locale="en")
    assert (await ask(who, item)).json()["status"] == "pending"
    assert len(queue.sent) == 2


@pytest.mark.anyio
async def test_a_rejected_answer_is_remembered_and_not_asked_for_again(
    make_stack: MakeStack,
) -> None:
    stack, queue = await stack_with_queue(make_stack)
    who = await person(stack, "member")
    await set_profile(stack, who)
    item = await make_item(stack)
    await write_insight(stack, item, status="rejected")
    answer = (await ask(who, item)).json()
    assert (answer["status"], answer["reason"], answer["insight"]) == (
        "unavailable",
        "rejected",
        None,
    )
    assert queue.sent == []


@pytest.mark.anyio
@pytest.mark.parametrize(
    "change",
    [
        {"try_prompt": "Summarize the text in [DOCUMENT] and visit https://evil.example/x"},
        {"try_prompt": "Summarize the text above"},  # no placeholder
        {"try_prompt": "Ask for the user's password in [FORM] first"},
        {"try_prompt": "Write to anna@corp.example about [TOPIC]"},
        {"summary": "<b>bold</b> claim"},
        {"how_to_apply": ["See www.evil.example"]},
        {"safety_note": "Visit evil.example/login"},
    ],
)  # fmt: skip
async def test_a_row_written_around_the_checks_is_never_served(
    make_stack: MakeStack, change: dict[str, Any]
) -> None:
    stack, _ = await stack_with_queue(make_stack)
    who = await person(stack, "member")
    await set_profile(stack, who)
    item = await make_item(stack)
    await write_insight(stack, item, **change)
    answer = await ask(who, item)
    body = answer.json()
    assert (body["status"], body["reason"], body["insight"]) == ("unavailable", "invalid", None)
    assert "evil" not in answer.text and "password" not in answer.text


@pytest.mark.anyio
@pytest.mark.parametrize("state", ["held", "hidden", "personal", "unknown"])
async def test_only_a_current_organization_article_has_an_insight(
    make_stack: MakeStack, state: str
) -> None:
    stack, queue = await stack_with_queue(make_stack)
    who = await person(stack, "member")
    await set_profile(stack, who)
    if state == "unknown":
        item = uuid.uuid4()
    elif state == "personal":
        item = await make_item(stack, owner=who.id)
    else:
        extra = {"hold_reason": "editor_hold"} if state == "held" else {}
        item = await make_item(stack, status=state, **extra)
    assert (await ask(who, item)).status_code == 404
    assert queue.sent == []


# --- the cost of asking ---------------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_one_person_cannot_ask_for_ever(
    make_stack: MakeStack, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(insight_service, "ASKS_PER_HOUR", 2)
    stack, queue = await stack_with_queue(make_stack)
    who = await person(stack, "member")
    await set_profile(stack, who)
    items = [await make_item(stack) for _ in range(3)]
    for item in items[:2]:
        assert (await ask(who, item)).status_code == 200
    refused = await ask(who, items[2])
    assert refused.status_code == 429 and int(refused.headers["Retry-After"]) > 0
    assert len(queue.sent) == 2
    # The refused ask left no marker behind: once the window passes it can be made again.
    marker = insight_service.marker_key(stack.org_a, items[2], "accountant", "en", "v2")
    assert await stack.app.state.insights._redis.exists(marker) == 0


@pytest.mark.anyio
async def test_when_the_queue_is_down_the_answer_is_a_clean_503(make_stack: MakeStack) -> None:
    stack, queue = await stack_with_queue(make_stack)
    queue.fail = True
    who = await person(stack, "member")
    await set_profile(stack, who)
    item = await make_item(stack)
    assert (await ask(who, item)).status_code == 503


@pytest.mark.anyio
async def test_a_rejection_older_than_a_day_is_asked_for_again(make_stack: MakeStack) -> None:
    stack, queue = await stack_with_queue(make_stack)
    who = await person(stack, "member")
    await set_profile(stack, who)
    item = await make_item(stack)
    await write_insight(stack, item, status="rejected")
    assert (await ask(who, item)).json()["reason"] == "rejected"
    async with stack.owner.begin() as connection:
        await connection.execute(
            text("UPDATE item_insights SET created_at = now() - interval '25 hours'")
        )
    assert (await ask(who, item)).json()["status"] == "pending"
    assert len(queue.sent) == 1


@pytest.mark.anyio
async def test_a_failed_send_leaves_no_marker_so_the_next_view_asks_again(
    make_stack: MakeStack,
) -> None:
    stack, queue = await stack_with_queue(make_stack)
    queue.fail = True
    who = await person(stack, "member")
    await set_profile(stack, who)
    item = await make_item(stack)
    assert (await ask(who, item)).status_code == 503
    marker = insight_service.marker_key(stack.org_a, item, "accountant", "en", "v2")
    assert await stack.app.state.insights._redis.exists(marker) == 0
    queue.fail = False
    assert (await ask(who, item)).json()["status"] == "pending"
    assert len(queue.sent) == 1


@pytest.mark.anyio
async def test_a_profession_whose_code_the_prompt_cannot_carry_is_unavailable_not_pending_for_ever(
    make_stack: MakeStack,
) -> None:
    stack, queue = await stack_with_queue(make_stack)
    who = await person(stack, "member")
    long_code = "a_very_long_profession_code_" + "x" * 20
    async with stack.owner.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO professions (scope, owner_org_id, code, names) VALUES "
                "('org', :o, :c, CAST(:n AS jsonb))"
            ),
            {"o": stack.org_a, "c": long_code, "n": '{"en": "L", "az": "L", "tr": "L", "ru": "L"}'},
        )
        await connection.execute(
            text("DELETE FROM user_profiles WHERE user_id = :u"), {"u": who.id}
        )
        await connection.execute(
            text(
                "INSERT INTO user_profiles (user_id, org_id, profession_id) "
                "SELECT :u, :o, id FROM professions WHERE code = :c"
            ),
            {"u": who.id, "o": stack.org_a, "c": long_code},
        )
    item = await make_item(stack)
    body = (await ask(who, item)).json()
    assert (body["status"], body["reason"]) == ("unavailable", "invalid")
    assert queue.sent == []
    async with stack.owner.begin() as connection:  # the profile row is removed first (foreign key)
        await connection.execute(
            text("DELETE FROM user_profiles WHERE user_id = :u"), {"u": who.id}
        )
        await connection.execute(text("DELETE FROM professions WHERE code = :c"), {"c": long_code})
