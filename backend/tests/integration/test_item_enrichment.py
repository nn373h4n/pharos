"""Reading public articles with a model: what is stored, what is held, what stops a sweep, and the one
narrow right the worker has over an article's state (ADR-0017). The gateway is a scripted stand-in;
the SQL runs for real as the worker role inside row-level security."""

# ruff: noqa: F811  (fixtures are imported by name)

import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from pharos.llm import enrichment
from pharos.llm.errors import (
    BudgetExhausted,
    LLMDisabled,
    ProviderFailed,
    SchemaRejected,
    StagePaused,
)
from pharos.llm.schemas import EnrichAnswer, FilterAnswer, ProfessionScore
from pharos.llm.types import PublicArticleText, Task

from .ingestion_support import Org, add_item, alpha_beta, worker_engine  # noqa: F401

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

NOW = datetime(2026, 10, 31, 12, 0, tzinfo=UTC)


def good(**changes: Any) -> EnrichAnswer:
    values: dict[str, Any] = {
        "summary": "A short plain summary.",
        "category": "tool",
        "quality": 4,
        "professions": [ProfessionScore(code="accountant", relevance=80)],
    }
    values.update(changes)
    return EnrichAnswer(**values)


class Scripted:
    """Answers the two tasks from a script and remembers every call."""

    def __init__(
        self,
        *,
        relevant: bool = True,
        confidence: int = 90,
        answer: EnrichAnswer | Exception | None = None,
        before_enrich: Callable[[PublicArticleText], Awaitable[None]] | None = None,
    ) -> None:
        self.relevant, self.confidence = relevant, confidence
        self.answer = answer if answer is not None else good()
        self.before_enrich = before_enrich
        self.calls: list[tuple[Task, str, dict[str, Any]]] = []

    async def generate(self, *, task: Task, article: PublicArticleText, **kwargs: Any) -> Any:
        self.calls.append((task, article.title, kwargs))
        if task is Task.FILTER_RELEVANCE:
            if isinstance(self.relevant, Exception):
                raise self.relevant
            return FilterAnswer(relevant=self.relevant, confidence=self.confidence)
        if self.before_enrich is not None:
            await self.before_enrich(article)
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


async def run(engine: AsyncEngine, org: Org, gateway: Scripted, batch: int = 20) -> Any:
    return await enrichment.sweep(engine, gateway, org.id, batch=batch, now=lambda: NOW)  # type: ignore[arg-type]


async def article(engine: AsyncEngine, org: Org, name: str, **columns: Any) -> uuid.UUID:
    async with engine.begin() as connection:
        return await add_item(
            connection, org, url=f"https://news.example.org/{name}", title=name, **columns
        )


async def row(owner: AsyncEngine, item: uuid.UUID) -> Any:
    async with owner.connect() as connection:
        return (
            await connection.execute(
                text(
                    "SELECT status, hold_reason, summary, category, quality, enriched_at, "
                    "enrichment_version FROM items WHERE id = :id"
                ),
                {"id": item},
            )
        ).one()


async def professions_of(owner: AsyncEngine, item: uuid.UUID) -> dict[str, int]:
    async with owner.connect() as connection:
        rows = await connection.execute(
            text("SELECT profession_code, relevance FROM item_professions WHERE item_id = :i"),
            {"i": item},
        )
        return {r.profession_code: r.relevance for r in rows}


# --- what is stored ------------------------------------------------------------------------------------


async def test_a_public_article_is_read_and_its_answer_is_stored(
    worker_engine: AsyncEngine, owner_engine: AsyncEngine, alpha_beta: tuple[Org, Org]
) -> None:
    alpha, _ = alpha_beta
    item = await article(owner_engine, alpha, "one", lang="az")
    gateway = Scripted(
        answer=good(
            professions=[
                ProfessionScore(code="accountant", relevance=60),
                ProfessionScore(code="accountant", relevance=85),  # repeated: the best one stays
                ProfessionScore(code="not_a_profession", relevance=99),  # unknown: dropped
                ProfessionScore(code="sap_basis_administrator", relevance=10),
            ]
        )
    )
    done = await run(worker_engine, alpha, gateway)
    assert (done.read, done.held, done.stopped) == (1, 0, None)
    stored = await row(owner_engine, item)
    assert (stored.status, stored.hold_reason) == ("new", None)
    assert (stored.summary, stored.category, stored.quality) == (
        "A short plain summary.",
        "tool",
        4,
    )
    assert stored.enriched_at == NOW and stored.enrichment_version == "v1"
    assert await professions_of(owner_engine, item) == {
        "accountant": 85,
        "sap_basis_administrator": 10,
    }
    [(_, _, filter_args), (task, _, enrich_args)] = gateway.calls
    assert task is Task.ENRICH and enrich_args["language"] == "az"
    assert "accountant" in enrich_args["professions"]  # the catalogue, from the database
    assert "sap_basis_administrator" in enrich_args["professions"]
    assert filter_args == {"org_id": alpha.id, "schema": FilterAnswer}


