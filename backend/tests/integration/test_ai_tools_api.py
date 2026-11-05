"""The approved AI tools against a real database: roles, tenancy, validation, audit, the cap and the
database's own second line (M5-07, ADR-0026)."""

# ruff: noqa: F811, RUF001  (fixtures are imported by name; look-alike characters are the point)

import asyncio
import json
import uuid
from typing import Any

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from .test_local_auth import (  # noqa: F401  (fixtures and helpers used by these tests)
    PUBLIC_URL,
    MakeStack,
    Stack,
    audit_rows,
    make_stack,
)
from .test_user_admin import Person, person

pytestmark = pytest.mark.integration

TOOL = {"name": "Example Assistant", "url": "https://assistant.example.org/chat"}


async def anonymous(stack: Stack) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=stack.app, client=("10.1.2.3", 4000)),
        base_url=PUBLIC_URL,
        headers={"Origin": PUBLIC_URL},
    )


async def post(who: Person, body: dict[str, Any]) -> httpx.Response:
    return await who.client.post("/api/ai-tools", json=body, headers=who.headers)


async def patch(who: Person, tool: str | uuid.UUID, body: dict[str, Any]) -> httpx.Response:
    return await who.client.patch(f"/api/ai-tools/{tool}", json=body, headers=who.headers)


async def rows(stack: Stack, sql: str, **params: Any) -> list[Any]:
    async with stack.owner.connect() as connection:
        return list((await connection.execute(text(sql), params)).all())


async def foreign_tool(stack: Stack, name: str = "Not yours") -> uuid.UUID:
    """A tool of the other organization, made by the owner role as an operator would."""
    tool = uuid.uuid4()
    async with stack.owner.begin() as connection:
        await connection.execute(
            text("INSERT INTO ai_tools (id, org_id, name, url) VALUES (:i, :o, :n, :u)"),
            {"i": tool, "o": stack.org_b, "n": name, "u": "https://other.example.net/"},
        )
    return tool


def details_of(entry: dict[str, Any]) -> dict[str, Any]:
    raw = entry["details"]
    return raw if isinstance(raw, dict) else json.loads(raw)


def tool_audit(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [e for e in entries if e["action"].startswith("ai_tool.")]


# --- who may call -----------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_everybody_signed_in_reads_the_list_and_only_administrators_change_it(
    make_stack: MakeStack,
) -> None:
    stack: Stack = await make_stack("on")
    admin = await person(stack, "admin")
    editor = await person(stack, "editor")
    member = await person(stack, "member")
    nobody = await anonymous(stack)
    try:
        tool = (await post(admin, TOOL)).json()["id"]
        assert (await nobody.get("/api/ai-tools")).status_code == 401
        for who in (admin, editor, member):
            seen = await who.client.get("/api/ai-tools")
            assert seen.status_code == 200 and [t["name"] for t in seen.json()["items"]] == [
                TOOL["name"]
            ]
        writes = [
            ("POST", "/api/ai-tools", TOOL),
            ("PATCH", f"/api/ai-tools/{tool}", {"name": "x"}),
            ("DELETE", f"/api/ai-tools/{tool}", None),
        ]
        for method, path, body in writes:
            assert (await nobody.request(method, path, json=body)).status_code == 401
            for who in (member, editor):
                denied = await who.client.request(method, path, json=body, headers=who.headers)
                assert denied.status_code == 403, (who, method, path)
        stored = await rows(stack, "SELECT name FROM ai_tools")
        assert [r.name for r in stored] == [TOOL["name"]]  # the refused callers changed nothing
    finally:
        await nobody.aclose()


@pytest.mark.anyio
async def test_a_change_needs_the_csrf_token(make_stack: MakeStack) -> None:
    stack: Stack = await make_stack("on")
    admin = await person(stack, "admin")
    response = await admin.client.post("/api/ai-tools", json=TOOL)
    assert response.status_code == 403 and response.json()["code"] == "csrf_failed"
    assert await rows(stack, "SELECT 1 FROM ai_tools") == []


@pytest.mark.anyio
async def test_a_demoted_administrator_is_refused_on_the_next_request(
    make_stack: MakeStack,
) -> None:
    stack: Stack = await make_stack("on")
    admin = await person(stack, "admin")
    spare = await person(stack, "admin")  # so that the organization still has one
    assert (await post(admin, TOOL)).status_code == 201
    async with stack.owner.begin() as connection:
        await connection.execute(
            text("UPDATE users SET role = 'member' WHERE id = :i"), {"i": admin.id}
        )
    assert (await post(admin, {**TOOL, "name": "Second"})).status_code == 403
    assert (await post(spare, {**TOOL, "name": "Second"})).status_code == 201  # control


