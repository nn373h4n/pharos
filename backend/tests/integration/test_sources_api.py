"""The sources API against a real database: roles, tenancy, validation, audit, races (M3-10a)."""

# ruff: noqa: F811, RUF001  (fixtures are imported by name; look-alike dots are the point of a test)

import asyncio
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
    """The organizations are deleted when the test ends; their sources must go first."""
    yield
    async with owner_engine.begin() as connection:
        for table in ("item_sources", "items", "raw_items", "fetch_runs", "sources"):
            await connection.execute(
                text(f"DELETE FROM {table} WHERE org_id = ANY(:orgs)"),
                {"orgs": list(orgs)},
            )


FEED = {
    "type": "rss",
    "name": "Industry news",
    "config": {"url": "https://news.example.org/feed.xml"},
}


async def anonymous(stack: Stack) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=stack.app, client=("10.1.2.3", 4000)),
        base_url=PUBLIC_URL,
        headers={"Origin": PUBLIC_URL},
    )


async def post(who: Person, body: dict[str, Any]) -> httpx.Response:
    return await who.client.post("/api/sources", json=body, headers=who.headers)


async def patch(who: Person, source: str | uuid.UUID, body: dict[str, Any]) -> httpx.Response:
    return await who.client.patch(f"/api/sources/{source}", json=body, headers=who.headers)


async def rows(stack: Stack, sql: str, **params: Any) -> list[Any]:
    async with stack.owner.connect() as connection:
        return list((await connection.execute(text(sql), params)).all())


async def foreign_source(stack: Stack, **columns: Any) -> uuid.UUID:
    """A source of the other organization, made by the owner role as an operator would."""
    source = uuid.uuid4()
    values = {
        "id": source,
        "org_id": stack.org_b,
        "type": "rss",
        "name": "Not yours",
        "config": '{"url": "https://other.example/feed"}',
        "dedupe_key": uuid.uuid4().hex,
        "owner_user_id": None,
        **columns,
    }
    async with stack.owner.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO sources (id, org_id, owner_user_id, type, name, config, dedupe_key) "
                "VALUES (:id, :org_id, :owner_user_id, :type, :name, CAST(:config AS jsonb), "
                ":dedupe_key)"
            ),
            values,
        )
    return source


def details_of(entry: dict[str, Any]) -> dict[str, Any]:
    raw = entry["details"]
    return raw if isinstance(raw, dict) else json.loads(raw)


# --- who may call -----------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_roles_decide_who_reads_and_who_writes(make_stack: MakeStack) -> None:
    stack: Stack = await make_stack("on")
    admin = await person(stack, "admin")
    editor = await person(stack, "editor")
    member = await person(stack, "member")
    nobody = await anonymous(stack)
    try:
        source = (await post(admin, FEED)).json()["id"]
        reads = [
            ("GET", "/api/sources"),
            ("GET", f"/api/sources/{source}"),
            ("GET", f"/api/sources/{source}/runs"),
        ]
        writes = [
            ("POST", "/api/sources", FEED),
            ("PATCH", f"/api/sources/{source}", {"name": "x"}),
            ("DELETE", f"/api/sources/{source}", None),
        ]
        for method, path in reads:
            assert (await nobody.request(method, path)).status_code == 401
            assert (await member.client.request(method, path)).status_code == 403
            assert (await editor.client.request(method, path)).status_code == 200
            assert (await admin.client.request(method, path)).status_code == 200
        for method, path, body in writes:
            assert (await nobody.request(method, path, json=body)).status_code == 401
            for who in (member, editor):
                denied = await who.client.request(method, path, json=body, headers=who.headers)
                assert denied.status_code == 403, (who, method, path)
        # nothing the refused callers tried changed anything
        stored = await rows(stack, "SELECT name FROM sources WHERE id = :i", i=uuid.UUID(source))
        assert [r.name for r in stored] == [FEED["name"]]
        assert len(await rows(stack, "SELECT 1 FROM sources")) == 1
    finally:
        await nobody.aclose()


@pytest.mark.anyio
async def test_a_change_needs_the_csrf_token(make_stack: MakeStack) -> None:
    stack: Stack = await make_stack("on")
    admin = await person(stack, "admin")
    response = await admin.client.post("/api/sources", json=FEED)
    assert response.status_code == 403 and response.json()["code"] == "csrf_failed"
    assert await rows(stack, "SELECT 1 FROM sources") == []