async def test_the_language_of_the_answer_falls_back_to_english(
    worker_engine: AsyncEngine, owner_engine: AsyncEngine, alpha_beta: tuple[Org, Org]
) -> None:
    alpha, _ = alpha_beta
    await article(owner_engine, alpha, "fr", lang="fr")
    gateway = Scripted()
    await run(worker_engine, alpha, gateway)
    assert gateway.calls[1][2]["language"] == "en"


async def test_an_article_is_read_once(
    worker_engine: AsyncEngine, owner_engine: AsyncEngine, alpha_beta: tuple[Org, Org]
) -> None:
    alpha, _ = alpha_beta
    await article(owner_engine, alpha, "once")
    first, second = Scripted(), Scripted()
    await run(worker_engine, alpha, first)
    done = await run(worker_engine, alpha, second)
    assert len(first.calls) == 2 and second.calls == [] and done.read == 0


async def test_only_public_new_articles_of_the_organization_are_sent(
    worker_engine: AsyncEngine, owner_engine: AsyncEngine, alpha_beta: tuple[Org, Org]
) -> None:
    alpha, beta = alpha_beta
    await article(owner_engine, alpha, "mine", owner=alpha.user)  # a person's own feed
    await article(owner_engine, alpha, "held", status="held", hold_reason="editor_hold")
    await article(owner_engine, alpha, "hidden", status="hidden")
    await article(owner_engine, beta, "theirs")
    shared = await article(owner_engine, alpha, "shared")
    gateway = Scripted()
    await run(worker_engine, alpha, gateway)
    assert {title for _, title, _ in gateway.calls} == {"shared"}
    assert (await row(owner_engine, shared)).summary is not None


async def test_a_sweep_reads_at_most_a_batch_oldest_first(
    worker_engine: AsyncEngine, owner_engine: AsyncEngine, alpha_beta: tuple[Org, Org]
) -> None:
    alpha, _ = alpha_beta
    for n in range(5):
        await article(owner_engine, alpha, f"n{n}")
    gateway = Scripted()
    done = await run(worker_engine, alpha, gateway, batch=2)
    assert done.read == 2
    assert [t for task, t, _ in gateway.calls if task is Task.ENRICH] == ["n0", "n1"]


# --- what is held ----------------------------------------------------------------------------------------


async def test_an_article_the_small_model_is_sure_is_off_topic_is_held_without_the_strong_model(
    worker_engine: AsyncEngine, owner_engine: AsyncEngine, alpha_beta: tuple[Org, Org]
) -> None:
    alpha, _ = alpha_beta
    item = await article(owner_engine, alpha, "sports")
    gateway = Scripted(relevant=False, confidence=90)
    done = await run(worker_engine, alpha, gateway)
    assert (done.read, done.held) == (1, 1)
    assert [task for task, _, _ in gateway.calls] == [Task.FILTER_RELEVANCE]
    stored = await row(owner_engine, item)
    assert (stored.status, stored.hold_reason) == ("held", "low_quality")
    assert stored.summary is None and stored.enriched_at == NOW


async def test_an_unsure_no_goes_on_to_the_strong_model(
    worker_engine: AsyncEngine, owner_engine: AsyncEngine, alpha_beta: tuple[Org, Org]
) -> None:
    alpha, _ = alpha_beta
    item = await article(owner_engine, alpha, "maybe")
    gateway = Scripted(relevant=False, confidence=40)
    await run(worker_engine, alpha, gateway)
    assert [task for task, _, _ in gateway.calls] == [Task.FILTER_RELEVANCE, Task.ENRICH]
    assert (await row(owner_engine, item)).status == "new"


