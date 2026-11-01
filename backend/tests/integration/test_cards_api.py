"""The Home feed and what people do with the cards: who sees which articles in which order, that the
order holds while paging, and that interactions are private to the person (ADR-0018). The API runs
for real against PostgreSQL and Redis."""

# ruff: noqa: F811  (pytest fixtures imported from another test module are used by name)

import json
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from pharos.cards import service as card_service

from .test_insights_api import set_profile
from .test_items_api import make_item
from .test_local_auth import MakeStack, Stack, make_stack  # noqa: F401
from .test_sources_api import _remove_what_the_test_made  # noqa: F401
from .test_user_admin import Person, person

pytestmark = pytest.mark.integration


def hours_ago(hours: float) -> datetime:
    return datetime.now(UTC) - timedelta(hours=hours)


async def card(
    stack: Stack,
    *,
    title: str = "A story",
    hours: float = 1,
    category: str = "news",
    quality: int = 3,
    fit: dict[str, int] | None = None,
    org: uuid.UUID | None = None,
    **item: Any,
) -> uuid.UUID:
    """An article the model has read: it has a summary, a category, a quality and professions."""
    found = await make_item(stack, org=org, title=title, published_at=hours_ago(hours), **item)
    async with stack.owner.begin() as connection:
        await connection.execute(
            text(
                "UPDATE items SET summary = :s, category = :c, quality = :q, enriched_at = now(), "
                "enrichment_version = 'v1' WHERE id = :i"
            ),
            {"s": f"Summary of {title}", "c": category, "q": quality, "i": found},
        )
        for code, relevance in (fit or {}).items():
            await connection.execute(
                text(
                    "INSERT INTO item_professions (org_id, item_id, profession_code, relevance) "
                    "VALUES (:o, :i, :c, :r)"
                ),
                {"o": org or stack.org_a, "i": found, "c": code, "r": relevance},
            )
    return found


async def stack_and_person(
    make_stack: MakeStack,
    role: str = "member",
    *,
    profession: str | None = "accountant",
    level: str | None = None,
    interests: list[str] | None = None,
) -> tuple[Stack, Person]:
    stack: Stack = await make_stack("on")
    who = await person(stack, role)
    await set_profile(stack, who, profession=profession)
    if level or interests:
        async with stack.owner.begin() as connection:
            await connection.execute(
                text(
                    "UPDATE user_profiles SET ai_level = :l, interests = CAST(:i AS jsonb) "
                    "WHERE user_id = :u"
                ),
                {"l": level, "i": json.dumps(interests or []), "u": who.id},
            )
    return stack, who


def titles(page: dict[str, Any]) -> list[str]:
    return [c["title"] for c in page["items"]]


async def feed(who: Person, **params: Any) -> dict[str, Any]:
    answer = await who.client.get("/api/cards", params=params)
    assert answer.status_code == 200, answer.text
    return answer.json()  # type: ignore[no-any-return]


# --- who sees which articles ----------------------------------------------------------------------


@pytest.mark.anyio
async def test_everyone_signed_in_has_a_feed_and_nobody_else(make_stack: MakeStack) -> None:
    stack: Stack = await make_stack("on")
    nobody = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=stack.app, client=("10.9.9.9", 1)),
        base_url="http://localhost:3000",
    )
    assert (await nobody.get("/api/cards")).status_code in (401, 403)
    await nobody.aclose()
    await card(stack)
    for role in ("admin", "editor", "member"):
        who = await person(stack, role)
        await set_profile(stack, who)
        assert len((await feed(who))["items"]) == 1, role


@pytest.mark.anyio
async def test_an_administrator_never_sees_another_organizations_cards(
    make_stack: MakeStack,
) -> None:
    stack, admin = await stack_and_person(make_stack, "admin")
    await card(stack, title="ours")
    await card(stack, title="theirs", org=stack.org_b)
    assert titles(await feed(admin)) == ["ours"]


@pytest.mark.anyio
async def test_a_person_without_a_profession_is_asked_to_finish_their_profile(
    make_stack: MakeStack,
) -> None:
    _stack, who = await stack_and_person(make_stack, profession=None)
    answer = await who.client.get("/api/cards")
    assert answer.status_code == 409 and answer.json()["code"] == "onboarding_incomplete"