@pytest.mark.anyio
async def test_a_demoted_administrator_is_refused_on_the_next_request(
    make_stack: MakeStack,
) -> None:
    stack: Stack = await make_stack("on")
    admin = await person(stack, "admin")
    other = await person(stack, "admin")  # so the organization keeps an administrator
    assert (await post(admin, FEED)).status_code == 201
    async with stack.owner.begin() as connection:
        await connection.execute(
            text("UPDATE users SET role = 'member' WHERE id = :i"), {"i": admin.id}
        )
    assert (await post(admin, {**FEED, "name": "Again"})).status_code == 403
    assert other.id != admin.id


# --- tenancy ------------------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_administrators_never_reach_another_organizations_sources(
    make_stack: MakeStack,
) -> None:
    stack: Stack = await make_stack("on")
    admin = await person(stack, "admin")
    foreign = await foreign_source(stack)
    listed = (await admin.client.get("/api/sources")).json()
    assert listed == {"items": [], "total": 0}
    for response in (
        await admin.client.get(f"/api/sources/{foreign}"),
        await admin.client.get(f"/api/sources/{foreign}/runs"),
        await patch(admin, foreign, {"name": "Mine now", "enabled": True}),
        await admin.client.delete(f"/api/sources/{foreign}", headers=admin.headers),
    ):
        assert response.status_code == 404  # the same answer as for an id that does not exist
        assert response.json()["code"] == "not_found"
    after = await rows(stack, "SELECT name, enabled FROM sources WHERE id = :i", i=foreign)
    assert [(r.name, r.enabled) for r in after] == [("Not yours", False)]
    # and the same configuration in two organizations is two sources, not a conflict
    assert (
        await post(admin, {**FEED, "config": {"url": "https://other.example/feed"}})
    ).status_code == 201


@pytest.mark.anyio
async def test_a_persons_own_feeds_are_not_the_administrators_to_manage(
    make_stack: MakeStack,
) -> None:
    stack: Stack = await make_stack("on")
    admin = await person(stack, "admin")
    owner = await person(stack, "member")
    personal = await foreign_source(stack, org_id=stack.org_a, owner_user_id=owner.id)
    shared = (await post(admin, FEED)).json()["id"]
    listed = (await admin.client.get("/api/sources")).json()
    assert [s["id"] for s in listed["items"]] == [shared] and listed["total"] == 1
    for response in (
        await admin.client.get(f"/api/sources/{personal}"),
        await patch(admin, personal, {"enabled": True}),
        await admin.client.delete(f"/api/sources/{personal}", headers=admin.headers),
    ):
        assert response.status_code == 404
    assert len(await rows(stack, "SELECT 1 FROM sources WHERE id = :i", i=personal)) == 1


# --- creating ----------------------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_a_new_source_is_stored_disabled_and_audited_without_its_configuration(
    make_stack: MakeStack,
) -> None:
    stack: Stack = await make_stack("on")
    admin = await person(stack, "admin")
    response = await post(admin, FEED)
    assert response.status_code == 201
    body = response.json()
    assert set(body) == {
        "id", "type", "name", "config", "enabled", "interval_seconds", "next_fetch_at",
        "last_fetched_at", "last_success_at", "consecutive_failures", "last_error_code",
        "created_by", "created_at", "updated_at",
    }  # fmt: skip
    assert (body["enabled"], body["interval_seconds"], body["consecutive_failures"]) == (
        False,
        3600,
        0,
    )
    assert body["created_by"] == str(admin.id) and body["config"] == FEED["config"]
    [row] = await rows(stack, "SELECT * FROM sources WHERE id = :i", i=uuid.UUID(body["id"]))
    assert row.owner_user_id is None and row.created_by == admin.id
    assert len(row.dedupe_key) == 64 and row.lease_token is None and row.etag is None
    [entry] = [e for e in await audit_rows(stack) if e["action"] == "source.created"]
    assert (entry["outcome"], entry["target"], entry["actor_id"]) == (
        "success",
        f"source:{body['id']}",
        admin.id,
    )
    assert details_of(entry) == {"type": "rss", "interval_seconds": 3600, "enabled": False}
    assert "news.example" not in json.dumps(entry, default=str)  # no address in the log