@pytest.mark.parametrize(("quality", "held"), [(1, True), (2, True), (3, False), (5, False)])
async def test_a_poor_article_is_held_but_keeps_what_the_model_said(
    worker_engine: AsyncEngine,
    owner_engine: AsyncEngine,
    alpha_beta: tuple[Org, Org],
    quality: int,
    held: bool,
) -> None:
    alpha, _ = alpha_beta
    item = await article(owner_engine, alpha, f"q{quality}")
    await run(worker_engine, alpha, Scripted(answer=good(quality=quality)))
    stored = await row(owner_engine, item)
    assert stored.status == ("held" if held else "new")
    assert stored.hold_reason == ("low_quality" if held else None)
    assert stored.summary and stored.quality == quality
    assert await professions_of(owner_engine, item) == {"accountant": 80}


@pytest.mark.parametrize(
    "summary",
    [
        "See https://evil.example/x for more.",
        "Open www.evil.example today",
        "Go to evil.example/login now",
        "<b>bold</b> claim",
        "[click](http://x.example)",
        "javascript:alert(1)",
        "write to mailto:a@evil.example",
        "bell \x07 inside",
    ],
)
async def test_an_answer_with_a_link_or_markup_is_never_stored(
    worker_engine: AsyncEngine, owner_engine: AsyncEngine, alpha_beta: tuple[Org, Org], summary: str
) -> None:
    alpha, _ = alpha_beta
    item = await article(owner_engine, alpha, "bait")
    await run(worker_engine, alpha, Scripted(answer=good(summary=summary)))
    stored = await row(owner_engine, item)
    assert (stored.status, stored.hold_reason) == ("held", "low_quality")
    assert stored.summary is None and stored.enriched_at == NOW
    assert await professions_of(owner_engine, item) == {}


async def test_an_answer_that_never_fits_the_schema_holds_the_article_instead_of_looping(
    worker_engine: AsyncEngine, owner_engine: AsyncEngine, alpha_beta: tuple[Org, Org]
) -> None:
    alpha, _ = alpha_beta
    item = await article(owner_engine, alpha, "garbled")
    await run(worker_engine, alpha, Scripted(answer=SchemaRejected()))
    stored = await row(owner_engine, item)
    assert (stored.status, stored.hold_reason, stored.summary) == ("held", "low_quality", None)
    again = Scripted()
    await run(worker_engine, alpha, again)
    assert again.calls == []  # not tried again


# --- what stops a sweep ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "stop", [BudgetExhausted(), StagePaused(), LLMDisabled(), ProviderFailed()]
)
async def test_a_budget_a_rest_or_a_failing_provider_ends_the_sweep_and_leaves_the_rest(
    worker_engine: AsyncEngine,
    owner_engine: AsyncEngine,
    alpha_beta: tuple[Org, Org],
    stop: Exception,
) -> None:
    alpha, _ = alpha_beta
    first = await article(owner_engine, alpha, "a")
    second = await article(owner_engine, alpha, "b")
    gateway = Scripted(answer=stop)
    done = await run(worker_engine, alpha, gateway)
    assert done.stopped is not None and done.read == 0
    assert len([c for c in gateway.calls if c[0] is Task.ENRICH]) == 1  # it did not go on
    for item in (first, second):
        stored = await row(owner_engine, item)
        assert (stored.status, stored.enriched_at) == ("new", None)  # still waiting
    ok = Scripted()
    assert (await run(worker_engine, alpha, ok)).read == 2  # and read later


async def test_a_decision_made_meanwhile_stands(
    worker_engine: AsyncEngine, owner_engine: AsyncEngine, alpha_beta: tuple[Org, Org]
) -> None:
    alpha, _ = alpha_beta
    item = await article(owner_engine, alpha, "raced")

    async def editor_hides_it(_: PublicArticleText) -> None:
        async with owner_engine.begin() as connection:
            await connection.execute(
                text("UPDATE items SET status = 'hidden' WHERE id = :id"), {"id": item}
            )

    done = await run(worker_engine, alpha, Scripted(before_enrich=editor_hides_it))
    assert (done.read, done.skipped) == (0, 1)
    stored = await row(owner_engine, item)
    assert (stored.status, stored.summary, stored.enriched_at) == ("hidden", None, None)
    assert await professions_of(owner_engine, item) == {}


# --- the worker's narrow right over an article's state ---------------------------------------------------------