# --- tenancy ----------------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_administrators_never_reach_another_organizations_tools(
    make_stack: MakeStack,
) -> None:
    stack: Stack = await make_stack("on")
    admin = await person(stack, "admin")
    foreign = await foreign_tool(stack)
    assert (await admin.client.get("/api/ai-tools")).json() == {"items": []}
    for response in (
        await patch(admin, foreign, {"name": "Mine now"}),
        await admin.client.delete(f"/api/ai-tools/{foreign}", headers=admin.headers),
    ):
        assert response.status_code == 404  # the same answer as for an id that does not exist
        assert response.json()["code"] == "not_found"
    after = await rows(stack, "SELECT name FROM ai_tools WHERE id = :i", i=foreign)
    assert [r.name for r in after] == ["Not yours"]
    # the same name in two organizations is two tools, not a conflict
    other = await foreign_tool(stack, name=TOOL["name"])
    assert other and (await post(admin, TOOL)).status_code == 201


@pytest.mark.anyio
async def test_the_cap_is_per_organization(make_stack: MakeStack) -> None:
    stack: Stack = await make_stack("on")
    admin = await person(stack, "admin")
    for n in range(10):
        await foreign_tool(stack, name=f"Other {n}")
    assert (await post(admin, TOOL)).status_code == 201  # the other organization's ten do not count


# --- using the list ---------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_the_list_is_empty_until_an_administrator_adds_a_tool(make_stack: MakeStack) -> None:
    stack: Stack = await make_stack("on")
    member = await person(stack, "member")
    assert (await member.client.get("/api/ai-tools")).json() == {"items": []}


@pytest.mark.anyio
async def test_a_tool_is_added_listed_by_name_changed_and_deleted(make_stack: MakeStack) -> None:
    stack: Stack = await make_stack("on")
    admin = await person(stack, "admin")
    member = await person(stack, "member")
    zeta = await post(admin, {"name": "zeta chat", "url": "https://zeta.example.org/"})
    alpha = await post(admin, {"name": "Alpha Assistant", "url": "https://alpha.example.org/"})
    assert zeta.status_code == alpha.status_code == 201
    # What everybody sees is the id, the name and the address; nothing about who made it.
    assert set(alpha.json()) == {"id", "name", "url"}
    listed = (await member.client.get("/api/ai-tools")).json()["items"]
    assert [t["name"] for t in listed] == ["Alpha Assistant", "zeta chat"]  # by name, not by case
    changed = await patch(admin, alpha.json()["id"], {"url": "https://alpha.example.org/new"})
    assert changed.status_code == 200 and changed.json()["url"] == "https://alpha.example.org/new"
    assert changed.json()["name"] == "Alpha Assistant"  # what was not sent stays
    renamed = await patch(admin, alpha.json()["id"], {"name": "Alpha 2"})
    assert renamed.json()["name"] == "Alpha 2" and renamed.json()["url"].endswith("/new")
    gone = await admin.client.delete(f"/api/ai-tools/{zeta.json()['id']}", headers=admin.headers)
    assert gone.status_code == 204
    again = await admin.client.delete(f"/api/ai-tools/{zeta.json()['id']}", headers=admin.headers)
    assert again.status_code == 404
    assert [t["name"] for t in (await member.client.get("/api/ai-tools")).json()["items"]] == [
        "Alpha 2"
    ]


@pytest.mark.anyio
async def test_an_empty_change_and_unknown_fields_are_refused(make_stack: MakeStack) -> None:
    stack: Stack = await make_stack("on")
    admin = await person(stack, "admin")
    tool = (await post(admin, TOOL)).json()["id"]
    assert (await patch(admin, tool, {})).status_code == 422
    assert (await patch(admin, tool, {"name": "ok", "org_id": str(stack.org_b)})).status_code == 422
    assert (await post(admin, {**TOOL, "created_by": str(admin.id)})).status_code == 422


# --- what is accepted -------------------------------------------------------------------------------