@pytest.mark.anyio
async def test_only_current_public_articles_the_model_has_read_are_cards(
    make_stack: MakeStack,
) -> None:
    stack, who = await stack_and_person(make_stack)
    await card(stack, title="shown")
    await card(stack, title="held", status="held", hold_reason="editor_hold")
    await card(stack, title="hidden", status="hidden")
    await card(stack, title="personal", owner=who.id)
    await make_item(stack, title="unread")  # no summary yet
    assert titles(await feed(who)) == ["shown"]


@pytest.mark.anyio
async def test_a_card_has_a_host_and_plain_text_and_the_score_only_for_reviewers(
    make_stack: MakeStack,
) -> None:
    stack, member = await stack_and_person(make_stack)
    editor = await person(stack, "editor")
    await set_profile(stack, editor)
    await card(stack, title="One", quality=4)
    [seen] = (await feed(member))["items"]
    assert seen["host"] == "news.example.org" and seen["summary"] == "Summary of One"
    assert seen["quality"] is None and seen["saved"] is False
    assert (await feed(editor))["items"][0]["quality"] == 4


# --- the order ------------------------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_the_professions_fit_comes_first(make_stack: MakeStack) -> None:
    stack, accountant = await stack_and_person(make_stack, profession="accountant")
    hr = await person(stack, "member")
    await set_profile(stack, hr, profession="hr_specialist")
    await card(stack, title="for accountants", fit={"accountant": 90, "hr_specialist": 10})
    await card(stack, title="for hr", fit={"hr_specialist": 90, "accountant": 10})
    await card(stack, title="for nobody")
    assert titles(await feed(accountant)) == ["for accountants", "for hr", "for nobody"]
    assert titles(await feed(hr)) == ["for hr", "for accountants", "for nobody"]


@pytest.mark.anyio
async def test_newer_and_better_articles_rank_higher_when_the_fit_is_equal(
    make_stack: MakeStack,
) -> None:
    stack, who = await stack_and_person(make_stack)
    await card(stack, title="old", hours=24 * 30)
    await card(stack, title="new", hours=1)
    await card(stack, title="new but poor", hours=1, quality=1)
    assert titles(await feed(who)) == ["new", "new but poor", "old"]


@pytest.mark.anyio
async def test_interests_and_level_nudge_the_order(make_stack: MakeStack) -> None:
    stack, who = await stack_and_person(make_stack, level="beginner", interests=["learning"])
    await card(stack, title="policy", category="policy")
    await card(stack, title="tutorial", category="tutorial")
    assert titles(await feed(who)) == ["tutorial", "policy"]
    advanced = await person(stack, "member")
    await set_profile(stack, advanced)
    async with stack.owner.begin() as connection:
        await connection.execute(
            text("UPDATE user_profiles SET ai_level = 'advanced' WHERE user_id = :u"),
            {"u": advanced.id},
        )
    assert titles(await feed(advanced)) == ["policy", "tutorial"]


@pytest.mark.anyio
async def test_a_new_person_gets_at_least_twenty_cards_even_when_none_fit(
    make_stack: MakeStack,
) -> None:
    stack, who = await stack_and_person(make_stack)
    for n in range(25):
        await card(stack, title=f"story {n:02d}", hours=n + 1)
    assert len((await feed(who))["items"]) == 20


# --- paging -----------------------------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_paging_gives_every_card_once_in_one_order_even_while_time_passes(
    make_stack: MakeStack,
) -> None:
    stack, who = await stack_and_person(make_stack)
    for n in range(45):
        await card(stack, title=f"s{n:02d}", hours=n * 3 + 1, quality=1 + n % 5)
    everything = titles(await feed(who, limit=50))
    assert len(everything) == 45
    seen: list[str] = []
    cursor: str | None = None

    class Later:
        def __init__(self, base: Any, minutes: int) -> None:
            self._base, self._minutes = base, minutes

        def now(self) -> datetime:
            return self._base.now() + timedelta(minutes=self._minutes)

    service = stack.app.state.cards
    original = service._clock
    pages = 0
    while True:
        page = await feed(who, limit=20, **({"cursor": cursor} if cursor else {}))
        seen += titles(page)
        pages += 1
        service._clock = Later(original, 10 * pages)  # ten minutes pass between pages
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert pages == 3 and seen == everything and len(set(seen)) == 45