async def worker_sql(engine: AsyncEngine, org: Org, sql: str, **params: Any) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            text("SELECT set_config('app.org_id', :o, true)"), {"o": str(org.id)}
        )
        await connection.execute(text(sql), params)


async def test_the_worker_can_hold_a_new_article_as_low_quality(
    worker_engine: AsyncEngine, owner_engine: AsyncEngine, alpha_beta: tuple[Org, Org]
) -> None:
    alpha, _ = alpha_beta
    item = await article(owner_engine, alpha, "ok")
    await worker_sql(
        worker_engine,
        alpha,
        "UPDATE items SET status = 'held', hold_reason = 'low_quality' WHERE id = :id",
        id=item,
    )
    assert (await row(owner_engine, item)).status == "held"


@pytest.mark.parametrize(
    ("before", "reason", "after"),
    [
        ("new", None, "UPDATE items SET status = 'hidden' WHERE id = :id"),
        ("new", None, "UPDATE items SET status = 'held', hold_reason = 'editor_hold' WHERE id = :id"),
        ("new", None, "UPDATE items SET status = 'held', hold_reason = 'instruction_phrase' WHERE id = :id"),
        ("held", "low_quality", "UPDATE items SET status = 'new', hold_reason = NULL WHERE id = :id"),
        ("held", "editor_hold", "UPDATE items SET status = 'new', hold_reason = NULL WHERE id = :id"),
        ("held", "editor_hold", "UPDATE items SET status = 'hidden', hold_reason = NULL WHERE id = :id"),
        ("held", "editor_hold", "UPDATE items SET hold_reason = 'low_quality' WHERE id = :id"),
        ("hidden", None, "UPDATE items SET status = 'new' WHERE id = :id"),
        ("hidden", None, "UPDATE items SET status = 'held', hold_reason = 'low_quality' WHERE id = :id"),
    ],
)  # fmt: skip
async def test_the_worker_can_do_nothing_else_to_an_articles_state(
    worker_engine: AsyncEngine,
    owner_engine: AsyncEngine,
    alpha_beta: tuple[Org, Org],
    before: str,
    reason: str | None,
    after: str,
) -> None:
    alpha, _ = alpha_beta
    columns: dict[str, Any] = {"status": before}
    if reason:
        columns["hold_reason"] = reason
    item = await article(owner_engine, alpha, f"{before}-{abs(hash(after))}", **columns)
    with pytest.raises(DBAPIError, match="may only hold"):
        await worker_sql(worker_engine, alpha, after, id=item)
    assert (await row(owner_engine, item)).status == before


async def test_the_text_of_a_held_article_can_still_be_updated_by_a_fetch(
    worker_engine: AsyncEngine, owner_engine: AsyncEngine, alpha_beta: tuple[Org, Org]
) -> None:
    """The trigger watches the state, not the row: ingestion keeps working on held articles."""
    alpha, _ = alpha_beta
    item = await article(owner_engine, alpha, "held-text", status="held", hold_reason="editor_hold")
    await worker_sql(
        worker_engine, alpha, "UPDATE items SET title = 'changed' WHERE id = :id", id=item
    )


# --- the shape of what is stored ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "change",
    [
        "summary = 'x', category = 'tool', quality = 3",  # a summary without time and version
        "enriched_at = now()",  # a time without a version
        "summary = 'x', category = 'tool', enriched_at = now(), enrichment_version = 'v1'",
        "summary = 'x', category = 'bogus', quality = 3, enriched_at = now(), enrichment_version = 'v1'",
        "summary = 'x', category = 'tool', quality = 6, enriched_at = now(), enrichment_version = 'v1'",
        "summary = '', category = 'tool', quality = 3, enriched_at = now(), enrichment_version = 'v1'",
    ],
)  # fmt: skip
async def test_the_database_refuses_a_half_written_or_odd_enrichment(
    owner_engine: AsyncEngine, alpha_beta: tuple[Org, Org], change: str
) -> None:
    alpha, _ = alpha_beta
    item = await article(owner_engine, alpha, f"shape-{abs(hash(change))}")
    async with owner_engine.begin() as connection:
        with pytest.raises(DBAPIError, match="check constraint"):
            await connection.execute(
                text(f"UPDATE items SET {change} WHERE id = :id"), {"id": item}
            )