@pytest.mark.anyio
@pytest.mark.parametrize(
    "body",
    [
        {
            "type": "github",
            "name": "Widget releases",
            "config": {"repo": "acme/widget"},
            "interval_seconds": 7200,
            "enabled": True,
        },
        {"type": "hackernews", "name": "SAP on HN", "config": {"query": "sap", "min_points": 30}},
    ],
)
async def test_every_source_type_can_be_created(
    make_stack: MakeStack, body: dict[str, Any]
) -> None:
    stack: Stack = await make_stack("on")
    admin = await person(stack, "admin")
    response = await post(admin, body)
    assert response.status_code == 201, response.text
    created = response.json()
    assert created["type"] == body["type"] and created["enabled"] is body.get("enabled", False)
    assert created["config"] == body["config"] | (
        {"min_points": 20}
        if body["type"] == "hackernews" and "min_points" not in body["config"]
        else {}
    )


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("body", "kind", "field"),
    [
        ({**FEED, "type": "ftp"}, "literal_error", "type"),
        ({**FEED, "config": {}}, "url_required", "config"),
        ({**FEED, "config": {"url": "javascript:alert(1)"}}, "url_invalid", "config"),
        (
            {**FEED, "config": {"url": "https://news.example.org/f", "x": 1}},
            "unknown_setting",
            "config",
        ),
        ({"type": "github", "name": "x", "config": {"repo": "acme/.."}}, "repo_invalid", "config"),
        (
            {"type": "hackernews", "name": "x", "config": {"query": "<b>"}},
            "query_invalid",
            "config",
        ),
        ({**FEED, "name": "<script>alert(1)</script>"}, "invalid_text", "name"),
        ({**FEED, "name": "a‮b"}, "invalid_text", "name"),
        ({**FEED, "name": "   "}, "invalid_text", "name"),
        ({**FEED, "name": ""}, "string_too_short", "name"),
        ({**FEED, "name": "x" * 201}, "string_too_long", "name"),
        ({**FEED, "interval_seconds": 299}, "greater_than_equal", "interval_seconds"),
        ({**FEED, "interval_seconds": 604_801}, "less_than_equal", "interval_seconds"),
        ({**FEED, "interval_seconds": "soon"}, "int_parsing", "interval_seconds"),
        ({**FEED, "enabled": "yes please"}, "bool_parsing", "enabled"),
        ({**FEED, "owner_user_id": str(uuid.uuid4())}, "extra_forbidden", "owner_user_id"),
        ({**FEED, "created_by": str(uuid.uuid4())}, "extra_forbidden", "created_by"),
        ({**FEED, "dedupe_key": "a" * 64}, "extra_forbidden", "dedupe_key"),
        ({"type": "rss", "config": FEED["config"]}, "missing", "name"),
        ({"type": "rss", "name": "x"}, "missing", "config"),
        ({**FEED, "config": "https://news.example.org/f"}, "dict_type", "config"),
    ],
)
async def test_a_request_that_is_not_valid_is_refused_and_stores_nothing(
    make_stack: MakeStack, body: dict[str, Any], kind: str, field: str
) -> None:
    stack: Stack = await make_stack("on")
    admin = await person(stack, "admin")
    response = await post(admin, body)
    assert response.status_code == 422, response.text
    problem = response.json()
    assert problem["code"] == "validation_failed"
    assert any(e["type"] == kind and field in e["loc"] for e in problem["errors"]), problem[
        "errors"
    ]
    assert await rows(stack, "SELECT 1 FROM sources") == []


@pytest.mark.anyio
@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/feed",
        "http://127.1/feed",
        "http://2130706433/feed",
        "http://[::1]/feed",
        "http://169.254.169.254/latest/meta-data",
        "http://localhost:8000/feed",
        "http://redis/feed",
        "http://postgres:5432/x",
        "http://api:8000/api/readyz",
        "http://wiki.internal/feed",
        "https://pharos.example/feed",  # this installation itself (PUBLIC_URL)
    ],
)
async def test_an_internal_address_is_refused_when_it_is_typed_and_the_attempt_is_audited(
    make_stack: MakeStack, url: str
) -> None:
    stack: Stack = await make_stack("on")
    admin = await person(stack, "admin")
    response = await post(admin, {**FEED, "config": {"url": url}})
    assert response.status_code == 422
    assert any(e["type"] == "url_not_allowed" for e in response.json()["errors"])
    assert await rows(stack, "SELECT 1 FROM sources") == []
    [entry] = [e for e in await audit_rows(stack) if e["action"] == "source.created"]
    assert entry["outcome"] == "failure" and details_of(entry) == {
        "reason": "url_not_allowed",
        "type": "rss",
    }
    assert url.split("//")[1].split("/")[0].split(":")[0] not in json.dumps(entry, default=str)


