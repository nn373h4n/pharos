"""A person's own feeds against a real database: privacy, limits, validation, audit (M3-13)."""

# ruff: noqa: F811  (fixtures are imported by name)

import asyncio
import hashlib
import json
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from .conftest import Orgs
from .test_local_auth import (  # noqa: F401  (fixtures and helpers used by these tests)
    PUBLIC_URL,
    MakeStack,
    Stack,
    audit_rows,
    make_stack,
)
from .test_user_admin import Person, person

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
async def _remove_what_the_test_made(owner_engine: AsyncEngine, orgs: Orgs) -> AsyncIterator[None]:
    """The organizations are deleted when the test ends; what the tests made must go first."""
    yield
    async with owner_engine.begin() as connection:
        for table in ("item_sources", "items", "raw_items", "fetch_runs", "sources"):
            await connection.execute(
                text(f"DELETE FROM {table} WHERE org_id = ANY(:orgs)"),
                {"orgs": list(orgs)},
            )


FEED = {"name": "My reading", "url": "https://blog.example.org/feed.xml"}
INWARDS = [
    "http://127.1/",
    "http://[::ffff:7f00:1]/",
    "http://2130706433/",
    "http://0x7f.1/",
    "http://localhost./",
    "http://169.254.169.254/latest",
    "https://intranet/feed",
]


async def anonymous(stack: Stack) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=stack.app, client=("10.1.2.3", 4000)),
        base_url=PUBLIC_URL,
        headers={"Origin": PUBLIC_URL},
    )


async def post(who: Person, body: dict[str, Any]) -> httpx.Response:
    return await who.client.post("/api/me/feeds", json=body, headers=who.headers)


async def patch(who: Person, feed: str | uuid.UUID, body: dict[str, Any]) -> httpx.Response:
    return await who.client.patch(f"/api/me/feeds/{feed}", json=body, headers=who.headers)


async def rows(stack: Stack, sql: str, **params: Any) -> list[Any]:
    async with stack.owner.connect() as connection:
        return list((await connection.execute(text(sql), params)).all())


def details_of(entry: dict[str, Any]) -> dict[str, Any]:
    raw = entry["details"]
    return raw if isinstance(raw, dict) else json.loads(raw)


def feed_url(n: int) -> dict[str, Any]:
    return {"name": f"Feed {n}", "url": f"https://host{n}.example.org/feed.xml"}


async def make_item(
    stack: Stack,
    *,
    owner: uuid.UUID | None,
    feeds: list[uuid.UUID],
    title: str = "An article",
    status: str = "new",
    reason: str | None = None,
    org: uuid.UUID | None = None,
) -> uuid.UUID:
    unique = uuid.uuid4().hex
    url = f"https://news.example.org/{unique}"
    org = org or stack.org_a
    async with stack.owner.begin() as connection:
        item = (
            await connection.execute(
                text(
                    "INSERT INTO items (org_id, owner_user_id, canonical_url, url_hash, title, "
                    "excerpt, content_sha256, status, hold_reason) VALUES (:org, :owner, :url, "
                    ":hash, :title, 'An excerpt', :content, :status, :reason) RETURNING id"
                ),
                {
                    "org": org,
                    "owner": owner,
                    "url": url,
                    "hash": hashlib.sha256(url.encode()).hexdigest(),
                    "title": title,
                    "content": unique + unique,
                    "status": status,
                    "reason": reason,
                },
            )
        ).scalar_one()
        for feed in feeds:
            await connection.execute(
                text("INSERT INTO item_sources (org_id, item_id, source_id) VALUES (:o, :i, :s)"),
                {"o": org, "i": item, "s": feed},
            )
    return item