GOOD_URLS = [
    "https://chat.example.org",
    "https://chat.example.org/",
    "https://chat.example.org/path/to?model=small&x=1",
    "https://ai.corp.internal:8443/portal",
    "https://intranet/",
    "https://a-b.c-d.example.org/",
    "https://xn--exmple-cua.example.org/",  # an international name in its punycode form
    "https://chat.example.org./",
    "https://chat.example.org/" + "a" * 270,
    "https://123.example.com/",
    "https://localhost.example.org/",
]

BAD_URLS = [
    ("http://chat.example.org/", "https_only"),
    ("HTTPS://chat.example.org/", "https_only"),
    ("ftp://chat.example.org/", "https_only"),
    ("javascript:alert(1)", "https_only"),
    ("data:text/html,<script>alert(1)</script>", "https_only"),
    ("//chat.example.org/", "https_only"),
    ("chat.example.org", "https_only"),
    ("https://user:secret@chat.example.org/", "credentials_not_allowed"),
    ("https://chat.example.org/?next=a@b", "credentials_not_allowed"),
    ("https://chat.example.org/#fragment", "invalid_url"),
    ("https://chat.example.org/a b", "invalid_url"),
    ("https://chat.example.org/a\tb", "invalid_url"),
    ("https://chat.example.org/a\nb", "invalid_url"),
    ("https://chat.example.org/‮", "invalid_url"),  # a right-to-left override
    ("https://chat.example.org/ ", "invalid_url"),  # a no-break space
    ("https://chat.example.org/ ", "invalid_url"),  # a line separator
    ("https://chat.example.org/\u0085", "invalid_url"),  # a control character
    ("https://exämple.org/", "invalid_url"),  # a non-ASCII host: write it in punycode
    ("https://chat.example.org:99999/", "invalid_url"),
    ("https://chat.example.org:abc/", "invalid_url"),
    ("https:///path", "invalid_url"),
    ("https://", "invalid_url"),
    ("https://-bad.example.org/", "invalid_url"),
    ("https://under_score.example.org/", "invalid_url"),
    ("https://a..b.example.org/", "invalid_url"),
    ("https://[::1]/", "ip_address_not_allowed"),
    ("https://[v1.x]/", "ip_address_not_allowed"),
    ("https://foo.127/", "ip_address_not_allowed"),
    ("https://foo.0x7f/", "ip_address_not_allowed"),
    ("https://chat.example.org../", "invalid_url"),
    ("https://localhost/", "local_host_not_allowed"),
    ("https://app.localhost:8443/x", "local_host_not_allowed"),
    ("https://LOCALHOST./", "local_host_not_allowed"),
    ("https://[2001:db8::1]:8443/", "ip_address_not_allowed"),
    ("https://127.0.0.1/", "ip_address_not_allowed"),
    ("https://10.0.0.5:8080/x", "ip_address_not_allowed"),
    ("https://169.254.169.254/latest/meta-data", "ip_address_not_allowed"),
    ("https://2130706433/", "ip_address_not_allowed"),  # 127.0.0.1 as one number
    ("https://0x7f.1/", "ip_address_not_allowed"),  # and in hexadecimal
    ("https://0177.0.0.1/", "ip_address_not_allowed"),  # and in octal
    ("https://[::ffff:127.0.0.1]/", "ip_address_not_allowed"),
]


@pytest.mark.anyio
@pytest.mark.parametrize("url", GOOD_URLS)
async def test_an_address_a_browser_may_safely_be_sent_to_is_accepted(
    make_stack: MakeStack, url: str
) -> None:
    stack: Stack = await make_stack("on")
    admin = await person(stack, "admin")
    answer = await post(admin, {"name": "Tool", "url": url})
    assert answer.status_code == 201, (url, answer.text)
    assert answer.json()["url"] == url  # stored as it was written, not rewritten


@pytest.mark.anyio
@pytest.mark.parametrize(("url", "kind"), BAD_URLS)
async def test_an_address_that_is_not_safe_to_send_a_browser_to_is_refused(
    make_stack: MakeStack, url: str, kind: str
) -> None:
    stack: Stack = await make_stack("on")
    admin = await person(stack, "admin")
    answer = await post(admin, {"name": "Tool", "url": url})
    assert answer.status_code == 422, (url, answer.text)
    errors = answer.json()["errors"]
    assert errors[0]["loc"] == ["body", "url"] and errors[0]["type"] == kind, (url, errors)
    assert await rows(stack, "SELECT 1 FROM ai_tools") == []
    # the same address is refused when an existing tool is changed to it
    tool = (await post(admin, TOOL)).json()["id"]
    assert (await patch(admin, tool, {"url": url})).status_code == 422
    assert (await rows(stack, "SELECT url FROM ai_tools"))[0].url == TOOL["url"]