@pytest.mark.anyio
async def test_the_same_source_twice_is_a_conflict_whatever_its_spelling(
    make_stack: MakeStack,
) -> None:
    stack: Stack = await make_stack("on")
    admin = await person(stack, "admin")
    assert (await post(admin, FEED)).status_code == 201
    again = await post(
        admin,
        {
            **FEED,
            "name": "Other name",
            "config": {"url": "HTTPS://News.Example.org:443/feed.xml#top"},
        },
    )
    assert again.status_code == 409 and again.json()["code"] == "source_conflict"
    assert (
        await post(admin, {"type": "github", "name": "a", "config": {"repo": "Acme/Widget"}})
    ).status_code == 201
    dup = await post(admin, {"type": "github", "name": "b", "config": {"repo": "acme/widget"}})
    assert dup.status_code == 409
    assert len(await rows(stack, "SELECT 1 FROM sources")) == 2
    failures = [e for e in await audit_rows(stack) if e["outcome"] == "failure"]
    assert [details_of(e)["reason"] for e in failures] == ["duplicate", "duplicate"]


@pytest.mark.anyio
async def test_two_administrators_creating_the_same_source_at_once_make_one(
    make_stack: MakeStack,
) -> None:
    stack: Stack = await make_stack("on")
    first, second = await person(stack, "admin"), await person(stack, "admin")
    answers = await asyncio.gather(post(first, FEED), post(second, FEED))
    assert sorted(a.status_code for a in answers) == [201, 409]
    assert len(await rows(stack, "SELECT 1 FROM sources")) == 1


@pytest.mark.anyio
async def test_an_organization_cannot_have_more_than_the_most_sources(
    make_stack: MakeStack,
) -> None:
    stack: Stack = await make_stack("on")
    admin = await person(stack, "admin")
    async with stack.owner.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO sources (org_id, type, name, config, dedupe_key) "
                "SELECT :org, 'rss', 'bulk ' || n, '{}'::jsonb, 'bulk' || n "
                "FROM generate_series(1, 200) AS n"
            ),
            {"org": stack.org_a},
        )
    response = await post(admin, FEED)
    assert response.status_code == 409 and response.json()["code"] == "source_limit"
    [entry] = [e for e in await audit_rows(stack) if e["outcome"] == "failure"]
    assert details_of(entry)["reason"] == "limit"


# --- reading -----------------------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_the_list_pages_filters_and_sorts_by_name(make_stack: MakeStack) -> None:
    stack: Stack = await make_stack("on")
    admin = await person(stack, "admin")
    editor = await person(stack, "editor")
    names = ["banana", "Apple", "cherry"]
    for n, name in enumerate(names):
        body = {
            "type": "rss",
            "name": name,
            "config": {"url": f"https://f{n}.example.org/feed"},
            "enabled": n == 0,
        }
        assert (await post(admin, body)).status_code == 201
    await post(admin, {"type": "github", "name": "dev", "config": {"repo": "acme/widget"}})
    everything = (await editor.client.get("/api/sources")).json()
    assert everything["total"] == 4
    assert [s["name"] for s in everything["items"]] == ["Apple", "banana", "cherry", "dev"]
    page = (await editor.client.get("/api/sources", params={"limit": 2, "offset": 1})).json()
    assert page["total"] == 4 and [s["name"] for s in page["items"]] == ["banana", "cherry"]
    only_github = (await editor.client.get("/api/sources", params={"type": "github"})).json()
    assert [s["name"] for s in only_github["items"]] == ["dev"] and only_github["total"] == 1
    enabled = (await editor.client.get("/api/sources", params={"enabled": "true"})).json()
    assert [s["name"] for s in enabled["items"]] == ["banana"] and enabled["total"] == 1
    for bad in (
        {"limit": 0},
        {"limit": 101},
        {"offset": -1},
        {"offset": 100_001},
        {"type": "ftp"},
        {"enabled": "maybe"},
    ):
        assert (await editor.client.get("/api/sources", params=bad)).status_code == 422


