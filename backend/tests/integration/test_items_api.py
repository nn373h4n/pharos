"""The items API against a real database: who reads what, review, tenancy, audit, races (M3-10b)."""

# ruff: noqa: F811  (pytest fixtures imported from another test module are used by name)

import asyncio
import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from pharos.ingest import pipeline
from pharos.ingest import repository as ingest_repository
from pharos.ingest.feed import RawEntry

from .test_local_auth import (  # noqa: F401  (fixtures and helpers used by these tests)
    PUBLIC_URL,
    MakeStack,
    Stack,
    audit_rows,
    make_stack,
)
from .test_sources_api import (  # noqa: F401  (the cleaning fixture applies to this module too)
    FEED,
    _remove_what_the_test_made,
    details_of,
    foreign_source,
    post,
    rows,
)
from .test_user_admin import Person, person

pytestmark = pytest.mark.integration

NOW = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)


async def make_item(
    stack: Stack,
    *,
    org: uuid.UUID | None = None,
    status: str = "new",
    hold_reason: str | None = None,
    owner: uuid.UUID | None = None,
    title: str = "A story",
    published_at: datetime | None = NOW,
    sources: tuple[uuid.UUID, ...] = (),
) -> uuid.UUID:
    org = org or stack.org_a
    unique = uuid.uuid4().hex
    url = f"https://news.example.org/{unique}"
    async with stack.owner.begin() as connection:
        item = (
            await connection.execute(
                text(
                    "INSERT INTO items (org_id, owner_user_id, canonical_url, url_hash, title, "
                    "excerpt, content_sha256, status, hold_reason, published_at) VALUES "
                    "(:org, :owner, :url, :hash, :title, 'An excerpt', :content, :status, :reason, "
                    ":published) RETURNING id"
                ),
                {
                    "org": org,
                    "owner": owner,
                    "url": url,
                    "hash": hashlib.sha256(url.encode()).hexdigest(),
                    "title": title,
                    "content": unique + unique,
                    "status": status,
                    "reason": hold_reason,
                    "published": published_at,
                },
            )
        ).scalar_one()
        for source in sources:
            await connection.execute(
                text("INSERT INTO item_sources (org_id, item_id, source_id) VALUES (:o, :i, :s)"),
                {"o": org, "i": item, "s": source},
            )
    return item


async def act(who: Person, item: uuid.UUID, action: str) -> httpx.Response:
    return await who.client.post(f"/api/items/{item}/{action}", headers=who.headers)


async def status_of(stack: Stack, item: uuid.UUID) -> tuple[str, str | None]:
    [row] = await rows(stack, "SELECT status, hold_reason FROM items WHERE id = :i", i=item)
    return row.status, row.hold_reason


def item_events(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [e for e in entries if e["action"].startswith("item.")]


async def anonymous(stack: Stack) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=stack.app, client=("10.1.2.3", 4000)),
        base_url=PUBLIC_URL,
        headers={"Origin": PUBLIC_URL},
    )


# --- who may call -----------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_everyone_signed_in_reads_but_only_administrators_and_editors_review(
    make_stack: MakeStack,
) -> None:
    stack: Stack = await make_stack("on")
    admin = await person(stack, "admin")
    editor = await person(stack, "editor")
    member = await person(stack, "member")
    nobody = await anonymous(stack)
    try:
        item = await make_item(stack)
        for path in ("/api/items", f"/api/items/{item}"):
            assert (await nobody.get(path)).status_code == 401
            for who in (admin, editor, member):
                assert (await who.client.get(path)).status_code == 200, (who.username, path)
        for action in ("hold", "approve", "hide", "restore"):
            path = f"/api/items/{item}/{action}"
            assert (await nobody.post(path)).status_code == 401
            denied = await act(member, item, action)
            assert denied.status_code == 403 and denied.json()["code"] == "forbidden"
        assert await status_of(stack, item) == ("new", None)
        assert item_events(await audit_rows(stack)) == []
        assert (await act(editor, item, "hide")).status_code == 200
        assert (await act(admin, item, "restore")).status_code == 200
    finally:
        await nobody.aclose()


