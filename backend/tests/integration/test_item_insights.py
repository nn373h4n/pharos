"""Making an insight as the worker role inside row-level security: what is stored, what is refused,
and the shape of the table (ADR-0015)."""

# ruff: noqa: F811  (fixtures are imported by name)

import uuid
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from pharos.llm import insights
from pharos.llm.errors import BudgetExhausted, LLMDisabled, ProviderFailed, SchemaRejected
from pharos.llm.schemas import InsightAnswer
from pharos.llm.types import PublicArticleText, Task

from .ingestion_support import Org, add_item, alpha_beta, worker_engine  # noqa: F401

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

GOOD = InsightAnswer(
    summary="A short plain summary.",
    how_to_apply=["Open the ledger.", "Ask for odd entries."],
    try_prompt="List unusual entries in [LEDGER EXTRACT] and explain each.",
    safety_note="Do not paste real client data.",
)


class Scripted:
    def __init__(self, outcome: InsightAnswer | Exception = GOOD) -> None:
        self.outcome = outcome
        self.calls: list[dict[str, Any]] = []

    async def generate(self, *, task: Task, article: PublicArticleText, **kwargs: Any) -> Any:
        self.calls.append({"task": task, "title": article.title, **kwargs})
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


async def article(engine: AsyncEngine, org: Org, name: str = "a", **columns: Any) -> uuid.UUID:
    async with engine.begin() as connection:
        return await add_item(
            connection, org, url=f"https://news.example.org/{name}", title=name, **columns
        )


async def stored(owner: AsyncEngine, item: uuid.UUID) -> list[Any]:
    async with owner.connect() as connection:
        rows = await connection.execute(
            text(
                "SELECT profession_code, language, prompt_version, status, summary, how_to_apply, "
                "try_prompt, safety_note FROM item_insights WHERE item_id = :i"
            ),
            {"i": item},
        )
        return list(rows)


async def make(
    engine: AsyncEngine,
    gateway: Scripted,
    org: Org,
    item: uuid.UUID,
    profession: str = "accountant",
    language: str = "az",
) -> str:
    return await insights.make_insight(engine, gateway, org.id, item, profession, language)  # type: ignore[arg-type]


async def test_an_insight_is_made_for_the_profession_and_language_and_stored(
    worker_engine: AsyncEngine, owner_engine: AsyncEngine, alpha_beta: tuple[Org, Org]
) -> None:
    alpha, _ = alpha_beta
    item = await article(owner_engine, alpha)
    gateway = Scripted()
    assert await make(worker_engine, gateway, alpha, item) == "ok"
    [call] = gateway.calls
    assert (call["task"], call["profession"], call["language"]) == (
        Task.INSIGHT,
        "accountant",
        "az",
    )
    [row] = await stored(owner_engine, item)
    assert (row.profession_code, row.language, row.prompt_version, row.status) == (
        "accountant",
        "az",
        "v2",
        "ok",
    )
    assert row.how_to_apply == GOOD.how_to_apply and row.try_prompt == GOOD.try_prompt


async def test_an_insight_that_exists_is_not_made_again(
    worker_engine: AsyncEngine, owner_engine: AsyncEngine, alpha_beta: tuple[Org, Org]
) -> None:
    alpha, _ = alpha_beta
    item = await article(owner_engine, alpha)
    await make(worker_engine, Scripted(), alpha, item)
    again = Scripted()
    assert await make(worker_engine, again, alpha, item) == "exists"
    assert again.calls == []
    other = Scripted()  # another language is another key
    assert await make(worker_engine, other, alpha, item, language="en") == "ok"
    assert len(await stored(owner_engine, item)) == 2


@pytest.mark.parametrize(
    "bad",
    [
        InsightAnswer(**{**GOOD.model_dump(), "try_prompt": "Summarize the text above."}),
        InsightAnswer(**{**GOOD.model_dump(), "try_prompt": "Open https://evil.example in [X]"}),
        InsightAnswer(**{**GOOD.model_dump(), "try_prompt": "Send the password in [FORM]"}),
        InsightAnswer(**{**GOOD.model_dump(), "summary": "<b>bold</b>"}),
        SchemaRejected(),
    ],
    ids=["no placeholder", "link", "password", "markup", "never fits"],
)
async def test_an_answer_that_fails_the_checks_is_remembered_as_rejected_without_its_text(
    worker_engine: AsyncEngine,
    owner_engine: AsyncEngine,
    alpha_beta: tuple[Org, Org],
    bad: InsightAnswer | Exception,
) -> None:
    alpha, _ = alpha_beta
    item = await article(owner_engine, alpha)
    assert await make(worker_engine, Scripted(bad), alpha, item) == "rejected"
    [row] = await stored(owner_engine, item)
    assert row.status == "rejected"
    assert (row.summary, row.how_to_apply, row.try_prompt, row.safety_note) == (None,) * 4
    again = Scripted()
    assert await make(worker_engine, again, alpha, item) == "exists"  # not paid for again
    assert again.calls == []