# --- who may call -----------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_every_signed_in_person_has_feeds_of_their_own_and_nobody_else_does(
    make_stack: MakeStack,
) -> None:
    stack: Stack = await make_stack("on")
    nobody = await anonymous(stack)
    try:
        for role in ("admin", "editor", "member"):
            who = await person(stack, role)
            created = await post(who, feed_url(1))
            assert created.status_code == 201, (role, created.text)
            feed = created.json()["id"]
            for path in (
                "/api/me/feeds",
                f"/api/me/feeds/{feed}",
                f"/api/me/feeds/{feed}/runs",
                f"/api/me/feeds/{feed}/items",
            ):
                assert (await who.client.get(path)).status_code == 200, (role, path)
                assert (await nobody.get(path)).status_code == 401, path
        assert (await nobody.post("/api/me/feeds", json=FEED)).status_code == 401
    finally:
        await nobody.aclose()


@pytest.mark.anyio
async def test_a_change_needs_the_csrf_token(make_stack: MakeStack) -> None:
    stack: Stack = await make_stack("on")
    who = await person(stack, "member")
    feed = (await post(who, FEED)).json()["id"]
    for method, path, body in (
        ("POST", "/api/me/feeds", FEED),
        ("PATCH", f"/api/me/feeds/{feed}", {"name": "x"}),
        ("DELETE", f"/api/me/feeds/{feed}", None),
    ):
        response = await who.client.request(method, path, json=body)
        assert response.status_code == 403 and response.json()["code"] == "csrf_failed", path
    assert len(await rows(stack, "SELECT 1 FROM sources")) == 1


# --- privacy ------------------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_a_person_never_reaches_another_persons_feeds_or_another_organizations(
    make_stack: MakeStack,
) -> None:
    stack: Stack = await make_stack("on")
    alice = await person(stack, "member")
    bob = await person(stack, "member")
    admin = await person(stack, "admin")
    mine = (await post(alice, FEED)).json()["id"]
    item = await make_item(stack, owner=alice.id, feeds=[uuid.UUID(mine)], title="Alice only")
    foreign = uuid.uuid4()
    async with stack.owner.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO sources (id, org_id, owner_user_id, type, name, config, dedupe_key) "
                "VALUES (:id, :org, NULL, 'rss', 'Not yours', CAST('{}' AS jsonb), :key)"
            ),
            {"id": foreign, "org": stack.org_b, "key": uuid.uuid4().hex},
        )

    for other in (bob, admin):
        assert (await other.client.get("/api/me/feeds")).json() == {"items": [], "total": 0}
        for response in (
            await other.client.get(f"/api/me/feeds/{mine}"),
            await other.client.get(f"/api/me/feeds/{mine}/runs"),
            await other.client.get(f"/api/me/feeds/{mine}/items"),
            await patch(other, mine, {"name": "Mine now", "enabled": False}),
            await other.client.delete(f"/api/me/feeds/{mine}", headers=other.headers),
        ):
            assert response.status_code == 404  # the same answer as for an id that does not exist
            assert response.json()["code"] == "not_found"
    # an organization source and another organization's are not "my feed" either
    for response in (
        await alice.client.get(f"/api/me/feeds/{foreign}"),
        await patch(alice, foreign, {"name": "x"}),
        await alice.client.delete(f"/api/me/feeds/{foreign}", headers=alice.headers),
    ):
        assert response.status_code == 404
    after = await rows(stack, "SELECT name, enabled FROM sources WHERE id = :i", i=uuid.UUID(mine))
    assert [(r.name, r.enabled) for r in after] == [(FEED["name"], True)]
    assert len(await rows(stack, "SELECT 1 FROM sources WHERE id = :i", i=foreign)) == 1
    # the administrator's list of the organization's sources does not show it, and nothing leaks
    # into the shared articles either
    assert (await admin.client.get("/api/sources")).json() == {"items": [], "total": 0}
    shared = (await admin.client.get("/api/items?status=new")).json()
    assert [i["id"] for i in shared["items"]] == [] and shared["total"] == 0
    assert (await admin.client.get(f"/api/items/{item}")).status_code == 404
    # the owner still has everything
    assert (await alice.client.get("/api/me/feeds")).json()["total"] == 1