async def test_profession_rows_belong_to_one_organization_and_cannot_dangle(
    worker_engine: AsyncEngine, owner_engine: AsyncEngine, alpha_beta: tuple[Org, Org]
) -> None:
    alpha, beta = alpha_beta
    item = await article(owner_engine, alpha, "scored")
    await run(worker_engine, alpha, Scripted())
    assert await professions_of(owner_engine, item) == {"accountant": 80}
    # Seen from the other organization, the rows are not there.
    async with worker_engine.begin() as connection:
        await connection.execute(
            text("SELECT set_config('app.org_id', :o, true)"), {"o": str(beta.id)}
        )
        count = (
            await connection.execute(text("SELECT count(*) FROM item_professions"))
        ).scalar_one()
    assert count == 0
    # A row for an article of another organization is refused.
    with pytest.raises(DBAPIError):
        await worker_sql(
            worker_engine,
            beta,
            "INSERT INTO item_professions (org_id, item_id, profession_code, relevance) "
            "VALUES (:o, :i, 'accountant', 5)",
            o=beta.id,
            i=item,
        )
    # Deleting the article takes its rows with it.
    async with owner_engine.begin() as connection:
        await connection.execute(text("DELETE FROM items WHERE id = :id"), {"id": item})
    assert await professions_of(owner_engine, item) == {}


async def test_articles_that_are_not_public_do_not_use_up_the_batch(
    worker_engine: AsyncEngine, owner_engine: AsyncEngine, alpha_beta: tuple[Org, Org]
) -> None:
    """The selection filters by itself (the loader would refuse them anyway); otherwise a pile of
    held or personal articles older than the rest would starve the sweep."""
    alpha, _ = alpha_beta
    await article(owner_engine, alpha, "mine", owner=alpha.user)
    await article(owner_engine, alpha, "held", status="held", hold_reason="editor_hold")
    await article(owner_engine, alpha, "hidden", status="hidden")
    shared = await article(owner_engine, alpha, "shared")
    done = await run(worker_engine, alpha, Scripted(), batch=1)
    assert (done.read, done.skipped) == (1, 0)
    assert (await row(owner_engine, shared)).summary is not None


# --- what the review of M4-04a found ----------------------------------------------------------------------------


async def test_an_article_a_person_has_decided_about_is_never_read_or_held_by_the_model(
    worker_engine: AsyncEngine, owner_engine: AsyncEngine, alpha_beta: tuple[Org, Org]
) -> None:
    """An editor approved a held article: it is `new` again with no reading yet. The model must not
    hold it again (it would undo the editor)."""
    alpha, _ = alpha_beta
    item = await article(owner_engine, alpha, "approved", reviewed_at=NOW)
    gateway = Scripted(answer=good(quality=1))
    done = await run(worker_engine, alpha, gateway)
    assert gateway.calls == [] and done.read == 0
    assert (await row(owner_engine, item)).status == "new"


async def test_a_decision_made_during_the_call_stands_even_if_the_row_is_new_again(
    worker_engine: AsyncEngine, owner_engine: AsyncEngine, alpha_beta: tuple[Org, Org]
) -> None:
    alpha, _ = alpha_beta
    item = await article(owner_engine, alpha, "approved-meanwhile")

    async def editor_decides(_: PublicArticleText) -> None:
        async with owner_engine.begin() as connection:
            await connection.execute(
                text("UPDATE items SET reviewed_at = now() WHERE id = :id"), {"id": item}
            )

    done = await run(
        worker_engine, alpha, Scripted(answer=good(quality=1), before_enrich=editor_decides)
    )
    assert done.skipped == 1
    stored = await row(owner_engine, item)
    assert (stored.status, stored.summary, stored.enriched_at) == ("new", None, None)