@pytest.mark.anyio
async def test_the_fetch_state_of_a_source_is_shown_but_never_its_internals(
    make_stack: MakeStack,
) -> None:
    stack: Stack = await make_stack("on")
    admin = await person(stack, "admin")
    source = (await post(admin, FEED)).json()["id"]
    async with stack.owner.begin() as connection:
        await connection.execute(
            text(
                "UPDATE sources SET consecutive_failures = 3, last_error_code = 'timed_out', "
                "etag = '\"secret-validator\"', lease_token = gen_random_uuid(), "
                "lease_until = now() + interval '5 minutes' WHERE id = :i"
            ),
            {"i": uuid.UUID(source)},
        )
    shown = (await admin.client.get(f"/api/sources/{source}")).json()
    assert (shown["consecutive_failures"], shown["last_error_code"]) == (3, "timed_out")
    text_form = json.dumps(shown)
    assert (
        "secret-validator" not in text_form
        and "lease" not in text_form
        and "dedupe" not in text_form
    )


@pytest.mark.anyio
async def test_an_unknown_or_malformed_id_is_a_clean_error(make_stack: MakeStack) -> None:
    stack: Stack = await make_stack("on")
    admin = await person(stack, "admin")
    assert (await admin.client.get(f"/api/sources/{uuid.uuid4()}")).status_code == 404
    assert (await admin.client.get("/api/sources/not-a-uuid")).status_code == 422
    assert (await patch(admin, uuid.uuid4(), {"name": "x"})).status_code == 404
    assert (
        await admin.client.delete(f"/api/sources/{uuid.uuid4()}", headers=admin.headers)
    ).status_code == 404
    assert (await admin.client.get(f"/api/sources/{uuid.uuid4()}/runs")).status_code == 404


@pytest.mark.anyio
async def test_the_runs_of_a_source_come_newest_first_and_are_bounded(
    make_stack: MakeStack,
) -> None:
    stack: Stack = await make_stack("on")
    admin = await person(stack, "admin")
    source = uuid.UUID((await post(admin, FEED)).json()["id"])
    async with stack.owner.begin() as connection:
        for n in range(25):
            await connection.execute(
                text(
                    "INSERT INTO fetch_runs (org_id, source_id, started_at, finished_at, outcome, "
                    "items_seen, items_new) VALUES (:org, :src, now() - make_interval(mins => :n), "
                    "now(), 'ok', :n, 0)"
                ),
                {"org": stack.org_a, "src": source, "n": n},
            )
    runs = (await admin.client.get(f"/api/sources/{source}/runs")).json()["items"]
    assert len(runs) == 20 and [r["items_seen"] for r in runs][:3] == [0, 1, 2]
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
    assert (
        len(
            (await admin.client.get(f"/api/sources/{source}/runs", params={"limit": 5})).json()[
                "items"
            ]
        )
        == 5
    )
    for bad in (0, 51):
        assert (
            await admin.client.get(f"/api/sources/{source}/runs", params={"limit": bad})
        ).status_code == 422


# --- changing ------------------------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_a_change_is_saved_and_audited_by_kind_without_values(make_stack: MakeStack) -> None:
    stack: Stack = await make_stack("on")
    admin = await person(stack, "admin")
    source = (await post(admin, FEED)).json()["id"]
    response = await patch(
        admin, source, {"name": "Renamed", "interval_seconds": 900, "enabled": True}
    )
    assert response.status_code == 200
    body = response.json()
    assert (body["name"], body["interval_seconds"], body["enabled"]) == ("Renamed", 900, True)
    entries = [
        e
        for e in await audit_rows(stack)
        if e["action"].startswith("source.") and e["action"] != "source.created"
    ]
    assert [e["action"] for e in entries] == ["source.updated", "source.enabled"]
    assert details_of(entries[0]) == {"type": "rss", "fields": "name,interval_seconds"}
    assert "Renamed" not in json.dumps(entries, default=str)
    off = await patch(admin, source, {"enabled": False})
    assert off.json()["enabled"] is False
    assert (await audit_rows(stack))[-1]["action"] == "source.disabled"