@pytest.mark.anyio
async def test_the_same_address_for_two_people_or_an_organization_is_not_a_conflict(
    make_stack: MakeStack,
) -> None:
    stack: Stack = await make_stack("on")
    alice = await person(stack, "member")
    bob = await person(stack, "member")
    admin = await person(stack, "admin")
    shared = await admin.client.post(
        "/api/sources",
        json={"type": "rss", "name": "Shared", "config": {"url": FEED["url"]}},
        headers=admin.headers,
    )
    assert shared.status_code == 201
    assert (await post(alice, FEED)).status_code == 201
    assert (await post(bob, FEED)).status_code == 201
    assert (await post(alice, {**FEED, "name": "Again"})).status_code == 409


# --- creating ----------------------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_a_new_feed_belongs_to_the_person_and_is_audited_without_its_address(
    make_stack: MakeStack,
) -> None:
    stack: Stack = await make_stack("on")
    who = await person(stack, "member")
    response = await post(who, FEED)
    assert response.status_code == 201
    body = response.json()
    assert set(body) == {
        "id",
        "name",
        "url",
        "enabled",
        "interval_seconds",
        "next_fetch_at",
        "last_fetched_at",
        "last_success_at",
        "consecutive_failures",
        "last_error_code",
        "created_at",
        "updated_at",
    }
    assert (body["name"], body["url"], body["enabled"], body["interval_seconds"]) == (
        "My reading",
        "https://blog.example.org/feed.xml",
        True,
        3600,
    )
    [row] = await rows(
        stack,
        "SELECT owner_user_id, created_by, type, org_id FROM sources WHERE id = :i",
        i=uuid.UUID(body["id"]),
    )
    assert (row.owner_user_id, row.created_by, row.type, row.org_id) == (
        who.id,
        who.id,
        "rss",
        stack.org_a,
    )
    entries = [e for e in await audit_rows(stack) if e["action"] == "source.created"]
    assert len(entries) == 1
    entry = entries[0]
    assert (entry["actor_id"], entry["outcome"], entry["target"]) == (
        who.id,
        "success",
        f"source:{body['id']}",
    )
    details = details_of(entry)
    assert details["scope"] == "personal" and details["type"] == "rss"
    assert "blog.example.org" not in json.dumps(details) and FEED["name"] not in json.dumps(details)


@pytest.mark.anyio
async def test_a_request_that_is_not_valid_is_refused_and_stores_nothing(
    make_stack: MakeStack,
) -> None:
    stack: Stack = await make_stack("on")
    who = await person(stack, "member")
    bad: list[dict[str, Any]] = [
        {},
        {"name": "", "url": FEED["url"]},
        {"name": "x" * 201, "url": FEED["url"]},
        {"name": "<b>bold</b>", "url": FEED["url"]},
        {"name": "zero​width", "url": FEED["url"]},
        {"name": "n", "url": ""},
        {"name": "n", "url": "ftp://example.org/feed"},
        {"name": "n", "url": "https://user:pass@example.org/feed"},
        {"name": "n", "url": "https://example.org/" + "a" * 2100},
        {**FEED, "interval_seconds": 3599},  # more often than hourly
        {**FEED, "interval_seconds": 60},
        {**FEED, "interval_seconds": 10**9},
        {**FEED, "type": "github"},  # only RSS and Atom
        {**FEED, "owner_user_id": str(uuid.uuid4())},
        {**FEED, "created_by": str(uuid.uuid4())},
        {**FEED, "config": {"url": FEED["url"]}},
    ]
    for body in bad:
        response = await post(who, body)
        assert response.status_code == 422, body
    assert await rows(stack, "SELECT 1 FROM sources") == []


@pytest.mark.anyio
@pytest.mark.parametrize("url", INWARDS)
async def test_an_address_pointing_inwards_is_refused_and_the_attempt_is_audited(
    make_stack: MakeStack, url: str
) -> None:
    stack: Stack = await make_stack("on")
    who = await person(stack, "member")
    response = await post(who, {"name": "Inward", "url": url})
    assert response.status_code == 422
    assert response.json()["errors"] == [{"loc": ["body", "url"], "type": "url_not_allowed"}]
    assert await rows(stack, "SELECT 1 FROM sources") == []
    refused = [
        e
        for e in await audit_rows(stack)
        if e["action"] == "source.created" and e["outcome"] == "failure"
    ]
    assert len(refused) == 1
    assert refused[0]["actor_id"] == who.id
    details = details_of(refused[0])
    assert (details["reason"], details["scope"]) == ("url_not_allowed", "personal")
    assert url not in json.dumps(details) and "127" not in json.dumps(details)