@pytest.mark.parametrize("stop", [BudgetExhausted(), LLMDisabled(), ProviderFailed()])
async def test_a_spent_budget_a_model_that_is_off_or_a_failing_provider_stores_nothing(
    worker_engine: AsyncEngine,
    owner_engine: AsyncEngine,
    alpha_beta: tuple[Org, Org],
    stop: Exception,
) -> None:
    alpha, _ = alpha_beta
    item = await article(owner_engine, alpha)
    with pytest.raises(type(stop)):
        await make(worker_engine, Scripted(stop), alpha, item)
    assert await stored(owner_engine, item) == []  # a later view asks again
    assert await make(worker_engine, Scripted(), alpha, item) == "ok"


@pytest.mark.parametrize("state", ["held", "hidden", "personal", "other_org", "unknown"])
async def test_only_a_current_public_article_of_the_organization_is_read(
    worker_engine: AsyncEngine,
    owner_engine: AsyncEngine,
    alpha_beta: tuple[Org, Org],
    state: str,
) -> None:
    alpha, beta = alpha_beta
    if state == "unknown":
        item = uuid.uuid4()
    elif state == "other_org":
        item = await article(owner_engine, beta)
    elif state == "personal":
        item = await article(owner_engine, alpha, owner=alpha.user)
    elif state == "held":
        item = await article(owner_engine, alpha, status="held", hold_reason="editor_hold")
    else:
        item = await article(owner_engine, alpha, status="hidden")
    gateway = Scripted()
    assert await make(worker_engine, gateway, alpha, item) == "skipped"
    assert gateway.calls == []


@pytest.mark.parametrize(
    ("profession", "language"),
    [("not_a_profession", "az"), ("Accountant", "az"), ("accountant", "fr"), ("accountant", "")],
)
async def test_an_unknown_profession_or_language_is_skipped(
    worker_engine: AsyncEngine,
    owner_engine: AsyncEngine,
    alpha_beta: tuple[Org, Org],
    profession: str,
    language: str,
) -> None:
    alpha, _ = alpha_beta
    item = await article(owner_engine, alpha)
    gateway = Scripted()
    assert await make(worker_engine, gateway, alpha, item, profession, language) == "skipped"
    assert gateway.calls == []


# --- the table ------------------------------------------------------------------------------------------------


async def worker_sql(engine: AsyncEngine, org: Org, sql: str, **params: Any) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            text("SELECT set_config('app.org_id', :o, true)"), {"o": str(org.id)}
        )
        await connection.execute(text(sql), params)


async def count_rows(owner: AsyncEngine, item: uuid.UUID) -> int:
    return len(await stored(owner, item))


async def test_the_worker_can_add_a_row_and_read_it_but_never_change_one(
    worker_engine: AsyncEngine, owner_engine: AsyncEngine, alpha_beta: tuple[Org, Org]
) -> None:
    alpha, _ = alpha_beta
    item = await article(owner_engine, alpha)
    await make(worker_engine, Scripted(), alpha, item)
    for statement in (
        "UPDATE item_insights SET try_prompt = 'changed [X]' WHERE item_id = :i",
        "UPDATE item_insights SET status = 'rejected' WHERE item_id = :i",
    ):
        with pytest.raises(DBAPIError, match="permission denied"):
            await worker_sql(worker_engine, alpha, statement, i=item)
    assert (await stored(owner_engine, item))[0].status == "ok"


async def test_the_worker_may_delete_a_rejected_answer_only_when_it_is_a_day_old(
    worker_engine: AsyncEngine, owner_engine: AsyncEngine, alpha_beta: tuple[Org, Org]
) -> None:
    alpha, _ = alpha_beta
    fresh_ok = await article(owner_engine, alpha, "ok")
    fresh_no = await article(owner_engine, alpha, "fresh")
    old_no = await article(owner_engine, alpha, "old")
    old_ok = await article(owner_engine, alpha, "old-ok")
    await make(worker_engine, Scripted(), alpha, fresh_ok)
    await make(worker_engine, Scripted(SchemaRejected()), alpha, fresh_no)
    await make(worker_engine, Scripted(SchemaRejected()), alpha, old_no)
    await make(worker_engine, Scripted(), alpha, old_ok)
    async with owner_engine.begin() as connection:
        await connection.execute(
            text(
                "UPDATE item_insights SET created_at = now() - interval '2 days' "
                "WHERE item_id = ANY(:ids)"
            ),
            {"ids": [old_no, old_ok]},
        )
    for item in (fresh_ok, fresh_no, old_no, old_ok):
        await worker_sql(
            worker_engine, alpha, "DELETE FROM item_insights WHERE item_id = :i", i=item
        )
    assert [await count_rows(owner_engine, i) for i in (fresh_ok, fresh_no, old_no, old_ok)] == [
        1,
        1,
        0,  # the only one that may go: rejected and a day old
        1,
    ]