@pytest.mark.anyio
async def test_a_new_configuration_gets_a_new_key_and_cannot_take_an_existing_one(
    make_stack: MakeStack,
) -> None:
    stack: Stack = await make_stack("on")
    admin = await person(stack, "admin")
    first = (await post(admin, FEED)).json()["id"]
    second = (
        await post(
            admin, {**FEED, "name": "Second", "config": {"url": "https://second.example.org/feed"}}
        )
    ).json()["id"]
    before = (
        await rows(stack, "SELECT dedupe_key FROM sources WHERE id = :i", i=uuid.UUID(second))
    )[0].dedupe_key
    moved = await patch(admin, second, {"config": {"url": "https://third.example.org/feed"}})
    assert moved.status_code == 200 and moved.json()["config"] == {
        "url": "https://third.example.org/feed"
    }
    after = (
        await rows(stack, "SELECT dedupe_key FROM sources WHERE id = :i", i=uuid.UUID(second))
    )[0].dedupe_key
    assert after != before
    clash = await patch(admin, second, {"config": FEED["config"]})
    assert clash.status_code == 409 and clash.json()["code"] == "source_conflict"
    kept = (await rows(stack, "SELECT config FROM sources WHERE id = :i", i=uuid.UUID(second)))[
        0
    ].config
    assert kept == {"url": "https://third.example.org/feed"}
    assert first != second


@pytest.mark.anyio
async def test_a_change_to_an_internal_address_is_refused_and_audited(
    make_stack: MakeStack,
) -> None:
    stack: Stack = await make_stack("on")
    admin = await person(stack, "admin")
    source = (await post(admin, FEED)).json()["id"]
    response = await patch(admin, source, {"config": {"url": "http://127.0.0.1/feed"}})
    assert response.status_code == 422 and response.json()["errors"][0]["type"] == "url_not_allowed"
    stored = (await rows(stack, "SELECT config FROM sources WHERE id = :i", i=uuid.UUID(source)))[
        0
    ].config
    assert stored == FEED["config"]
    [entry] = [e for e in await audit_rows(stack) if e["outcome"] == "failure"]
    assert (entry["action"], entry["target"]) == ("source.updated", f"source:{source}")
    assert "127.0.0.1" not in json.dumps(entry, default=str)


@pytest.mark.anyio
@pytest.mark.parametrize(
    "body",
    [
        {},
        {"name": None},
        {"enabled": None},
        {"config": None},
        {"type": "github"},
        {"owner_user_id": str(uuid.uuid4())},
        {"dedupe_key": "x"},
        {"etag": "x"},
        {"next_fetch_at": "2020-01-01T00:00:00Z"},
        {"consecutive_failures": 0},
        {"interval_seconds": 10},
        {"name": "<b>x</b>"},
    ],
)
async def test_a_change_that_is_not_allowed_is_refused_and_nothing_moves(
    make_stack: MakeStack, body: dict[str, Any]
) -> None:
    stack: Stack = await make_stack("on")
    admin = await person(stack, "admin")
    source = (await post(admin, FEED)).json()["id"]
    before = await rows(stack, "SELECT * FROM sources WHERE id = :i", i=uuid.UUID(source))
    assert (await patch(admin, source, body)).status_code == 422
    assert await rows(stack, "SELECT * FROM sources WHERE id = :i", i=uuid.UUID(source)) == before


@pytest.mark.anyio
async def test_a_change_that_changes_nothing_is_not_recorded(make_stack: MakeStack) -> None:
    stack: Stack = await make_stack("on")
    admin = await person(stack, "admin")
    created = (await post(admin, FEED)).json()
    source = created["id"]
    count = len(await audit_rows(stack))
    same = await patch(
        admin, source, {"name": FEED["name"], "enabled": False, "interval_seconds": 3600}
    )
    assert same.status_code == 200 and len(await audit_rows(stack)) == count
    # nothing was written either: the row (and its update time) is as it was
    assert same.json()["updated_at"] == created["updated_at"]
    stored = await rows(stack, "SELECT updated_at FROM sources WHERE id = :i", i=uuid.UUID(source))
    assert stored[0].updated_at.isoformat() == created["updated_at"].replace("Z", "+00:00")


@pytest.mark.anyio
async def test_changing_a_source_leaves_the_workers_state_alone(make_stack: MakeStack) -> None:
    stack: Stack = await make_stack("on")
    admin = await person(stack, "admin")
    source = uuid.UUID((await post(admin, FEED)).json()["id"])
    async with stack.owner.begin() as connection:
        await connection.execute(
            text(
                "UPDATE sources SET etag = '\"v1\"', consecutive_failures = 2, last_error_code = 'timed_out' WHERE id = :i"
            ),
            {"i": source},
        )
    assert (await patch(admin, source, {"name": "Renamed", "enabled": True})).status_code == 200
    [row] = await rows(
        stack,
        "SELECT etag, consecutive_failures, last_error_code FROM sources WHERE id = :i",
        i=source,
    )
    assert (row.etag, row.consecutive_failures, row.last_error_code) == ('"v1"', 2, "timed_out")