@pytest.mark.anyio
async def test_a_review_needs_the_csrf_token(make_stack: MakeStack) -> None:
    stack: Stack = await make_stack("on")
    editor = await person(stack, "editor")
    item = await make_item(stack)
    response = await editor.client.post(f"/api/items/{item}/hide")
    assert response.status_code == 403 and response.json()["code"] == "csrf_failed"
    assert await status_of(stack, item) == ("new", None)


@pytest.mark.anyio
async def test_a_demoted_editor_is_refused_on_the_next_request(make_stack: MakeStack) -> None:
    stack: Stack = await make_stack("on")
    editor = await person(stack, "editor")
    item = await make_item(stack)
    async with stack.owner.begin() as connection:
        await connection.execute(
            text("UPDATE users SET role = 'member' WHERE id = :i"), {"i": editor.id}
        )
    assert (await act(editor, item, "hide")).status_code == 403
    assert await status_of(stack, item) == ("new", None)


# --- what a member sees ---------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_a_member_sees_only_new_articles_and_is_not_told_about_the_others(
    make_stack: MakeStack,
) -> None:
    stack: Stack = await make_stack("on")
    member = await person(stack, "member")
    visible = await make_item(stack, title="Visible")
    held = await make_item(stack, status="held", hold_reason="link_ip_host", title="Held")
    hidden = await make_item(stack, status="hidden", title="Hidden")
    listed = (await member.client.get("/api/items")).json()
    assert [i["id"] for i in listed["items"]] == [str(visible)] and listed["total"] == 1
    assert (await member.client.get("/api/items", params={"status": "new"})).json()["total"] == 1
    for status in ("held", "hidden"):
        denied = await member.client.get("/api/items", params={"status": status})
        assert denied.status_code == 403
    by_source = await member.client.get("/api/items", params={"source_id": str(uuid.uuid4())})
    assert by_source.status_code == 403  # which source said what is for reviewers
    shown = (await member.client.get(f"/api/items/{visible}")).json()
    assert (shown["sources"], shown["updated_at"], shown["hold_reason"]) == ([], None, None)
    assert (await member.client.get(f"/api/items/{visible}")).status_code == 200
    for other in (held, hidden, uuid.uuid4()):  # the same answer as for an id that does not exist
        gone = await member.client.get(f"/api/items/{other}")
        assert gone.status_code == 404 and gone.json()["code"] == "not_found"


@pytest.mark.anyio
async def test_a_member_is_not_shown_where_an_article_came_from(make_stack: MakeStack) -> None:
    stack: Stack = await make_stack("on")
    admin = await person(stack, "admin")
    member = await person(stack, "member")
    source = uuid.UUID((await post(admin, FEED)).json()["id"])
    item = await make_item(stack, sources=(source,))
    assert (await member.client.get(f"/api/items/{item}")).json()["sources"] == []
    on_the_list = (await member.client.get("/api/items")).json()["items"]
    assert [(i["id"], i["sources"], i["updated_at"]) for i in on_the_list] == [
        (str(item), [], None)
    ]
    shown = (await admin.client.get(f"/api/items/{item}")).json()["sources"]
    assert shown == [{"id": str(source), "name": FEED["name"]}]