@pytest.mark.anyio
async def test_a_cursor_that_is_malformed_forged_or_stale_is_refused(make_stack: MakeStack) -> None:
    stack, who = await stack_and_person(make_stack)
    for n in range(3):
        await card(stack, title=f"s{n}")
    first = await feed(who, limit=1)
    good = first["next_cursor"]
    assert good and (await who.client.get("/api/cards", params={"cursor": good})).status_code == 200
    service = stack.app.state.cards

    class Later:
        def now(self) -> datetime:
            return datetime.now(UTC) + timedelta(hours=2)

    original = service._clock
    service._clock = Later()
    stale = await who.client.get("/api/cards", params={"cursor": good})
    service._clock = original
    assert stale.status_code == 422
    for bad in ("x", "!!!!", "e30", "a" * 300, "eyJhIjoiMSIsInMiOjEsImkiOiJ4In0"):
        answer = await who.client.get("/api/cards", params={"cursor": bad})
        assert answer.status_code == 422, bad


# --- interactions -------------------------------------------------------------------------------------------------


async def put(who: Person, item: uuid.UUID, kind: str, **body: Any) -> httpx.Response:
    return await who.client.put(
        f"/api/items/{item}/interactions/{kind}", headers=who.headers, json=body or None
    )


@pytest.mark.anyio
async def test_saving_marks_the_card_and_is_undone_and_idempotent(make_stack: MakeStack) -> None:
    stack, who = await stack_and_person(make_stack)
    item = await card(stack)
    assert (await put(who, item, "save")).status_code == 204
    assert (await put(who, item, "save")).status_code == 204
    assert (await feed(who))["items"][0]["saved"] is True
    undone = await who.client.delete(f"/api/items/{item}/interactions/save", headers=who.headers)
    assert undone.status_code == 204
    assert (await feed(who))["items"][0]["saved"] is False


@pytest.mark.anyio
async def test_not_useful_removes_the_card_and_useful_takes_it_back(make_stack: MakeStack) -> None:
    stack, who = await stack_and_person(make_stack)
    keep = await card(stack, title="keep")
    drop = await card(stack, title="drop")
    assert (await put(who, drop, "not_useful")).status_code == 204
    assert titles(await feed(who)) == ["keep"]
    assert (await put(who, drop, "useful")).status_code == 204  # replaces not_useful
    assert sorted(titles(await feed(who))) == ["drop", "keep"]
    del keep


@pytest.mark.anyio
async def test_time_saved_needs_minutes_and_nothing_else_may_carry_them(
    make_stack: MakeStack,
) -> None:
    stack, who = await stack_and_person(make_stack)
    item = await card(stack)
    assert (await put(who, item, "time_saved", value=30)).status_code == 204
    assert (await put(who, item, "time_saved", value=45)).status_code == 204  # replaces
    for kind, body in (("time_saved", {}), ("save", {"value": 5}), ("time_saved", {"value": 0}),
                       ("time_saved", {"value": 481})):  # fmt: skip
        assert (await put(who, item, kind, **body)).status_code == 422, (kind, body)
    assert (await put(who, item, "applaud")).status_code == 422
    async with stack.owner.connect() as connection:
        value = (
            await connection.execute(
                text("SELECT value FROM interactions WHERE item_id = :i AND type = 'time_saved'"),
                {"i": item},
            )
        ).scalar_one()
    assert value == 45


@pytest.mark.anyio
async def test_interactions_need_the_csrf_token_and_a_readable_article(
    make_stack: MakeStack,
) -> None:
    stack, who = await stack_and_person(make_stack)
    item = await card(stack)
    no_token = await who.client.put(f"/api/items/{item}/interactions/save")
    assert no_token.status_code == 403 and no_token.json()["code"] == "csrf_failed"
    for hidden in (
        await card(stack, status="hidden"),
        await card(stack, owner=who.id),
        await card(stack, org=stack.org_b),
        uuid.uuid4(),
    ):
        assert (await put(who, hidden, "save")).status_code == 404