@pytest.mark.parametrize(
    "setup",
    [
        {"status": "held", "hold_reason": "editor_hold"},
        {"status": "hidden"},
        {"reviewed_at": NOW},
        {"summary": "x", "category": "tool", "quality": 3, "enriched_at": NOW, "enrichment_version": "v1"},
        {"owner": "person"},
    ],
    ids=["held", "hidden", "reviewed", "already read", "personal"],
)  # fmt: skip
async def test_the_worker_cannot_write_an_answer_onto_an_article_it_may_not_read(
    worker_engine: AsyncEngine,
    owner_engine: AsyncEngine,
    alpha_beta: tuple[Org, Org],
    setup: dict[str, Any],
) -> None:
    alpha, _ = alpha_beta
    columns = dict(setup)
    if columns.pop("owner", None):
        columns["owner"] = alpha.user
    item = await article(owner_engine, alpha, f"closed-{len(setup)}-{sorted(setup)[0]}", **columns)
    with pytest.raises(DBAPIError, match="may only hold"):
        await worker_sql(
            worker_engine,
            alpha,
            "UPDATE items SET summary = 'forged', category = 'tool', quality = 5, "
            "enriched_at = now(), enrichment_version = 'v1' WHERE id = :id",
            id=item,
        )


async def test_a_text_the_provider_refuses_does_not_stop_the_articles_after_it(
    worker_engine: AsyncEngine, owner_engine: AsyncEngine, alpha_beta: tuple[Org, Org]
) -> None:
    from pharos.llm.errors import ProviderRejected

    alpha, _ = alpha_beta
    bad = await article(owner_engine, alpha, "refused")
    fine = await article(owner_engine, alpha, "fine")

    class Picky(Scripted):
        async def generate(self, *, task: Task, article: PublicArticleText, **kwargs: Any) -> Any:
            if article.title == "refused":
                raise ProviderRejected()
            return await super().generate(task=task, article=article, **kwargs)

    done = await run(worker_engine, alpha, Picky())
    assert (done.read, done.failed, done.stopped) == (1, 1, None)
    assert (await row(owner_engine, fine)).summary is not None
    # The key and the provider work (the other article was read), so the refused one is the
    # problem: it is held for an editor instead of being tried every minute.
    stored = await row(owner_engine, bad)
    assert (stored.status, stored.hold_reason) == ("held", "low_quality")
    assert done.held == 1


async def test_a_provider_that_refuses_everything_ends_the_sweep_and_holds_nothing(
    worker_engine: AsyncEngine, owner_engine: AsyncEngine, alpha_beta: tuple[Org, Org]
) -> None:
    from pharos.llm.errors import ProviderRejected

    alpha, _ = alpha_beta
    ids = [await article(owner_engine, alpha, f"r{n}") for n in range(5)]
    done = await run(worker_engine, alpha, Scripted(relevant=ProviderRejected()))  # type: ignore[arg-type]
    assert done.stopped == "repeated_failures" and done.read == 0 and done.failed == 3
    for item in ids:  # a wrong key must not hold the whole feed
        assert (await row(owner_engine, item)).status == "new"


async def test_a_bug_on_one_article_does_not_stop_the_others(
    worker_engine: AsyncEngine, owner_engine: AsyncEngine, alpha_beta: tuple[Org, Org]
) -> None:
    alpha, _ = alpha_beta
    await article(owner_engine, alpha, "boom")
    fine = await article(owner_engine, alpha, "fine2")

    class Buggy(Scripted):
        async def generate(self, *, task: Task, article: PublicArticleText, **kwargs: Any) -> Any:
            if article.title == "boom":
                raise KeyError("a bug")
            return await super().generate(task=task, article=article, **kwargs)

    done = await run(worker_engine, alpha, Buggy())
    assert (done.read, done.failed) == (1, 1)
    assert (await row(owner_engine, fine)).summary is not None


async def test_the_profession_list_is_the_organizations_own_and_the_public_ones(
    worker_engine: AsyncEngine, owner_engine: AsyncEngine, alpha_beta: tuple[Org, Org]
) -> None:
    alpha, beta = alpha_beta
    async with owner_engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO professions (scope, owner_org_id, code, names) VALUES "
                "('org', :o, 'beta_only_role', CAST(:n AS jsonb))"
            ),
            {"o": beta.id, "n": '{"en": "B", "az": "B", "tr": "B", "ru": "B"}'},
        )
    async with worker_engine.begin() as connection:
        mine = await enrichment.profession_codes(connection, alpha.id)
        theirs = await enrichment.profession_codes(connection, beta.id)
    assert "accountant" in mine and "beta_only_role" not in mine
    # As the table owner, which row-level security does not apply to: the query's own filter.
    async with owner_engine.begin() as connection:
        own = await enrichment.profession_codes(connection, alpha.id)
    assert "accountant" in own and "beta_only_role" not in own
    assert "beta_only_role" in theirs and "accountant" in theirs