# --- what an editor sees ----------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_an_editor_lists_every_status_newest_first_and_filters_and_pages(
    make_stack: MakeStack,
) -> None:
    stack: Stack = await make_stack("on")
    admin = await person(stack, "admin")
    editor = await person(stack, "editor")
    first = uuid.UUID((await post(admin, FEED)).json()["id"])
    second = uuid.UUID(
        (
            await post(
                admin, {**FEED, "name": "Other", "config": {"url": "https://o.example.org/f"}}
            )
        ).json()["id"]
    )
    old = await make_item(
        stack, title="old", published_at=NOW - timedelta(days=3), sources=(first,)
    )
    mid = await make_item(
        stack,
        title="mid",
        status="held",
        hold_reason="link_odd_port",
        published_at=NOW - timedelta(days=2),
        sources=(first, second),
    )
    new = await make_item(
        stack, title="new", status="hidden", published_at=NOW - timedelta(days=1), sources=(second,)
    )
    undated = await make_item(stack, title="undated", published_at=None)
    body = (await editor.client.get("/api/items")).json()
    assert body["total"] == 4
    assert [i["title"] for i in body["items"]][1:] == ["new", "mid", "old"]  # newest first
    assert body["items"][0]["id"] == str(undated)  # no date: the time it was fetched, which is now
    assert set(body["items"][1]) == {
        "id", "url", "title", "excerpt", "lang", "published_at", "fetched_at", "status",
        "hold_reason", "updated_at", "sources",
    }  # fmt: skip
    for status, expected in (("new", {old, undated}), ("held", {mid}), ("hidden", {new})):
        found = (await editor.client.get("/api/items", params={"status": status})).json()
        assert {uuid.UUID(i["id"]) for i in found["items"]} == expected and found["total"] == len(
            expected
        )
    by_source = (await editor.client.get("/api/items", params={"source_id": str(second)})).json()
    assert {uuid.UUID(i["id"]) for i in by_source["items"]} == {mid, new}
    both = {i["id"]: i for i in (await editor.client.get("/api/items")).json()["items"]}
    assert [s["name"] for s in both[str(mid)]["sources"]] == ["Industry news", "Other"]
    assert both[str(mid)]["hold_reason"] == "link_odd_port"
    page = (await editor.client.get("/api/items", params={"limit": 2, "offset": 1})).json()
    assert page["total"] == 4 and len(page["items"]) == 2
    nothing = await editor.client.get("/api/items", params={"source_id": str(uuid.uuid4())})
    assert nothing.json() == {"items": [], "total": 0}
    for bad in (
        {"limit": 0},
        {"limit": 101},
        {"offset": -1},
        {"offset": 100_001},
        {"status": "approved"},
        {"source_id": "x"},
    ):
        assert (await editor.client.get("/api/items", params=bad)).status_code == 422


@pytest.mark.anyio
async def test_nothing_internal_is_in_an_answer(make_stack: MakeStack) -> None:
    stack: Stack = await make_stack("on")
    editor = await person(stack, "editor")
    item = await make_item(stack)
    shown = json.dumps((await editor.client.get(f"/api/items/{item}")).json())
    for secret in ("url_hash", "content_sha256", "owner_user_id", "org_id"):
        assert secret not in shown


# --- tenancy and personal feeds -----------------------------------------------------------------------------


@pytest.mark.anyio
async def test_administrators_never_reach_another_organizations_items(
    make_stack: MakeStack,
) -> None:
    stack: Stack = await make_stack("on")
    admin = await person(stack, "admin")
    foreign = await make_item(stack, org=stack.org_b, status="held", hold_reason="link_ip_host")
    theirs = await foreign_source(stack)
    assert (await admin.client.get("/api/items")).json() == {"items": [], "total": 0}
    assert (await admin.client.get("/api/items", params={"source_id": str(theirs)})).json()[
        "total"
    ] == 0
    assert (await admin.client.get(f"/api/items/{foreign}")).status_code == 404
    for action in ("hold", "approve", "hide", "restore"):
        response = await act(admin, foreign, action)
        assert response.status_code == 404 and response.json()["code"] == "not_found"
    assert await status_of(stack, foreign) == ("held", "link_ip_host")
    assert item_events(await audit_rows(stack)) == []


@pytest.mark.anyio
async def test_a_persons_own_articles_are_not_part_of_the_organizations_list_or_review(
    make_stack: MakeStack,
) -> None:
    stack: Stack = await make_stack("on")
    admin = await person(stack, "admin")
    owner = await person(stack, "editor")
    personal = await make_item(stack, owner=owner.id, title="Private reading")
    shared = await make_item(stack, title="Shared")
    for who in (admin, owner):
        listed = (await who.client.get("/api/items")).json()
        assert [i["id"] for i in listed["items"]] == [str(shared)]
        assert (await who.client.get(f"/api/items/{personal}")).status_code == 404
        for action in ("hold", "approve", "hide", "restore"):
            assert (await act(who, personal, action)).status_code == 404
    assert await status_of(stack, personal) == ("new", None)


# --- review: the state machine ------------------------------------------------------------------------------------