@pytest.mark.anyio
async def test_one_person_cannot_press_buttons_for_ever(
    make_stack: MakeStack, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(card_service, "INTERACTIONS_PER_HOUR", 2)
    stack, who = await stack_and_person(make_stack)
    item = await card(stack)
    assert (await put(who, item, "save")).status_code == 204
    assert (await put(who, item, "view")).status_code == 204
    refused = await put(who, item, "tried")
    assert refused.status_code == 429 and int(refused.headers["Retry-After"]) > 0


# --- privacy (ADR-0018) ------------------------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_what_one_person_did_is_invisible_to_everybody_else(make_stack: MakeStack) -> None:
    stack, alice = await stack_and_person(make_stack)
    bob = await person(stack, "admin")
    await set_profile(stack, bob)
    item = await card(stack)
    await put(alice, item, "save")
    await put(alice, item, "not_useful")
    assert titles(await feed(alice)) == []
    assert titles(await feed(bob)) == ["A story"]  # alice's dislike is hers
    assert (await feed(bob))["items"][0]["saved"] is False


@pytest.mark.anyio
async def test_the_database_itself_shows_a_person_only_their_own_rows(
    make_stack: MakeStack, app_engine: AsyncEngine
) -> None:
    stack, alice = await stack_and_person(make_stack)
    bob = await person(stack, "member")
    item = await card(stack)
    await put(alice, item, "save")
    await put(alice, item, "time_saved", value=20)

    async def rows_seen_by(user: uuid.UUID | None) -> int:
        async with app_engine.begin() as connection:
            await connection.execute(
                text("SELECT set_config('app.org_id', :o, true)"), {"o": str(stack.org_a)}
            )
            if user is not None:
                await connection.execute(
                    text("SELECT set_config('app.user_id', :u, true)"), {"u": str(user)}
                )
            return int(
                (await connection.execute(text("SELECT count(*) FROM interactions"))).scalar_one()
            )

    assert await rows_seen_by(alice.id) == 2  # control
    assert await rows_seen_by(bob.id) == 0
    assert await rows_seen_by(None) == 0  # nobody named: nothing
    async with app_engine.begin() as connection:  # and nobody can write as somebody else
        await connection.execute(
            text("SELECT set_config('app.org_id', :o, true)"), {"o": str(stack.org_a)}
        )
        await connection.execute(
            text("SELECT set_config('app.user_id', :u, true)"), {"u": str(bob.id)}
        )
        with pytest.raises(Exception, match="row-level security"):
            await connection.execute(
                text(
                    "INSERT INTO interactions (org_id, user_id, item_id, type) "
                    "VALUES (:o, :u, :i, 'save')"
                ),
                {"o": stack.org_a, "u": alice.id, "i": item},
            )


async def many_people(stack: Stack, item: uuid.UUID, n: int, kind: str = "useful") -> None:
    for _ in range(n):
        who = await person(stack, "member")
        await set_profile(stack, who)
        assert (await put(who, item, kind)).status_code == 204


async def yesterday(stack: Stack) -> None:
    """Counts cover complete days only: move everything done so far to yesterday."""
    async with stack.owner.begin() as connection:
        await connection.execute(
            text("UPDATE interactions SET created_at = now() - interval '1 day'")
        )


@pytest.mark.anyio
async def test_the_organization_sees_counts_only_and_nothing_for_a_small_group(
    make_stack: MakeStack,
) -> None:
    stack, admin = await stack_and_person(make_stack, "admin")
    item = await card(stack)
    await many_people(stack, item, 4)  # four people: below the floor of five
    await yesterday(stack)
    assert (await admin.client.get("/api/interactions/summary")).json()["counts"] == []
    await many_people(stack, item, 1)
    await yesterday(stack)
    full = (await admin.client.get("/api/interactions/summary")).json()
    [useful] = [c for c in full["counts"] if c["type"] == "useful"]
    assert (useful["people"], useful["actions"]) == (5, 5)
    assert set(useful) == {"day", "type", "people", "actions", "minutes"}


@pytest.mark.anyio
async def test_today_is_never_counted_so_a_live_figure_cannot_be_differenced(
    make_stack: MakeStack,
) -> None:
    stack, admin = await stack_and_person(make_stack, "admin")
    item = await card(stack)
    await many_people(stack, item, 6)  # six people, but all of it today
    assert (await admin.client.get("/api/interactions/summary")).json()["counts"] == []


@pytest.mark.anyio
async def test_minutes_are_capped_per_person_and_rounded(make_stack: MakeStack) -> None:
    stack, admin = await stack_and_person(make_stack, "admin")
    item = await card(stack)
    # 10 + 20 + 30 + 40 + 480: the outlier counts as 120, and the total 220 is rounded to 210
    for minutes in (10, 20, 30, 40, 480):
        who = await person(stack, "member")
        await set_profile(stack, who)
        assert (await put(who, item, "time_saved", value=minutes)).status_code == 204
    await yesterday(stack)
    counts = (await admin.client.get("/api/interactions/summary")).json()["counts"]
    [saved] = [c for c in counts if c["type"] == "time_saved"]
    assert (saved["people"], saved["minutes"]) == (5, 210)


@pytest.mark.anyio
async def test_a_row_never_moves_to_another_day_and_minutes_are_not_edited_after_the_day(
    make_stack: MakeStack,
) -> None:
    stack, who = await stack_and_person(make_stack)
    item = await card(stack)
    await put(who, item, "time_saved", value=30)
    async with stack.owner.begin() as connection:
        await connection.execute(
            text("UPDATE interactions SET created_at = now() - interval '2 days'")
        )

    async def row() -> Any:
        async with stack.owner.connect() as connection:
            return (
                await connection.execute(
                    text("SELECT value, created_at FROM interactions WHERE type = 'time_saved'")
                )
            ).one()

    before = await row()
    assert (await put(who, item, "time_saved", value=99)).status_code == 204  # accepted, ignored
    after = await row()
    assert (after.value, after.created_at) == (30, before.created_at)
    # the same day: a correction is taken and the day stays
    async with stack.owner.begin() as connection:
        await connection.execute(text("UPDATE interactions SET created_at = now()"))
    today = await row()
    await put(who, item, "time_saved", value=45)
    corrected = await row()
    assert (corrected.value, corrected.created_at) == (45, today.created_at)
    await put(who, item, "view")
    await put(who, item, "view")
    async with stack.owner.connect() as connection:
        views = (
            (
                await connection.execute(
                    text("SELECT created_at FROM interactions WHERE type='view'")
                )
            )
            .scalars()
            .all()
        )
    assert len(views) == 1


@pytest.mark.anyio
async def test_the_function_gives_the_caller_no_control_over_the_window_or_the_day(
    make_stack: MakeStack, app_engine: AsyncEngine
) -> None:
    stack, admin = await stack_and_person(make_stack, "admin")
    item = await card(stack)
    await many_people(stack, item, 5)
    await yesterday(stack)

    async def answer(days: int, zone: str = "UTC") -> list[tuple[Any, ...]]:
        async with app_engine.begin() as connection:
            await connection.execute(
                text("SELECT set_config('app.org_id', :o, true)"), {"o": str(stack.org_a)}
            )
            await connection.execute(text(f"SET LOCAL TimeZone = '{zone}'"))
            rows = await connection.execute(
                text("SELECT * FROM pharos_interaction_counts(:d)"), {"d": days}
            )
            return [tuple(r) for r in rows]

    baseline = await answer(30)
    assert baseline  # control
    for days in (1, 2, 7, 90, 10_000, 0, -5):  # clamped to 1..90, and yesterday is inside any
        assert await answer(days) == baseline, days
    for zone in ("Pacific/Kiritimati", "America/Los_Angeles", "Asia/Baku"):
        assert await answer(30, zone) == baseline, zone
    del admin


@pytest.mark.anyio
async def test_the_summary_is_for_administrators_only_and_an_administrator_sees_no_rows(
    make_stack: MakeStack,
) -> None:
    stack, member = await stack_and_person(make_stack)
    editor = await person(stack, "editor")
    for who in (member, editor):
        assert (await who.client.get("/api/interactions/summary")).status_code == 403
    admin = await person(stack, "admin")
    item = await card(stack)
    await put(member, item, "save")
    body = (await admin.client.get("/api/interactions/summary")).text
    assert str(member.id) not in body and str(item) not in body


@pytest.mark.anyio
async def test_the_feed_stays_fast_with_a_realistic_pile_of_articles(make_stack: MakeStack) -> None:
    """Acceptance: p95 under 400 ms on the pilot data size (a few thousand articles)."""
    stack, who = await stack_and_person(make_stack)
    async with stack.owner.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO items (org_id, canonical_url, url_hash, title, content_sha256, "
                "published_at, summary, category, quality, enriched_at, enrichment_version) "
                "SELECT :org, 'https://news.example.org/p' || g, md5(g::text) || md5('x' || g), "
                "'Pile ' || g, md5(g::text) || md5('y' || g), now() - (g || ' minutes')::interval, "
                "'Summary ' || g, (ARRAY['tool','technique','tutorial','news'])[1 + g % 4], "
                "1 + g % 5, now(), 'v1' FROM generate_series(1, 3000) g"
            ),
            {"org": stack.org_a},
        )
        await connection.execute(
            text(
                "INSERT INTO item_professions (org_id, item_id, profession_code, relevance) "
                "SELECT org_id, id, 'accountant', (random() * 100)::int FROM items "
                "WHERE title LIKE 'Pile %' AND random() < 0.4"
            )
        )
    async with stack.owner.connect() as connection:  # what autovacuum does in a real installation
        await connection.execution_options(isolation_level="AUTOCOMMIT")
        for table in ("items", "item_professions"):
            await connection.execute(text(f"ANALYZE {table}"))
    await feed(who)  # warm-up
    timings = []
    for _ in range(30):
        started = time.perf_counter()
        await feed(who)
        timings.append(time.perf_counter() - started)
    timings.sort()
    p95 = timings[int(len(timings) * 0.95) - 1]
    print(f"CARDS p95 {p95 * 1000:.0f} ms, median {timings[len(timings) // 2] * 1000:.0f} ms")
    assert p95 < 0.4, f"p95 {p95:.3f}s"