@pytest.mark.anyio
async def test_two_administrators_changing_one_source_at_once_both_succeed_in_turn(
    make_stack: MakeStack,
) -> None:
    stack: Stack = await make_stack("on")
    first, second = await person(stack, "admin"), await person(stack, "admin")
    source = (await post(first, FEED)).json()["id"]
    answers = await asyncio.gather(
        patch(first, source, {"name": "From the first"}),
        patch(second, source, {"interval_seconds": 1800}),
    )
    assert [a.status_code for a in answers] == [200, 200]
    [row] = await rows(
        stack, "SELECT name, interval_seconds FROM sources WHERE id = :i", i=uuid.UUID(source)
    )
    assert (row.name, row.interval_seconds) == ("From the first", 1800)  # neither change was lost
    assert len([e for e in await audit_rows(stack) if e["action"] == "source.updated"]) == 2


# --- deleting -------------------------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_deleting_removes_the_sources_history_but_keeps_the_items(
    make_stack: MakeStack,
) -> None:
    stack: Stack = await make_stack("on")
    admin = await person(stack, "admin")
    source = uuid.UUID((await post(admin, FEED)).json()["id"])
    async with stack.owner.begin() as connection:
        await connection.execute(
            text("INSERT INTO fetch_runs (org_id, source_id, outcome) VALUES (:o, :s, 'ok')"),
            {"o": stack.org_a, "s": source},
        )
        await connection.execute(
            text(
                "INSERT INTO raw_items (org_id, source_id, external_id, title, url, content_sha256) "
                "VALUES (:o, :s, 'g1', 't', 'https://a.example/1', repeat('a', 64))"
            ),
            {"o": stack.org_a, "s": source},
        )
        item = (
            await connection.execute(
                text(
                    "INSERT INTO items (org_id, canonical_url, url_hash, title, content_sha256) "
                    "VALUES (:o, 'https://a.example/1', repeat('b', 64), 't', repeat('c', 64)) RETURNING id"
                ),
                {"o": stack.org_a},
            )
        ).scalar_one()
        await connection.execute(
            text("INSERT INTO item_sources (org_id, item_id, source_id) VALUES (:o, :i, :s)"),
            {"o": stack.org_a, "i": item, "s": source},
        )
    response = await admin.client.delete(f"/api/sources/{source}", headers=admin.headers)
    assert response.status_code == 204 and response.content == b""
    assert await rows(stack, "SELECT 1 FROM sources WHERE id = :i", i=source) == []
    for table in ("fetch_runs", "raw_items", "item_sources"):
        assert await rows(stack, f"SELECT 1 FROM {table}") == [], table
    assert len(await rows(stack, "SELECT 1 FROM items WHERE id = :i", i=item)) == 1
    entry = next(e for e in await audit_rows(stack) if e["action"] == "source.deleted")
    assert (entry["target"], details_of(entry)) == (
        f"source:{source}",
        {"type": "rss", "enabled": False},
    )
    assert (
        await admin.client.delete(f"/api/sources/{source}", headers=admin.headers)
    ).status_code == 404
    assert (await admin.client.get(f"/api/sources/{source}")).status_code == 404
    again = await post(admin, FEED)  # the same configuration can be added again
    assert again.status_code == 201


@pytest.mark.anyio
async def test_a_change_waits_for_a_concurrent_one_and_does_not_undo_it(
    make_stack: MakeStack,
) -> None:
    """Deterministic version of the race: the test holds the row, the request must queue behind it
    and then build on what was committed, not on what it could see before."""
    stack: Stack = await make_stack("on")
    admin = await person(stack, "admin")
    source = uuid.UUID((await post(admin, FEED)).json()["id"])
    async with stack.owner.connect() as connection:
        transaction = await connection.begin()
        await connection.execute(
            text("UPDATE sources SET name = 'Changed elsewhere' WHERE id = :i"), {"i": source}
        )
        pending = asyncio.create_task(patch(admin, source, {"interval_seconds": 900}))
        await asyncio.sleep(0.5)
        assert not pending.done()  # the request is waiting for the row
        await transaction.commit()
        response = await pending
    assert response.status_code == 200
    assert response.json()["name"] == "Changed elsewhere"
    [row] = await rows(stack, "SELECT name, interval_seconds FROM sources WHERE id = :i", i=source)
    assert (row.name, row.interval_seconds) == ("Changed elsewhere", 900)