# (state it starts in, action) -> (answer, state afterwards)
CASES = [
    (("new", None), "hold", (200, ("held", "editor_hold"))),
    (("new", None), "approve", (200, ("new", None))),
    (("new", None), "hide", (200, ("hidden", None))),
    (("new", None), "restore", (200, ("new", None))),
    (("held", "link_ip_host"), "hold", (200, ("held", "link_ip_host"))),
    (("held", "link_ip_host"), "approve", (200, ("new", None))),
    (("held", "link_ip_host"), "hide", (200, ("hidden", None))),
    (("held", "link_ip_host"), "restore", (409, ("held", "link_ip_host"))),
    (("hidden", None), "hold", (409, ("hidden", None))),
    (("hidden", None), "approve", (409, ("hidden", None))),
    (("hidden", None), "hide", (200, ("hidden", None))),
    (("hidden", None), "restore", (200, ("new", None))),
]


@pytest.mark.anyio
@pytest.mark.parametrize(("start", "action", "expected"), CASES)
async def test_every_step_of_the_review_does_what_it_says_and_only_that(
    make_stack: MakeStack,
    start: tuple[str, str | None],
    action: str,
    expected: tuple[int, tuple[str, str | None]],
) -> None:
    stack: Stack = await make_stack("on")
    editor = await person(stack, "editor")
    item = await make_item(stack, status=start[0], hold_reason=start[1])
    response = await act(editor, item, action)
    code, after = expected
    assert response.status_code == code, response.text
    assert await status_of(stack, item) == after
    if code == 409:
        assert response.json()["code"] == "item_state"
    else:
        assert (response.json()["status"], response.json()["hold_reason"]) == after
    events = item_events(await audit_rows(stack))
    if code == 200 and start != after:
        [event] = events
        assert (
            event["action"]
            == {
                "hold": "item.held",
                "approve": "item.approved",
                "hide": "item.hidden",
                "restore": "item.restored",
            }[action]
        )
        assert (event["target"], event["actor_id"], event["outcome"]) == (
            f"item:{item}",
            editor.id,
            "success",
        )
        assert details_of(event) == {"from": start[0], "was": start[1]}
        assert "A story" not in json.dumps(event, default=str)  # no article text in the log
    else:
        assert events == []  # a step that changes nothing, or does not exist, is not recorded


@pytest.mark.anyio
async def test_the_whole_life_of_an_article_is_in_the_log_in_order(make_stack: MakeStack) -> None:
    stack: Stack = await make_stack("on")
    editor, admin = await person(stack, "editor"), await person(stack, "admin")
    item = await make_item(stack, status="held", hold_reason="instruction_phrase")
    for who, action in ((editor, "approve"), (admin, "hold"), (editor, "hide"), (admin, "restore")):
        assert (await act(who, item, action)).status_code == 200
    events = item_events(await audit_rows(stack))
    assert [(e["action"], e["actor_id"]) for e in events] == [
        ("item.approved", editor.id), ("item.held", admin.id),
        ("item.hidden", editor.id), ("item.restored", admin.id),
    ]  # fmt: skip
    assert [details_of(e)["from"] for e in events] == ["held", "new", "held", "hidden"]
    assert details_of(events[0])["was"] == "instruction_phrase"
    assert await status_of(stack, item) == ("new", None)


# --- races --------------------------------------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_a_review_waits_for_a_concurrent_decision_and_builds_on_it(
    make_stack: MakeStack,
) -> None:
    """The test holds the row; the request must queue behind it and then see what was committed.
    Without the row lock it would act on the state it saw before and record a change that never
    happened."""
    stack: Stack = await make_stack("on")
    editor = await person(stack, "editor")
    item = await make_item(stack)
    async with stack.owner.connect() as connection:
        transaction = await connection.begin()
        await connection.execute(
            text("UPDATE items SET status = 'hidden' WHERE id = :i"), {"i": item}
        )
        pending = asyncio.create_task(act(editor, item, "hide"))
        await asyncio.sleep(0.5)
        assert not pending.done()  # waiting for the row
        await transaction.commit()
        response = await pending
    assert response.status_code == 200 and response.json()["status"] == "hidden"
    assert item_events(await audit_rows(stack)) == []  # it was hidden already: nothing to record


@pytest.mark.anyio
async def test_two_editors_hiding_one_article_at_once_leave_one_record(
    make_stack: MakeStack,
) -> None:
    stack: Stack = await make_stack("on")
    first, second = await person(stack, "editor"), await person(stack, "editor")
    item = await make_item(stack)
    answers = await asyncio.gather(act(first, item, "hide"), act(second, item, "hide"))
    assert [a.status_code for a in answers] == [200, 200]
    assert (
        len([e for e in item_events(await audit_rows(stack)) if e["action"] == "item.hidden"]) == 1
    )
    assert await status_of(stack, item) == ("hidden", None)