@pytest.mark.anyio
async def test_the_feed_query_filters_by_organization_itself_not_only_through_row_security(
    make_stack: MakeStack,
) -> None:
    """As the table owner, which row-level security does not apply to: only the statement stands
    between one organization and another's article."""
    from pharos.cards import repository

    stack, who = await stack_and_person(make_stack)
    await card(stack, title="ours")
    await card(stack, title="theirs", org=stack.org_b)
    from sqlalchemy.ext.asyncio import AsyncSession

    async with stack.owner.connect() as connection:
        session = AsyncSession(bind=connection)
        rows = await repository.ranked_cards(
            session,
            stack.org_a,
            who.id,
            profession="accountant",
            interest_categories=[],
            level_categories=[],
            as_of=datetime.now(UTC),
            after=None,
            limit=50,
        )
    assert [r.title for r in rows] == ["ours"]


@pytest.mark.anyio
async def test_cards_with_the_same_score_are_paged_without_gaps_or_repeats(
    make_stack: MakeStack,
) -> None:
    stack, who = await stack_and_person(make_stack)
    moment = hours_ago(5)
    async with stack.owner.begin() as connection:  # the same moment, category and quality
        for n in range(6):
            found = await make_item(stack, title=f"same {n}", published_at=moment)
            await connection.execute(
                text(
                    "UPDATE items SET summary = 's', category = 'news', quality = 3, "
                    "enriched_at = now(), enrichment_version = 'v1' WHERE id = :i"
                ),
                {"i": found},
            )
    seen: list[str] = []
    cursor: str | None = None
    while True:
        page = await feed(who, limit=1, **({"cursor": cursor} if cursor else {}))
        seen += titles(page)
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert sorted(seen) == [f"same {n}" for n in range(6)]