@pytest.mark.anyio
async def test_the_address_of_this_installation_is_refused(make_stack: MakeStack) -> None:
    stack: Stack = await make_stack("on")
    who = await person(stack, "member")
    response = await post(who, {"name": "Self", "url": f"{PUBLIC_URL}/feed.xml"})
    assert response.status_code == 422
    assert response.json()["errors"][0]["type"] == "url_not_allowed"


@pytest.mark.anyio
async def test_a_person_has_at_most_five_feeds_and_the_sixth_is_refused_and_audited(
    make_stack: MakeStack,
) -> None:
    stack: Stack = await make_stack("on")
    who = await person(stack, "member")
    other = await person(stack, "member")
    for n in range(1, 6):
        assert (await post(who, feed_url(n))).status_code == 201
    sixth = await post(who, feed_url(6))
    assert sixth.status_code == 409 and sixth.json()["code"] == "source_limit"
    assert (await who.client.get("/api/me/feeds")).json()["total"] == 5
    refused = [
        details_of(e)
        for e in await audit_rows(stack)
        if e["outcome"] == "failure" and e["action"] == "source.created"
    ]
    assert [d["reason"] for d in refused] == ["limit"]
    # the limit is per person, and a deleted feed frees a place
    assert (await post(other, feed_url(1))).status_code == 201
    first = (await who.client.get("/api/me/feeds")).json()["items"][0]["id"]
    assert (
        await who.client.delete(f"/api/me/feeds/{first}", headers=who.headers)
    ).status_code == 204
    assert (await post(who, feed_url(6))).status_code == 201


@pytest.mark.anyio
async def test_a_switched_off_feed_counts_towards_the_limit(make_stack: MakeStack) -> None:
    stack: Stack = await make_stack("on")
    who = await person(stack, "member")
    for n in range(1, 6):
        assert (await post(who, {**feed_url(n), "enabled": False})).status_code == 201
    assert (await post(who, feed_url(6))).status_code == 409


@pytest.mark.anyio
async def test_parallel_creations_cannot_get_past_the_limit(make_stack: MakeStack) -> None:
    stack: Stack = await make_stack("on")
    who = await person(stack, "member")
    responses = await asyncio.gather(*(post(who, feed_url(n)) for n in range(1, 11)))
    assert sorted(r.status_code for r in responses).count(201) == 5
    assert {r.status_code for r in responses} <= {201, 409}
    assert len(await rows(stack, "SELECT 1 FROM sources WHERE owner_user_id = :u", u=who.id)) == 5


@pytest.mark.anyio
async def test_the_service_keeps_the_limit_by_itself_without_the_databases_trigger(
    make_stack: MakeStack,
) -> None:
    """The trigger is the last line; this proves the first one (the count and the lock) alone."""
    stack: Stack = await make_stack("on")
    who = await person(stack, "member")
    racer = await person(stack, "member")
    async with stack.owner.begin() as connection:
        await connection.execute(text("ALTER TABLE sources DISABLE TRIGGER sources_limit_personal"))
    try:
        for n in range(1, 6):
            assert (await post(who, feed_url(n))).status_code == 201
        sixth = await post(who, feed_url(6))
        assert sixth.status_code == 409 and sixth.json()["code"] == "source_limit"
        responses = await asyncio.gather(*(post(racer, feed_url(n)) for n in range(1, 11)))
        assert sorted(r.status_code for r in responses).count(201) == 5
        stored = await rows(stack, "SELECT 1 FROM sources WHERE owner_user_id = :u", u=racer.id)
        assert len(stored) == 5
    finally:
        async with stack.owner.begin() as connection:
            await connection.execute(
                text("ALTER TABLE sources ENABLE TRIGGER sources_limit_personal")
            )