@pytest.mark.anyio
async def test_a_hold_and_a_hide_at_once_end_hidden_whatever_the_order(
    make_stack: MakeStack,
) -> None:
    stack: Stack = await make_stack("on")
    first, second = await person(stack, "editor"), await person(stack, "editor")
    item = await make_item(stack)
    answers = await asyncio.gather(act(first, item, "hold"), act(second, item, "hide"))
    # hold then hide: both apply; hide then hold: the hold finds a hidden article and is refused
    assert sorted(a.status_code for a in answers) in ([200, 200], [200, 409])
    assert await status_of(stack, item) == ("hidden", None)
    events = item_events(await audit_rows(stack))
    assert len([e for e in events if e["action"] == "item.hidden"]) == 1
    assert len(events) == (2 if [a.status_code for a in answers] == [200, 200] else 1)


# --- what a fetch cannot undo --------------------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_an_unknown_or_malformed_id_is_a_clean_error(make_stack: MakeStack) -> None:
    stack: Stack = await make_stack("on")
    editor = await person(stack, "editor")
    assert (await editor.client.get(f"/api/items/{uuid.uuid4()}")).status_code == 404
    assert (await editor.client.get("/api/items/not-a-uuid")).status_code == 422
    assert (await act(editor, uuid.uuid4(), "hide")).status_code == 404
    assert (
        await editor.client.post("/api/items/not-a-uuid/hide", headers=editor.headers)
    ).status_code == 422
    assert (
        await editor.client.post(f"/api/items/{uuid.uuid4()}/delete", headers=editor.headers)
    ).status_code == 404


@pytest.mark.anyio
async def test_an_article_stays_listed_when_its_source_is_deleted(make_stack: MakeStack) -> None:
    stack: Stack = await make_stack("on")
    admin = await person(stack, "admin")
    source = uuid.UUID((await post(admin, FEED)).json()["id"])
    item = await make_item(stack, sources=(source,))
    assert (
        await admin.client.delete(f"/api/sources/{source}", headers=admin.headers)
    ).status_code == 204
    shown = (await admin.client.get(f"/api/items/{item}")).json()
    assert shown["sources"] == [] and shown["status"] == "new"


@pytest.mark.anyio
async def test_reviewers_see_when_an_article_was_last_changed(make_stack: MakeStack) -> None:
    stack: Stack = await make_stack("on")
    editor = await person(stack, "editor")
    item = await make_item(stack)
    shown = (await editor.client.get(f"/api/items/{item}")).json()
    assert shown["updated_at"] is not None
    listed = (await editor.client.get("/api/items")).json()["items"]
    assert listed[0]["updated_at"] is not None


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("status", "reason"), [("new", None), ("held", "editor_hold"), ("hidden", None)]
)
async def test_a_later_fetch_may_change_the_text_but_never_a_decision(
    make_stack: MakeStack, worker_engine: AsyncEngine, status: str, reason: str | None
) -> None:
    """The worker writes through the pipeline, as the role it really has: it refreshes the text of
    an article it knows and cannot touch `status` or `hold_reason`."""
    stack: Stack = await make_stack("on")
    admin = await person(stack, "admin")
    source = uuid.UUID((await post(admin, FEED)).json()["id"])
    item = await make_item(stack, status=status, hold_reason=reason, sources=(source,))
    [row] = await rows(stack, "SELECT canonical_url FROM items WHERE id = :i", i=item)
    entry = RawEntry("g1", "Changed by the feed", "New excerpt", row.canonical_url, None, "c" * 64)
    async with worker_engine.begin() as connection:
        await ingest_repository.enter_org(connection, stack.org_a)
        await pipeline.store_items(connection, stack.org_a, source, None, [entry])
    [after] = await rows(
        stack, "SELECT title, status, hold_reason FROM items WHERE id = :i", i=item
    )
    assert after.title == "Changed by the feed"  # positive control: the fetch did write
    assert (after.status, after.hold_reason) == (status, reason)