@pytest.mark.anyio
async def test_the_counting_function_answers_for_the_organization_it_is_called_in(
    make_stack: MakeStack, app_engine: AsyncEngine
) -> None:
    stack, admin = await stack_and_person(make_stack, "admin")
    item = await card(stack)
    await many_people(stack, item, 5)
    await yesterday(stack)

    async def counts_in(org: uuid.UUID) -> int:
        async with app_engine.begin() as connection:
            await connection.execute(
                text("SELECT set_config('app.org_id', :o, true)"), {"o": str(org)}
            )
            rows = await connection.execute(text("SELECT * FROM pharos_interaction_counts(30)"))
            return len(rows.all())

    assert await counts_in(stack.org_a) >= 1  # control
    assert await counts_in(stack.org_b) == 0
    del admin


@pytest.mark.anyio
async def test_one_person_cannot_page_through_the_feed_for_ever(
    make_stack: MakeStack, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(card_service, "CARDS_PER_HOUR", 2)
    stack, who = await stack_and_person(make_stack)
    await card(stack)
    assert (await who.client.get("/api/cards")).status_code == 200
    assert (await who.client.get("/api/cards")).status_code == 200
    refused = await who.client.get("/api/cards")
    assert refused.status_code == 429 and int(refused.headers["Retry-After"]) > 0


@pytest.mark.anyio
async def test_an_interaction_needs_an_article_that_is_a_card(make_stack: MakeStack) -> None:
    stack, who = await stack_and_person(make_stack)
    unread = await make_item(stack, title="unread")  # the model has not read it: no card
    assert (await put(who, unread, "save")).status_code == 404