@pytest.mark.anyio
async def test_a_too_long_address_or_name_is_refused(make_stack: MakeStack) -> None:
    stack: Stack = await make_stack("on")
    admin = await person(stack, "admin")
    assert (
        await post(admin, {"name": "T", "url": "https://x.example.org/" + "a" * 300})
    ).status_code == 422
    assert (await post(admin, {"name": "n" * 41, "url": TOOL["url"]})).status_code == 422
    assert (await post(admin, {"name": "n" * 40, "url": TOOL["url"]})).status_code == 201  # control


@pytest.mark.anyio
@pytest.mark.parametrize(
    "name",
    [
        "",
        "   ",
        "<b>Bold</b>",
        "Tool​",  # a zero-width space
        "Tool‮",  # a right-to-left override
        "Tool\x00",
        "Tool\nTwo",
        "<script>alert(1)</script>",
        "&lt;b&gt;",
    ],
)
async def test_a_name_that_is_not_plain_text_is_refused_not_repaired(
    make_stack: MakeStack, name: str
) -> None:
    stack: Stack = await make_stack("on")
    admin = await person(stack, "admin")
    answer = await post(admin, {"name": name, "url": TOOL["url"]})
    assert answer.status_code == 422, (name, answer.text)
    assert answer.json()["errors"][0]["loc"] == ["body", "name"]
    assert await rows(stack, "SELECT 1 FROM ai_tools") == []


@pytest.mark.anyio
@pytest.mark.parametrize("name", ["Plain Tool", "Ünïcode Çhat", "Чат", "助手", "A-1_b.c (beta)"])
async def test_ordinary_names_in_any_language_are_accepted(
    make_stack: MakeStack, name: str
) -> None:
    stack: Stack = await make_stack("on")
    admin = await person(stack, "admin")
    answer = await post(admin, {"name": name, "url": TOOL["url"]})
    assert answer.status_code == 201 and answer.json()["name"] == name


# --- the same name twice ----------------------------------------------------------------------------


@pytest.mark.anyio
async def test_the_same_name_in_any_case_is_one_tool_and_a_decomposed_form_is_not_repaired(
    make_stack: MakeStack,
) -> None:
    stack: Stack = await make_stack("on")
    admin = await person(stack, "admin")
    first = await post(admin, {"name": "Café Chat", "url": "https://a.example.org/"})
    assert first.status_code == 201
    for twin in ("café chat", "CAFÉ CHAT", "  Café Chat  "):
        clash = await post(admin, {"name": twin, "url": "https://b.example.org/"})
        assert clash.status_code == 409 and clash.json()["code"] == "ai_tool_conflict", twin
    # "e" followed by a combining accent is the same name to a person, but it is not plain text in
    # its normal form: refused, not repaired (the table's own index still treats it as the same name).
    decomposed = await post(admin, {"name": "Cafe\u0301 Chat", "url": "https://b.example.org/"})
    assert decomposed.status_code == 422
    other = await post(admin, {"name": "Cafe Chat", "url": "https://b.example.org/"})
    assert other.status_code == 201  # a different name: no accent
    assert len(await rows(stack, "SELECT 1 FROM ai_tools")) == 2


@pytest.mark.anyio
async def test_a_tool_cannot_be_renamed_to_the_name_of_another(make_stack: MakeStack) -> None:
    stack: Stack = await make_stack("on")
    admin = await person(stack, "admin")
    first = (await post(admin, {"name": "First", "url": "https://a.example.org/"})).json()["id"]
    await post(admin, {"name": "Second", "url": "https://b.example.org/"})
    clash = await patch(admin, first, {"name": "second"})
    assert clash.status_code == 409 and clash.json()["code"] == "ai_tool_conflict"
    assert (
        await patch(admin, first, {"name": "FIRST"})
    ).status_code == 200  # its own name, other case
    stored = await rows(stack, "SELECT name FROM ai_tools ORDER BY name")
    assert [r.name for r in stored] == ["FIRST", "Second"]