@pytest.mark.anyio
async def test_parallel_creations_cannot_get_past_the_cap(make_stack: MakeStack) -> None:
    stack: Stack = await make_stack("on")
    admins = [await person(stack, "admin") for _ in range(5)]
    async with stack.owner.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO sources (org_id, type, name, config, dedupe_key) "
                "SELECT :org, 'rss', 'bulk ' || n, '{}'::jsonb, 'bulk' || n "
                "FROM generate_series(1, 199) AS n"
            ),
            {"org": stack.org_a},
        )
    answers = await asyncio.gather(
        *[
            post(admin, {**FEED, "name": f"n{n}", "config": {"url": f"https://f{n}.example.org/x"}})
            for n, admin in enumerate(admins)
        ]
    )
    assert sorted(a.status_code for a in answers) == [201, 409, 409, 409, 409]
    assert len(await rows(stack, "SELECT 1 FROM sources")) == 200


@pytest.mark.anyio
async def test_switching_a_source_on_checks_its_stored_address_again(make_stack: MakeStack) -> None:
    stack: Stack = await make_stack("on")
    admin = await person(stack, "admin")
    old = await foreign_source(
        stack, org_id=stack.org_a, config='{"url": "http://10.0.0.5/feed"}', name="Stored earlier"
    )
    refused = await patch(admin, old, {"enabled": True})
    assert refused.status_code == 422 and refused.json()["errors"][0]["type"] == "url_not_allowed"
    stored = await rows(stack, "SELECT enabled FROM sources WHERE id = :i", i=old)
    assert stored[0].enabled is False
    failure = [e for e in await audit_rows(stack) if e["outcome"] == "failure"]
    assert [(e["action"], e["target"]) for e in failure] == [("source.updated", f"source:{old}")]
    # a change that does not switch it on does not look at the address
    assert (await patch(admin, old, {"name": "Renamed"})).status_code == 200
    # and a good address is switched on as before
    good = (await post(admin, FEED)).json()["id"]
    assert (await patch(admin, good, {"enabled": True})).json()["enabled"] is True


@pytest.mark.anyio
@pytest.mark.parametrize("separator", ["。", "．", "｡"])
async def test_an_internal_address_with_another_kind_of_dot_is_refused_and_audited(
    make_stack: MakeStack, separator: str
) -> None:
    stack: Stack = await make_stack("on")
    admin = await person(stack, "admin")
    for url in (
        f"http://169.254.169.254{separator}/latest",
        f"https://pharos.example{separator}/f",
    ):
        response = await post(admin, {**FEED, "config": {"url": url}})
        assert response.status_code == 422
        assert response.json()["errors"][0]["type"] == "url_not_allowed"
    assert await rows(stack, "SELECT 1 FROM sources") == []
    failures = [e for e in await audit_rows(stack) if e["outcome"] == "failure"]
    assert len(failures) == 2
    # the same feed twice with another dot is still a duplicate, not a second source
    assert (await post(admin, FEED)).status_code == 201
    again = {**FEED, "config": {"url": f"https://news{separator}example.org/feed.xml"}}
    assert (await post(admin, again)).status_code == 409


@pytest.mark.anyio
async def test_a_creation_queues_behind_the_organizations_creation_lock(
    make_stack: MakeStack,
) -> None:
    """Deterministic version of the cap race: the test holds the lock a creation must take, and the
    request has to wait for it. Without the lock it would count and insert straight away."""
    stack: Stack = await make_stack("on")
    admin = await person(stack, "admin")
    async with stack.owner.connect() as connection:
        transaction = await connection.begin()
        await connection.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:name, 0))"),
            {"name": f"sources:{stack.org_a}"},
        )
        pending = asyncio.create_task(post(admin, FEED))
        await asyncio.sleep(0.5)
        assert not pending.done()  # the creation is waiting for its turn
        await transaction.commit()
        response = await pending
    assert response.status_code == 201