async def test_a_rejection_is_forgotten_after_a_day_and_the_insight_is_tried_again(
    worker_engine: AsyncEngine, owner_engine: AsyncEngine, alpha_beta: tuple[Org, Org]
) -> None:
    alpha, _ = alpha_beta
    item = await article(owner_engine, alpha)
    assert await make(worker_engine, Scripted(SchemaRejected()), alpha, item) == "rejected"
    soon = Scripted()
    assert await make(worker_engine, soon, alpha, item) == "exists"
    assert soon.calls == []
    async with owner_engine.begin() as connection:
        await connection.execute(
            text("UPDATE item_insights SET created_at = now() - interval '25 hours'")
        )
    later = Scripted()
    assert await make(worker_engine, later, alpha, item) == "ok"
    assert len(later.calls) == 1
    [row] = await stored(owner_engine, item)
    assert row.status == "ok"


async def test_a_worker_clean_up_of_old_articles_is_not_blocked_by_their_insights(
    worker_engine: AsyncEngine, owner_engine: AsyncEngine, alpha_beta: tuple[Org, Org]
) -> None:
    alpha, _ = alpha_beta
    item = await article(owner_engine, alpha)
    await make(worker_engine, Scripted(), alpha, item)
    await worker_sql(worker_engine, alpha, "DELETE FROM items WHERE id = :i", i=item)
    assert await count_rows(owner_engine, item) == 0


@pytest.mark.parametrize(
    "values",
    [
        "'ok', NULL, NULL, NULL, NULL",  # ok with nothing in it
        "'ok', 'x', '[]'::jsonb, 'p [X]', 'n'",  # no steps
        "'ok', 'x', '[\"a\",\"b\",\"c\",\"d\"]'::jsonb, 'p [X]', 'n'",  # four steps
        "'ok', 'x', '{\"a\": 1}'::jsonb, 'p [X]', 'n'",  # not a list
        "'rejected', 'x', NULL, NULL, NULL",  # rejected with content
        "'maybe', NULL, NULL, NULL, NULL",  # a status nobody defined
    ],
)
async def test_the_database_refuses_a_half_written_insight(
    worker_engine: AsyncEngine,
    owner_engine: AsyncEngine,
    alpha_beta: tuple[Org, Org],
    values: str,
) -> None:
    alpha, _ = alpha_beta
    item = await article(owner_engine, alpha)
    with pytest.raises(DBAPIError, match="check constraint"):
        await worker_sql(
            worker_engine,
            alpha,
            "INSERT INTO item_insights (org_id, item_id, profession_code, language, "
            "prompt_version, status, summary, how_to_apply, try_prompt, safety_note) "
            f"VALUES (:o, :i, 'accountant', 'en', 'v2', {values})",
            o=alpha.id,
            i=item,
        )


async def test_rows_belong_to_one_organization_and_follow_their_article(
    worker_engine: AsyncEngine, owner_engine: AsyncEngine, alpha_beta: tuple[Org, Org]
) -> None:
    alpha, beta = alpha_beta
    item = await article(owner_engine, alpha)
    await make(worker_engine, Scripted(), alpha, item)
    async with worker_engine.begin() as connection:
        await connection.execute(
            text("SELECT set_config('app.org_id', :o, true)"), {"o": str(beta.id)}
        )
        count = (await connection.execute(text("SELECT count(*) FROM item_insights"))).scalar_one()
    assert count == 0
    with pytest.raises(DBAPIError):  # a row for another organization's article
        await worker_sql(
            worker_engine,
            beta,
            "INSERT INTO item_insights (org_id, item_id, profession_code, language, "
            "prompt_version, status) VALUES (:o, :i, 'accountant', 'en', 'v1', 'rejected')",
            o=beta.id,
            i=item,
        )
    async with owner_engine.begin() as connection:
        await connection.execute(text("DELETE FROM items WHERE id = :i"), {"i": item})
    assert await stored(owner_engine, item) == []  # removed with its article