# --- the cap ----------------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_an_organization_has_at_most_ten_tools(make_stack: MakeStack) -> None:
    stack: Stack = await make_stack("on")
    admin = await person(stack, "admin")
    for n in range(10):
        assert (
            await post(admin, {"name": f"Tool {n}", "url": f"https://t{n}.example.org/"})
        ).status_code == 201
    over = await post(admin, {"name": "Eleventh", "url": "https://t11.example.org/"})
    assert over.status_code == 409 and over.json()["code"] == "ai_tool_limit"
    assert len(await rows(stack, "SELECT 1 FROM ai_tools")) == 10
    # deleting one makes room (control: the refusal was the cap and nothing else)
    first = (await admin.client.get("/api/ai-tools")).json()["items"][0]["id"]
    assert (
        await admin.client.delete(f"/api/ai-tools/{first}", headers=admin.headers)
    ).status_code == 204
    assert (
        await post(admin, {"name": "Eleventh", "url": "https://t11.example.org/"})
    ).status_code == 201


@pytest.mark.anyio
async def test_fifteen_requests_at_once_make_exactly_ten_tools(make_stack: MakeStack) -> None:
    stack: Stack = await make_stack("on")
    admin = await person(stack, "admin")
    answers = await asyncio.gather(
        *(
            post(admin, {"name": f"Race {n}", "url": f"https://r{n}.example.org/"})
            for n in range(15)
        )
    )
    codes = sorted(a.status_code for a in answers)
    assert codes == [201] * 10 + [409] * 5, codes
    assert len(await rows(stack, "SELECT 1 FROM ai_tools")) == 10


# --- the audit trail --------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_every_change_is_audited_without_the_name_or_the_address(
    make_stack: MakeStack,
) -> None:
    stack: Stack = await make_stack("on")
    admin = await person(stack, "admin")
    created = (await post(admin, TOOL)).json()["id"]
    await patch(admin, created, {"url": "https://secret-portal.example.org/x"})
    await patch(admin, created, {"name": "Renamed Assistant", "url": "https://other.example.org/"})
    await admin.client.delete(f"/api/ai-tools/{created}", headers=admin.headers)
    entries = tool_audit(await audit_rows(stack))
    assert [e["action"] for e in entries] == [
        "ai_tool.created",
        "ai_tool.updated",
        "ai_tool.updated",
        "ai_tool.deleted",
    ]
    assert {e["target"] for e in entries} == {f"ai_tool:{created}"}
    assert {e["outcome"] for e in entries} == {"success"}
    assert {e["actor_id"] for e in entries} == {admin.id}
    assert [details_of(e).get("fields") for e in entries] == [None, "url", "name,url", None]
    everything = json.dumps([{k: str(v) for k, v in e.items()} for e in entries])
    for secret in ("secret-portal", "assistant.example.org", "Example Assistant", "Renamed"):
        assert secret not in everything  # neither the address nor the name is ever audited


@pytest.mark.anyio
async def test_a_refused_duplicate_or_a_refused_cap_leaves_a_failure_row(
    make_stack: MakeStack,
) -> None:
    stack: Stack = await make_stack("on")
    admin = await person(stack, "admin")
    await post(admin, TOOL)
    await post(admin, TOOL)  # the same name: refused
    for n in range(9):
        await post(admin, {"name": f"Tool {n}", "url": f"https://t{n}.example.org/"})
    await post(admin, {"name": "Eleventh", "url": "https://t11.example.org/"})  # the cap
    failures = [e for e in tool_audit(await audit_rows(stack)) if e["outcome"] == "failure"]
    assert [details_of(e)["reason"] for e in failures] == ["duplicate", "limit"]
    assert all("example.org" not in json.dumps({k: str(v) for k, v in e.items()}) for e in failures)


@pytest.mark.anyio
async def test_a_refused_request_that_is_not_authorized_leaves_no_tool_audit_row(
    make_stack: MakeStack,
) -> None:
    stack: Stack = await make_stack("on")
    member = await person(stack, "member")
    assert (await post(member, TOOL)).status_code == 403
    assert tool_audit(await audit_rows(stack)) == []


# --- the database is the second line ----------------------------------------------------------------


async def insert_directly(stack: Stack, name: str, url: str, org: uuid.UUID | None = None) -> None:
    async with stack.owner.begin() as connection:
        await connection.execute(
            text("INSERT INTO ai_tools (org_id, name, url) VALUES (:o, :n, :u)"),
            {"o": org or stack.org_a, "n": name, "u": url},
        )