@pytest.mark.anyio
async def test_a_person_cannot_bring_in_more_than_ten_new_addresses_an_hour(
    make_stack: MakeStack,
) -> None:
    """Without it, adding and deleting feeds would make the worker fetch addresses at will."""
    stack: Stack = await make_stack("on")
    who = await person(stack, "member")
    other = await person(stack, "member")
    for n in range(1, 11):
        created = await post(who, feed_url(n))
        assert created.status_code == 201, n
        assert (
            await who.client.delete(f"/api/me/feeds/{created.json()['id']}", headers=who.headers)
        ).status_code == 204
    refused = await post(who, feed_url(11))
    assert refused.status_code == 429 and refused.json()["code"] == "rate_limited"
    assert int(refused.headers["Retry-After"]) > 0
    # changing the address of a feed is a new address too
    assert (await post(other, feed_url(1))).status_code == 201
    kept = (await other.client.get("/api/me/feeds")).json()["items"][0]["id"]
    for n in range(2, 11):
        assert (await patch(other, kept, {"url": feed_url(n)["url"]})).status_code == 200, n
    assert (await patch(other, kept, {"url": feed_url(11)["url"]})).status_code == 429
    # a rename is not a new address, and neither is the same address again
    assert (await patch(other, kept, {"name": "Renamed"})).status_code == 200
    assert (await patch(other, kept, {"url": feed_url(10)["url"]})).status_code == 200
    refusals = [
        details_of(e)
        for e in await audit_rows(stack)
        if e["outcome"] == "failure" and details_of(e).get("reason") == "rate"
    ]
    assert len(refusals) == 2
    # the first person is still not able to start again, and nobody else is touched
    assert (await post(who, feed_url(12))).status_code == 429


@pytest.mark.anyio
async def test_changes_to_the_same_address_at_once_make_one_winner_and_conflicts_not_errors(
    make_stack: MakeStack,
) -> None:
    stack: Stack = await make_stack("on")
    who = await person(stack, "member")
    feeds = [(await post(who, feed_url(n))).json()["id"] for n in range(1, 6)]
    target = {"url": "https://same.example.org/feed.xml"}
    responses = await asyncio.gather(*(patch(who, feed, target) for feed in feeds))
    assert sorted(r.status_code for r in responses) == [200, 409, 409, 409, 409]