@pytest.mark.anyio
@pytest.mark.parametrize(
    "url",
    [
        "http://chat.example.org/",
        "HTTPS://chat.example.org/",
        "javascript:alert(1)",
        "https://user:pw@chat.example.org/",
        "https://chat.example.org/#frag",
        "https://chat.example.org/a b",
        "https://chat.example.org/\x07",
        "https://chat.example.org/\u202e",
        "https://chat.example.org/\u200b",
        "https://chat.example.org/\ufeff",
        "https://chat.example.org/\u00a0",
        "https://127.0.0.1/",
        "https://2130706433/",
        "https://0x7f.1/",
        "https://[::1]/",
        "https://[v1.x]/",
        "https://foo.127/",
        "https://foo.0x7f/",
        "https://localhost/",
        "https://app.localhost:8443/",
        "https://LocalHost/x",
        "https://",
        "https:///x",
        "https://chat.example.org/" + "a" * 300,
    ],
)
async def test_the_table_itself_refuses_an_address_the_application_would_refuse(
    make_stack: MakeStack, url: str
) -> None:
    stack: Stack = await make_stack("on")
    with pytest.raises(DBAPIError):
        await insert_directly(stack, "Tool", url)
    assert await rows(stack, "SELECT 1 FROM ai_tools") == []


@pytest.mark.anyio
async def test_the_table_accepts_what_the_application_accepts(make_stack: MakeStack) -> None:
    """The positive control of the test above: the same call with a good address goes through."""
    stack: Stack = await make_stack("on")
    # An organization holds ten tools at most (the table's trigger), and there are more good
    # addresses than that: the first ten go to one organization, the rest to the other.
    for n, url in enumerate(GOOD_URLS):
        org = stack.org_a if n < 10 else stack.org_b
        await insert_directly(stack, f"Tool {n}", url, org=org)
    assert len(await rows(stack, "SELECT 1 FROM ai_tools")) == len(GOOD_URLS)


@pytest.mark.anyio
@pytest.mark.parametrize(
    "name", ["", "   ", "x" * 41, "tab\there", "bell\x07", "a\u202eb", "a\u200bb", "a\u00a0b"]
)
async def test_the_table_itself_refuses_a_name_the_application_would_refuse(
    make_stack: MakeStack, name: str
) -> None:
    stack: Stack = await make_stack("on")
    with pytest.raises(DBAPIError):
        await insert_directly(stack, name, TOOL["url"])


@pytest.mark.anyio
async def test_the_table_itself_stops_at_ten_and_at_a_repeated_name(make_stack: MakeStack) -> None:
    stack: Stack = await make_stack("on")
    for n in range(10):
        await insert_directly(stack, f"Tool {n}", f"https://t{n}.example.org/")
    with pytest.raises(DBAPIError):
        await insert_directly(stack, "Eleventh", "https://t11.example.org/")
    with pytest.raises(DBAPIError):
        await insert_directly(stack, "TOOL 3", "https://other.example.org/")
    with pytest.raises(DBAPIError):  # the same name in a decomposed Unicode form
        await insert_directly(stack, "Tool\u0301 3", "https://other.example.org/")
    assert len(await rows(stack, "SELECT 1 FROM ai_tools")) == 10
    # the other organization has its own ten
    await insert_directly(stack, "Tool 0", "https://x.example.org/", org=stack.org_b)


@pytest.mark.anyio
async def test_the_application_role_cannot_change_who_made_a_tool_or_where_it_lives(
    make_stack: MakeStack, app_engine: AsyncEngine
) -> None:
    """The application role may change only name, address and the time of the change: the column
    privileges are checked in test_identity_schema; here the statement itself is refused."""
    stack: Stack = await make_stack("on")
    admin = await person(stack, "admin")
    tool = (await post(admin, TOOL)).json()["id"]
    async with app_engine.begin() as connection:
        await connection.execute(
            text("SELECT set_config('app.org_id', :o, true)"), {"o": str(stack.org_a)}
        )
        for column, value in (("org_id", str(stack.org_b)), ("created_by", str(uuid.uuid4()))):
            nested = await connection.begin_nested()
            with pytest.raises(DBAPIError):
                await connection.execute(
                    text(f"UPDATE ai_tools SET {column} = CAST(:v AS uuid) WHERE id = :i"),
                    {"v": value, "i": uuid.UUID(tool)},
                )
            await nested.rollback()