@pytest.mark.anyio
async def test_a_clash_the_database_catches_after_the_check_is_a_conflict_too(
    make_stack: MakeStack, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two changes can pass the check together; the unique index then decides, and says 409."""
    from pharos.feeds import service as feeds_service

    stack: Stack = await make_stack("on")
    who = await person(stack, "member")
    await post(who, feed_url(1))
    second = (await post(who, feed_url(2))).json()["id"]

    async def never_taken(*_: object, **__: object) -> bool:
        return False

    monkeypatch.setattr(feeds_service.repository, "key_taken", never_taken)
    clash = await patch(who, second, {"url": feed_url(1)["url"]})
    assert clash.status_code == 409 and clash.json()["code"] == "source_conflict"
    [row] = await rows(stack, "SELECT config FROM sources WHERE id = :i", i=uuid.UUID(second))
    assert row.config == {"url": feed_url(2)["url"]}


@pytest.mark.anyio
async def test_the_same_feed_twice_is_a_conflict_whatever_its_spelling(
    make_stack: MakeStack,
) -> None:
    stack: Stack = await make_stack("on")
    who = await person(stack, "member")
    assert (await post(who, FEED)).status_code == 201
    for spelling in (
        "https://BLOG.example.org/feed.xml",
        "https://blog.example.org:443/feed.xml",
        "https://blog.example.org/feed.xml#top",
    ):
        again = await post(who, {"name": "Copy", "url": spelling})
        assert again.status_code == 409, spelling
        assert again.json()["code"] == "source_conflict"
    assert len(await rows(stack, "SELECT 1 FROM sources")) == 1


# --- reading ---------------------------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_the_list_pages_in_the_order_the_feeds_were_added(make_stack: MakeStack) -> None:
    stack: Stack = await make_stack("on")
    who = await person(stack, "member")
    ids = [(await post(who, feed_url(n))).json()["id"] for n in range(1, 4)]
    page = (await who.client.get("/api/me/feeds?limit=2")).json()
    assert [f["id"] for f in page["items"]] == ids[:2] and page["total"] == 3
    rest = (await who.client.get("/api/me/feeds?limit=2&offset=2")).json()
    assert [f["id"] for f in rest["items"]] == ids[2:]
    for query in ("limit=0", "limit=51", "offset=-1", "offset=1001"):
        assert (await who.client.get(f"/api/me/feeds?{query}")).status_code == 422


@pytest.mark.anyio
async def test_an_unknown_or_malformed_id_is_a_clean_error(make_stack: MakeStack) -> None:
    stack: Stack = await make_stack("on")
    who = await person(stack, "member")
    assert (await who.client.get(f"/api/me/feeds/{uuid.uuid4()}")).status_code == 404
    assert (await who.client.get("/api/me/feeds/not-an-id")).status_code == 422
    assert (await who.client.get("/api/me/feeds/..%2f..")).status_code in (404, 422)


@pytest.mark.anyio
async def test_the_runs_and_the_articles_of_a_feed_belong_to_that_feed(
    make_stack: MakeStack,
) -> None:
    stack: Stack = await make_stack("on")
    who = await person(stack, "member")
    first = uuid.UUID((await post(who, feed_url(1))).json()["id"])
    second = uuid.UUID((await post(who, feed_url(2))).json()["id"])
    async with stack.owner.begin() as connection:
        for n in range(3):
            await connection.execute(
                text(
                    "INSERT INTO fetch_runs (org_id, source_id, started_at, finished_at, outcome, "
                    "items_seen, items_new) VALUES (:org, :src, now() - make_interval(mins => :n), "
                    "now(), 'ok', :n, 0)"
                ),
                {"org": stack.org_a, "src": first, "n": n},
            )
    runs = (await who.client.get(f"/api/me/feeds/{first}/runs")).json()["items"]
    assert [r["items_seen"] for r in runs] == [0, 1, 2]
    assert set(runs[0]) == {
        "id",
        "started_at",
        "finished_at",
        "outcome",
        "error_code",
        "http_status",
        "duration_ms",
        "bytes_fetched",
        "items_seen",
        "items_new",
    }
    assert (await who.client.get(f"/api/me/feeds/{second}/runs")).json()["items"] == []

    one = await make_item(stack, owner=who.id, feeds=[first], title="In the first")
    held = await make_item(
        stack, owner=who.id, feeds=[first], title="Flagged", status="held", reason="link_ip_host"
    )
    await make_item(stack, owner=who.id, feeds=[second], title="In the second")
    listed = (await who.client.get(f"/api/me/feeds/{first}/items")).json()
    assert listed["total"] == 2
    assert {i["id"] for i in listed["items"]} == {str(one), str(held)}
    flagged = next(i for i in listed["items"] if i["id"] == str(held))
    assert (flagged["status"], flagged["hold_reason"]) == ("held", "link_ip_host")
    assert set(listed["items"][0]) == {
        "id",
        "url",
        "title",
        "excerpt",
        "lang",
        "published_at",
        "fetched_at",
        "status",
        "hold_reason",
    }
    paged = (await who.client.get(f"/api/me/feeds/{first}/items?limit=1&offset=1")).json()
    assert len(paged["items"]) == 1 and paged["total"] == 2


@pytest.mark.anyio
async def test_nobody_can_review_a_personal_article(make_stack: MakeStack) -> None:
    stack: Stack = await make_stack("on")
    who = await person(stack, "admin")
    feed = uuid.UUID((await post(who, FEED)).json()["id"])
    item = await make_item(stack, owner=who.id, feeds=[feed], status="held", reason="link_ip_host")
    for action in ("hold", "approve", "hide", "restore"):
        denied = await who.client.post(f"/api/items/{item}/{action}", headers=who.headers)
        assert denied.status_code == 404, action
    [row] = await rows(stack, "SELECT status FROM items WHERE id = :i", i=item)
    assert row.status == "held"


# --- changing ------------------------------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_a_change_is_saved_and_audited_by_kind_without_values(
    make_stack: MakeStack,
) -> None:
    stack: Stack = await make_stack("on")
    who = await person(stack, "member")
    feed = (await post(who, FEED)).json()["id"]
    response = await patch(
        who,
        feed,
        {
            "name": "Renamed",
            "url": "https://other.example.org/feed.xml",
            "interval_seconds": 7200,
            "enabled": False,
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert (body["name"], body["url"], body["interval_seconds"], body["enabled"]) == (
        "Renamed",
        "https://other.example.org/feed.xml",
        7200,
        False,
    )
    entries = await audit_rows(stack)
    updated = next(e for e in entries if e["action"] == "source.updated")
    assert details_of(updated)["fields"] == "name,url,interval_seconds"
    assert any(e["action"] == "source.disabled" for e in entries)
    everything = json.dumps([details_of(e) for e in entries])
    assert "example.org" not in everything and "Renamed" not in everything
    # nothing to change, nothing to record
    count = len(entries)
    assert (await patch(who, feed, {"name": "Renamed"})).status_code == 200
    assert len(await audit_rows(stack)) == count


@pytest.mark.anyio
async def test_a_change_that_is_not_allowed_is_refused_and_nothing_moves(
    make_stack: MakeStack,
) -> None:
    stack: Stack = await make_stack("on")
    who = await person(stack, "member")
    feed = (await post(who, FEED)).json()["id"]
    for body in (
        {"interval_seconds": 3599},
        {"name": ""},
        {"type": "github"},
        {"owner_user_id": str(uuid.uuid4())},
        {"dedupe_key": "x"},
        {"next_fetch_at": "2020-01-01T00:00:00Z"},
        {"url": "ftp://example.org/x"},
    ):
        assert (await patch(who, feed, body)).status_code == 422, body
    [row] = await rows(
        stack, "SELECT name, interval_seconds FROM sources WHERE id = :i", i=uuid.UUID(feed)
    )
    assert (row.name, row.interval_seconds) == (FEED["name"], 3600)


@pytest.mark.anyio
async def test_a_change_to_an_internal_address_is_refused_and_audited(
    make_stack: MakeStack,
) -> None:
    stack: Stack = await make_stack("on")
    who = await person(stack, "member")
    feed = (await post(who, FEED)).json()["id"]
    response = await patch(who, feed, {"url": "http://127.1/"})
    assert response.status_code == 422
    [row] = await rows(stack, "SELECT config FROM sources WHERE id = :i", i=uuid.UUID(feed))
    assert row.config == {"url": FEED["url"]}
    refused = [
        details_of(e)
        for e in await audit_rows(stack)
        if e["action"] == "source.updated" and e["outcome"] == "failure"
    ]
    assert [d["reason"] for d in refused] == ["url_not_allowed"]


@pytest.mark.anyio
async def test_a_change_cannot_take_the_address_of_another_feed_of_the_same_person(
    make_stack: MakeStack,
) -> None:
    stack: Stack = await make_stack("on")
    who = await person(stack, "member")
    await post(who, feed_url(1))
    second = (await post(who, feed_url(2))).json()["id"]
    clash = await patch(who, second, {"url": feed_url(1)["url"]})
    assert clash.status_code == 409 and clash.json()["code"] == "source_conflict"


@pytest.mark.anyio
async def test_switching_a_feed_on_checks_its_stored_address_again(make_stack: MakeStack) -> None:
    stack: Stack = await make_stack("on")
    who = await person(stack, "member")
    feed = (await post(who, {**FEED, "enabled": False})).json()["id"]
    # an address that was fine when stored and is not any more (the installation moved there)
    async with stack.owner.begin() as connection:
        await connection.execute(
            text("UPDATE sources SET config = CAST(:c AS jsonb) WHERE id = :i"),
            {"c": json.dumps({"url": f"{PUBLIC_URL}/feed.xml"}), "i": uuid.UUID(feed)},
        )
    response = await patch(who, feed, {"enabled": True})
    assert response.status_code == 422
    [row] = await rows(stack, "SELECT enabled FROM sources WHERE id = :i", i=uuid.UUID(feed))
    assert row.enabled is False


@pytest.mark.anyio
async def test_changing_a_feed_leaves_the_workers_state_alone(make_stack: MakeStack) -> None:
    stack: Stack = await make_stack("on")
    who = await person(stack, "member")
    feed = uuid.UUID((await post(who, FEED)).json()["id"])
    async with stack.owner.begin() as connection:
        await connection.execute(
            text(
                "UPDATE sources SET consecutive_failures = 3, last_error_code = 'timed_out', "
                "etag = 'abc' WHERE id = :i"
            ),
            {"i": feed},
        )
    assert (await patch(who, feed, {"name": "Renamed"})).status_code == 200
    [row] = await rows(
        stack,
        "SELECT consecutive_failures, last_error_code, etag FROM sources WHERE id = :i",
        i=feed,
    )
    assert (row.consecutive_failures, row.last_error_code, row.etag) == (3, "timed_out", "abc")


# --- deleting ----------------------------------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_deleting_removes_the_feed_and_its_history_and_hides_its_articles(
    make_stack: MakeStack,
) -> None:
    stack: Stack = await make_stack("on")
    who = await person(stack, "member")
    feed = uuid.UUID((await post(who, FEED)).json()["id"])
    item = await make_item(stack, owner=who.id, feeds=[feed])
    async with stack.owner.begin() as connection:
        await connection.execute(
            text("INSERT INTO fetch_runs (org_id, source_id, outcome) VALUES (:o, :s, 'ok')"),
            {"o": stack.org_a, "s": feed},
        )
    assert (
        await who.client.delete(f"/api/me/feeds/{feed}", headers=who.headers)
    ).status_code == 204
    assert await rows(stack, "SELECT 1 FROM sources WHERE id = :i", i=feed) == []
    assert await rows(stack, "SELECT 1 FROM fetch_runs WHERE source_id = :i", i=feed) == []
    # the article row stays until retention, but nobody can reach it
    assert len(await rows(stack, "SELECT 1 FROM items WHERE id = :i", i=item)) == 1
    assert (await who.client.get(f"/api/me/feeds/{feed}/items")).status_code == 404
    admin = await person(stack, "admin")
    assert (await admin.client.get(f"/api/items/{item}")).status_code == 404
    assert (
        await who.client.delete(f"/api/me/feeds/{feed}", headers=who.headers)
    ).status_code == 404
    deleted = [e for e in await audit_rows(stack) if e["action"] == "source.deleted"]
    assert len(deleted) == 1 and details_of(deleted[0])["scope"] == "personal"


# --- the operator's switch ----------------------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_when_the_operator_switches_personal_feeds_off_the_endpoints_do_not_exist(
    make_stack: MakeStack,
) -> None:
    stack: Stack = await make_stack("on", personal_feeds=False)
    who = await person(stack, "admin")
    feed = uuid.uuid4()
    for method, path, body in (
        ("GET", "/api/me/feeds", None),
        ("POST", "/api/me/feeds", FEED),
        ("GET", f"/api/me/feeds/{feed}", None),
        ("PATCH", f"/api/me/feeds/{feed}", {"name": "x"}),
        ("DELETE", f"/api/me/feeds/{feed}", None),
        ("GET", f"/api/me/feeds/{feed}/runs", None),
        ("GET", f"/api/me/feeds/{feed}/items", None),
    ):
        response = await who.client.request(method, path, json=body, headers=who.headers)
        assert response.status_code == 404, (method, path)
    assert await rows(stack, "SELECT 1 FROM sources") == []
    # not even a body that would be refused says that the path exists
    for method, path, body in (
        ("POST", "/api/me/feeds", {"nonsense": True}),
        ("PATCH", "/api/me/feeds/not-an-id", {"interval_seconds": 1}),
        ("GET", "/api/me/feeds/not-an-id", None),
        ("GET", "/api/me/feeds?limit=0", None),
    ):
        response = await who.client.request(method, path, json=body, headers=who.headers)
        assert response.status_code == 404, (method, path)
