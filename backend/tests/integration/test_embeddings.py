"""Embeddings and clusters, in the database (M4-05, ADR-0007, ADR-0021): the table's own rules, who
may touch it, and the sweep that embeds and clusters, run as the worker role inside row-level
security with the demo embedder."""

# ruff: noqa: F811  (fixtures are imported by name)

import asyncio
import math
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from pharos.llm import clustering
from pharos.llm.embedding import DemoEmbedder, Embedded
from pharos.llm.errors import NotPublicArticle, ProviderFailed
from pharos.llm.types import PublicArticleText, mint_public_text

from .ingestion_support import Org, add_item, alpha_beta, worker_engine  # noqa: F401

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

MODEL = "test-model"
STORY = ("Acme ships Widget 2 with faster sync", "Finance teams get a quicker sync in Widget 2")
SAME = ("Faster sync: Acme ships Widget 2", "Widget 2 from Acme brings a quicker sync to finance")
OTHER = ("City council approves bicycle lane budget", "The vote on the new lanes passed last night")


def read_columns(**changes: Any) -> dict[str, Any]:
    """The columns of an article the model has read (they must come together)."""
    return {
        "summary": "S",
        "category": "news",
        "quality": 3,
        "enriched_at": datetime.now(UTC),
        "enrichment_version": "v1",
        **changes,
    }


class Gateway:
    """What the sweep needs from the gateway: the demo embedder, as the real one would call it."""

    def __init__(self, fail: BaseException | None = None) -> None:
        self.fail = fail
        self.calls = 0
        self._embedder = DemoEmbedder()

    async def embed(self, *, org_id: uuid.UUID, article: PublicArticleText) -> Embedded:
        self.calls += 1
        if self.fail is not None:
            raise self.fail
        from pharos.llm.embedding import check_vector, embedding_text

        answer = await self._embedder.embed(
            model=MODEL, texts=[embedding_text(article)], deadline_seconds=1
        )
        return Embedded(MODEL, check_vector(answer))


async def story(
    owner_engine: AsyncEngine, org: Org, name: str, words: tuple[str, str], **columns: Any
) -> uuid.UUID:
    async with owner_engine.begin() as connection:
        return await add_item(
            connection,
            org,
            url=f"https://news.example.org/{name}",
            title=words[0],
            excerpt=words[1],
            **read_columns(**columns),
        )


async def run(
    worker_engine: AsyncEngine, org: Org, gateway: Any = None, *, threshold: float = 0.6
) -> clustering.SweepResult:
    return await clustering.sweep(
        worker_engine,
        gateway or Gateway(),
        org.id,
        model=MODEL,
        batch=50,
        threshold=threshold,
        window_days=14,
    )


async def clusters(owner_engine: AsyncEngine, org: Org) -> dict[uuid.UUID, uuid.UUID | None]:
    async with owner_engine.connect() as connection:
        rows = await connection.execute(
            text("SELECT id, cluster_id FROM items WHERE org_id = :o"), {"o": org.id}
        )
        return {row.id: row.cluster_id for row in rows}


# --- the sweep ---------------------------------------------------------------------------------------


async def test_a_rewritten_copy_joins_the_cluster_and_another_story_stands_alone(
    worker_engine: AsyncEngine, owner_engine: AsyncEngine, alpha_beta: tuple[Org, Org]
) -> None:
    alpha, _ = alpha_beta
    a = await story(owner_engine, alpha, "a", STORY)
    b = await story(owner_engine, alpha, "b", SAME)
    c = await story(owner_engine, alpha, "c", OTHER)
    done = await run(worker_engine, alpha)
    assert (done.embedded, done.clustered, done.failed) == (3, 1, 0)
    found = await clusters(owner_engine, alpha)
    assert found[a] is not None and found[a] == found[b] and found[c] is None


async def test_a_second_sweep_does_nothing_and_a_third_article_joins_the_same_cluster(
    worker_engine: AsyncEngine, owner_engine: AsyncEngine, alpha_beta: tuple[Org, Org]
) -> None:
    alpha, _ = alpha_beta
    a = await story(owner_engine, alpha, "a", STORY)
    b = await story(owner_engine, alpha, "b", SAME)
    await run(worker_engine, alpha)
    before = await clusters(owner_engine, alpha)
    gateway = Gateway()
    again = await run(worker_engine, alpha, gateway)
    assert again.embedded == 0 and gateway.calls == 0  # nothing left to embed
    c = await story(owner_engine, alpha, "c", ("Widget 2 by Acme: faster sync", "Quicker sync"))
    await run(worker_engine, alpha)
    found = await clusters(owner_engine, alpha)
    assert found[a] == found[b] == found[c] == before[a]  # one cluster, not a second one


async def test_only_public_current_articles_are_embedded_or_compared(
    worker_engine: AsyncEngine, owner_engine: AsyncEngine, alpha_beta: tuple[Org, Org]
) -> None:
    alpha, beta = alpha_beta
    shown = await story(owner_engine, alpha, "shown", STORY)
    async with owner_engine.begin() as connection:
        personal = await add_item(
            connection,
            alpha,
            url="https://news.example.org/personal",
            owner=alpha.user,
            title=SAME[0],
            excerpt=SAME[1],
            **read_columns(),
        )
    held = await story(owner_engine, alpha, "held", SAME, status="held", hold_reason="editor_hold")
    hidden = await story(owner_engine, alpha, "hidden", SAME, status="hidden")
    unread = await story(
        owner_engine,
        alpha,
        "unread",
        SAME,
        summary=None,
        category=None,
        quality=None,
        enriched_at=None,
        enrichment_version=None,
    )
    theirs = await story(owner_engine, beta, "theirs", SAME)
    gateway = Gateway()
    done = await run(worker_engine, alpha, gateway)
    # Only the one shown article is even tried: no call is made for the others, none is skipped.
    assert done.embedded == 1 and done.clustered == 0 and done.skipped == 0 and gateway.calls == 1
    found = await clusters(owner_engine, alpha)
    assert found[shown] is None and all(found[i] is None for i in (personal, held, hidden, unread))
    async with owner_engine.connect() as connection:
        embedded = {
            row.item_id
            for row in await connection.execute(text("SELECT item_id FROM item_embeddings"))
        }
    assert embedded == {shown}
    assert (await clusters(owner_engine, beta))[theirs] is None  # another organization untouched


async def test_an_article_never_joins_a_cluster_with_one_that_is_not_a_candidate_card(
    worker_engine: AsyncEngine, owner_engine: AsyncEngine, alpha_beta: tuple[Org, Org]
) -> None:
    alpha, _ = alpha_beta
    # Read by the model but found irrelevant: no summary, so it is never a card.
    await story(owner_engine, alpha, "irrelevant", STORY, summary=None, category=None, quality=None)
    only = await story(owner_engine, alpha, "only", SAME)
    done = await run(worker_engine, alpha)
    assert done.embedded == 1 and done.clustered == 0
    assert (await clusters(owner_engine, alpha))[only] is None


async def test_old_articles_are_outside_the_window_in_both_directions(
    worker_engine: AsyncEngine, owner_engine: AsyncEngine, alpha_beta: tuple[Org, Org]
) -> None:
    alpha, _ = alpha_beta
    old = await story(owner_engine, alpha, "old", STORY)
    async with owner_engine.begin() as connection:
        await connection.execute(
            text("UPDATE items SET created_at = now() - interval '20 days' WHERE id = :i"),
            {"i": old},
        )
    fresh = await story(owner_engine, alpha, "fresh", SAME)
    done = await run(worker_engine, alpha)
    assert done.embedded == 1  # the old one is not even embedded
    assert (await clusters(owner_engine, alpha))[fresh] is None
    # Positive control: the same two, both recent, are one story.
    async with owner_engine.begin() as connection:
        await connection.execute(
            text("UPDATE items SET created_at = now() WHERE id = :i"), {"i": old}
        )
    again = await run(worker_engine, alpha)
    assert again.embedded == 1 and again.clustered == 1


async def test_the_threshold_decides_and_a_higher_one_keeps_them_apart(
    worker_engine: AsyncEngine, owner_engine: AsyncEngine, alpha_beta: tuple[Org, Org]
) -> None:
    alpha, _ = alpha_beta
    await story(owner_engine, alpha, "a", STORY)
    await story(owner_engine, alpha, "b", SAME)
    done = await run(worker_engine, alpha, threshold=0.99)
    assert done.embedded == 2 and done.clustered == 0


async def test_vectors_of_another_model_or_size_are_never_compared(
    worker_engine: AsyncEngine, owner_engine: AsyncEngine, alpha_beta: tuple[Org, Org]
) -> None:
    alpha, _ = alpha_beta
    older = await story(owner_engine, alpha, "older", STORY)
    async with owner_engine.begin() as connection:
        # A vector of another model, of another size, for a very similar article.
        await connection.execute(
            text(
                "INSERT INTO item_embeddings (org_id, item_id, model, dim, embedding) "
                "VALUES (:o, :i, 'other-model', 8, CAST(:v AS vector))"
            ),
            {"o": alpha.id, "i": older, "v": "[1,0,0,0,0,0,0,0]"},
        )
    newer = await story(owner_engine, alpha, "newer", SAME)
    done = await run(worker_engine, alpha)
    # `older` has no vector for MODEL yet, so both are embedded now; they cluster with each other
    # and the vector of the other model is never in the comparison (no size mismatch error).
    assert done.failed == 0 and done.embedded == 2 and done.clustered == 1
    found = await clusters(owner_engine, alpha)
    assert found[older] is not None and found[older] == found[newer]


async def test_two_sweeps_at_once_make_one_cluster_not_two(
    worker_engine: AsyncEngine, owner_engine: AsyncEngine, alpha_beta: tuple[Org, Org]
) -> None:
    alpha, _ = alpha_beta
    ids = [
        await story(owner_engine, alpha, f"n{i}", (STORY[0] + f" {i}", STORY[1])) for i in range(4)
    ]
    await asyncio.gather(*(run(worker_engine, alpha, threshold=0.8) for _ in range(3)))
    found = await clusters(owner_engine, alpha)
    assert len({found[i] for i in ids}) == 1 and found[ids[0]] is not None


async def test_a_failing_embedder_stops_the_sweep_and_leaves_the_articles_alone(
    worker_engine: AsyncEngine, owner_engine: AsyncEngine, alpha_beta: tuple[Org, Org]
) -> None:
    alpha, _ = alpha_beta
    a = await story(owner_engine, alpha, "a", STORY)
    done = await run(worker_engine, alpha, Gateway(ProviderFailed()))
    assert done.embedded == 0 and done.stopped == "provider_failed"
    assert (await clusters(owner_engine, alpha))[a] is None
    async with owner_engine.connect() as connection:
        count = (
            await connection.execute(text("SELECT count(*) FROM item_embeddings"))
        ).scalar_one()
    assert count == 0
    ok = await run(worker_engine, alpha)  # and the next sweep simply tries again
    assert ok.embedded == 1


async def test_an_article_that_stops_being_public_meanwhile_is_skipped(
    worker_engine: AsyncEngine, owner_engine: AsyncEngine, alpha_beta: tuple[Org, Org]
) -> None:
    alpha, _ = alpha_beta
    item = await story(owner_engine, alpha, "a", STORY)

    class Hiding(Gateway):
        async def embed(self, *, org_id: uuid.UUID, article: PublicArticleText) -> Embedded:
            async with owner_engine.begin() as connection:
                await connection.execute(
                    text("UPDATE items SET status = 'hidden' WHERE id = :i"), {"i": item}
                )
            return await super().embed(org_id=org_id, article=article)

    done = await run(worker_engine, alpha, Hiding())
    assert done.embedded == 0 and done.skipped == 1
    async with owner_engine.connect() as connection:
        count = (
            await connection.execute(text("SELECT count(*) FROM item_embeddings"))
        ).scalar_one()
    assert count == 0  # nothing stored for an article that left


async def test_the_sweep_refuses_a_personal_article_even_if_asked_by_id(
    worker_engine: AsyncEngine, owner_engine: AsyncEngine, alpha_beta: tuple[Org, Org]
) -> None:
    alpha, _ = alpha_beta
    async with owner_engine.begin() as connection:
        mine = await add_item(
            connection,
            alpha,
            url="https://news.example.org/mine",
            owner=alpha.user,
            title="Private",
            excerpt="p",
            **read_columns(),
        )
    from pharos.llm.repository import load_public_article

    async with worker_engine.begin() as connection:
        with pytest.raises(NotPublicArticle):
            await load_public_article(connection, alpha.id, mine)
    async with worker_engine.begin() as connection:
        placed = await clustering.place(
            connection,
            alpha.id,
            mine,
            Embedded(MODEL, (0.5,) * 16),
            threshold=0.5,
            window_days=14,
        )
    assert placed is None  # not stored either


# --- the table ---------------------------------------------------------------------------------------


async def insert(connection: Any, org: Org, item: uuid.UUID, **values: Any) -> None:
    params = {"o": org.id, "i": item, "m": "m", "d": 8, "v": "[1,0,0,0,0,0,0,0]", **values}
    await connection.execute(
        text(
            "INSERT INTO item_embeddings (org_id, item_id, model, dim, embedding) "
            "VALUES (:o, :i, :m, :d, CAST(:v AS vector))"
        ),
        params,
    )


@pytest.mark.parametrize(
    "bad",
    [
        {"d": 9},  # the stated size must be the real size
        {"d": 4},  # below the bounds, and it is the real size of a 4-vector
        {"m": ""},
        {"m": "has space"},
        {"m": "x" * 101},
        {"m": "line\nbreak"},
    ],
)
async def test_the_database_refuses_a_vector_that_disagrees_with_itself(
    owner_engine: AsyncEngine, alpha_beta: tuple[Org, Org], bad: dict[str, Any]
) -> None:
    alpha, _ = alpha_beta
    item = await story(owner_engine, alpha, "a", STORY)
    values = dict(bad)
    if bad.get("d") == 4:
        values["v"] = "[1,0,0,0]"
    async with owner_engine.begin() as connection:
        await insert(connection, alpha, item)  # positive control: a good row goes in
    with pytest.raises(DBAPIError):
        async with owner_engine.begin() as connection:
            await insert(connection, alpha, item, **{"m": "second", **values})


async def test_a_vector_goes_with_its_article(
    owner_engine: AsyncEngine, alpha_beta: tuple[Org, Org]
) -> None:
    alpha, _ = alpha_beta
    item = await story(owner_engine, alpha, "a", STORY)
    async with owner_engine.begin() as connection:
        await insert(connection, alpha, item)
    async with owner_engine.begin() as connection:
        await connection.execute(text("DELETE FROM items WHERE id = :i"), {"i": item})
    async with owner_engine.connect() as connection:
        left = (await connection.execute(text("SELECT count(*) FROM item_embeddings"))).scalar_one()
    assert left == 0
    other = uuid.uuid4()
    with pytest.raises(DBAPIError):  # and none can exist for an article that does not
        async with owner_engine.begin() as connection:
            await insert(connection, alpha, other)


async def test_the_worker_may_add_read_and_delete_vectors_and_not_change_them(
    worker_engine: AsyncEngine, owner_engine: AsyncEngine, alpha_beta: tuple[Org, Org]
) -> None:
    alpha, _ = alpha_beta
    item = await story(owner_engine, alpha, "a", STORY)
    async with worker_engine.begin() as connection:
        await connection.execute(
            text("SELECT set_config('app.org_id', :o, true)"), {"o": str(alpha.id)}
        )
        await insert(connection, alpha, item)
        got = (await connection.execute(text("SELECT count(*) FROM item_embeddings"))).scalar_one()
        assert got == 1
    for statement in (
        "UPDATE item_embeddings SET model = 'x'",
        "UPDATE item_embeddings SET embedding = CAST('[0,1,0,0,0,0,0,0]' AS vector)",
    ):
        with pytest.raises(DBAPIError):
            async with worker_engine.begin() as connection:
                await connection.execute(
                    text("SELECT set_config('app.org_id', :o, true)"), {"o": str(alpha.id)}
                )
                await connection.execute(text(statement))
    async with worker_engine.begin() as connection:  # positive control: its one right on items
        await connection.execute(
            text("SELECT set_config('app.org_id', :o, true)"), {"o": str(alpha.id)}
        )
        await connection.execute(
            text("UPDATE items SET cluster_id = gen_random_uuid() WHERE id = :i"), {"i": item}
        )


async def test_the_application_role_cannot_read_vectors_at_all(
    app_engine: AsyncEngine, owner_engine: AsyncEngine, alpha_beta: tuple[Org, Org]
) -> None:
    alpha, _ = alpha_beta
    item = await story(owner_engine, alpha, "a", STORY)
    async with owner_engine.begin() as connection:
        await insert(connection, alpha, item)
    with pytest.raises(DBAPIError):
        async with app_engine.begin() as connection:
            await connection.execute(text("SELECT count(*) FROM item_embeddings"))


async def test_rows_of_another_organization_are_invisible_to_the_worker(
    worker_engine: AsyncEngine, owner_engine: AsyncEngine, alpha_beta: tuple[Org, Org]
) -> None:
    alpha, beta = alpha_beta
    theirs = await story(owner_engine, beta, "theirs", STORY)
    async with owner_engine.begin() as connection:
        await insert(connection, beta, theirs)
    async with worker_engine.begin() as connection:
        await connection.execute(
            text("SELECT set_config('app.org_id', :o, true)"), {"o": str(alpha.id)}
        )
        assert (
            await connection.execute(text("SELECT count(*) FROM item_embeddings"))
        ).scalar_one() == 0
        await connection.execute(
            text("SELECT set_config('app.org_id', :o, true)"), {"o": str(beta.id)}
        )
        assert (
            await connection.execute(text("SELECT count(*) FROM item_embeddings"))
        ).scalar_one() == 1  # positive control: the owner organization sees it


async def test_the_usage_record_accepts_the_embedding_stage_and_nothing_invented(
    worker_engine: AsyncEngine, owner_engine: AsyncEngine, alpha_beta: tuple[Org, Org]
) -> None:
    alpha, _ = alpha_beta

    async def record(stage: str) -> None:
        async with worker_engine.begin() as connection:
            await connection.execute(
                text("SELECT set_config('app.org_id', :o, true)"), {"o": str(alpha.id)}
            )
            await connection.execute(
                text(
                    "INSERT INTO llm_usage (org_id, stage, provider, model, tokens_in, "
                    "tokens_out, cost_micro_usd, outcome) VALUES (:o, :s, 'p', 'm', 1, 0, 1, 'ok')"
                ),
                {"o": alpha.id, "s": stage},
            )

    try:
        await record("embedding")
        with pytest.raises(DBAPIError):
            await record("embeddings")
    finally:  # the record is append-only for the worker; the table owner clears the test's rows
        async with owner_engine.begin() as connection:
            await connection.execute(
                text("DELETE FROM llm_usage WHERE org_id = :o"), {"o": alpha.id}
            )


async def test_the_migration_goes_down_and_up_again(
    database: Any, owner_engine: AsyncEngine, alpha_beta: tuple[Org, Org]
) -> None:
    from alembic import command

    alpha, _ = alpha_beta
    item = await story(owner_engine, alpha, "a", STORY)
    async with owner_engine.begin() as connection:
        await insert(connection, alpha, item)
        await connection.execute(
            text(
                "INSERT INTO llm_usage (org_id, stage, provider, model, tokens_in, tokens_out, "
                "cost_micro_usd, outcome) VALUES (:o, 'embedding', 'p', 'm', 1, 0, 1, 'ok')"
            ),
            {"o": alpha.id},
        )
    await asyncio.to_thread(command.downgrade, database.alembic, "0008")
    async with owner_engine.connect() as connection:
        gone = await connection.execute(text("SELECT to_regclass('public.item_embeddings')"))
        assert gone.scalar_one() is None
        columns = await connection.execute(
            text("SELECT 1 FROM information_schema.columns WHERE column_name = 'cluster_id'")
        )
        assert columns.first() is None
        assert (
            await connection.execute(
                text("SELECT count(*) FROM llm_usage WHERE stage = 'embedding'")
            )
        ).scalar_one() == 0
    await asyncio.to_thread(command.upgrade, database.alembic, "head")
    async with owner_engine.connect() as connection:
        back = await connection.execute(text("SELECT to_regclass('public.item_embeddings')"))
        assert back.scalar_one() is not None


_ = timedelta  # kept for tests that move time


# --- what a neighbour must be --------------------------------------------------------------------------


async def vector_of(words: tuple[str, str]) -> str:
    from pharos.llm.embedding import check_vector, embedding_text

    article = mint_public_text(words[0], words[1], "https://news.example.org/x", None)
    answer = await DemoEmbedder().embed(
        model=MODEL, texts=[embedding_text(article)], deadline_seconds=1
    )
    return clustering.vector_literal(check_vector(answer))


async def give_vector(
    owner_engine: AsyncEngine, org: Org, item: uuid.UUID, model: str, literal: str, dim: int = 64
) -> None:
    async with owner_engine.begin() as connection:
        await insert(connection, org, item, m=model, d=dim, v=literal)


async def test_a_vector_of_another_model_of_the_same_size_is_never_compared(
    worker_engine: AsyncEngine, owner_engine: AsyncEngine, alpha_beta: tuple[Org, Org]
) -> None:
    alpha, _ = alpha_beta
    older = await story(owner_engine, alpha, "older", OTHER)
    await give_vector(owner_engine, alpha, older, MODEL, await vector_of(OTHER))
    # The same article has a vector of ANOTHER model that looks exactly like the newcomer.
    await give_vector(owner_engine, alpha, older, "other-model", await vector_of(STORY))
    newer = await story(owner_engine, alpha, "newer", STORY)
    done = await run(worker_engine, alpha)
    assert done.embedded == 1 and done.clustered == 0
    assert (await clusters(owner_engine, alpha))[newer] is None


@pytest.mark.parametrize(
    "state",
    [
        {"owner": True},
        {"status": "held", "hold_reason": "editor_hold"},
        {"status": "hidden"},
        {"summary": None, "category": None, "quality": None},
    ],
    ids=["personal", "held", "hidden", "no-summary"],
)
async def test_only_a_public_current_card_can_be_a_neighbour(
    worker_engine: AsyncEngine,
    owner_engine: AsyncEngine,
    alpha_beta: tuple[Org, Org],
    state: dict[str, Any],
) -> None:
    alpha, _ = alpha_beta
    columns = dict(state)
    owner = alpha.user if columns.pop("owner", False) else None
    async with owner_engine.begin() as connection:
        neighbour = await add_item(
            connection,
            alpha,
            url="https://news.example.org/neighbour",
            owner=owner,
            title=STORY[0],
            excerpt=STORY[1],
            **read_columns(**columns),
        )
    # It already has a vector identical to the newcomer's (someone embedded it earlier).
    await give_vector(owner_engine, alpha, neighbour, MODEL, await vector_of(STORY))
    newcomer = await story(owner_engine, alpha, "newcomer", STORY)
    done = await run(worker_engine, alpha)
    assert done.embedded == 1 and done.clustered == 0
    assert (await clusters(owner_engine, alpha))[newcomer] is None
    # Positive control: the same neighbour as a normal public card does join.
    async with owner_engine.begin() as connection:
        await connection.execute(
            text(
                "UPDATE items SET owner_user_id = NULL, status = 'new', hold_reason = NULL, "
                "summary = 'S', category = 'news', quality = 3 WHERE id = :i"
            ),
            {"i": neighbour},
        )
    again = await story(owner_engine, alpha, "again", STORY)
    assert (await run(worker_engine, alpha)).clustered == 1
    assert (await clusters(owner_engine, alpha))[again] is not None


async def test_the_sweep_filters_by_organization_itself_not_only_through_row_security(
    owner_engine: AsyncEngine, alpha_beta: tuple[Org, Org]
) -> None:
    """As the table owner, which row-level security does not apply to: only the statements stand
    between one organization and another's articles."""
    alpha, beta = alpha_beta
    theirs = await story(owner_engine, beta, "theirs", STORY)
    await give_vector(owner_engine, beta, theirs, MODEL, await vector_of(STORY))
    ours = await story(owner_engine, alpha, "ours", STORY)
    done = await run(owner_engine, alpha)
    assert done.embedded == 1 and done.clustered == 0
    assert (await clusters(owner_engine, alpha))[ours] is None
    assert (await clusters(owner_engine, beta))[theirs] is None


async def test_a_stale_vector_of_the_same_model_name_but_another_size_is_never_compared(
    worker_engine: AsyncEngine, owner_engine: AsyncEngine, alpha_beta: tuple[Org, Org]
) -> None:
    """A model kept its name and changed its size (it happens): the old rows must not be compared
    with the new ones, and must not make the sweep fail."""
    alpha, _ = alpha_beta
    older = await story(owner_engine, alpha, "older", OTHER)
    await give_vector(owner_engine, alpha, older, MODEL, "[1,0,0,0,0,0,0,0]", dim=8)
    newer = await story(owner_engine, alpha, "newer", STORY)
    done = await run(worker_engine, alpha)
    assert done.failed == 0 and done.stopped is None
    # The stale row keeps `older` out of the queue (it has a vector for this model name), so it is
    # simply not compared; a changed model needs a new name, which recomputes (ADR-0007).
    assert done.embedded == 1 and done.clustered == 0
    assert (await clusters(owner_engine, alpha))[newer] is None


async def test_an_old_article_that_already_has_a_vector_is_not_a_neighbour(
    worker_engine: AsyncEngine, owner_engine: AsyncEngine, alpha_beta: tuple[Org, Org]
) -> None:
    alpha, _ = alpha_beta
    old = await story(owner_engine, alpha, "old", STORY)
    await give_vector(owner_engine, alpha, old, MODEL, await vector_of(STORY))
    async with owner_engine.begin() as connection:
        await connection.execute(
            text("UPDATE items SET created_at = now() - interval '20 days' WHERE id = :i"),
            {"i": old},
        )
    newer = await story(owner_engine, alpha, "newer", STORY)
    done = await run(worker_engine, alpha)
    assert done.embedded == 1 and done.clustered == 0
    assert (await clusters(owner_engine, alpha))[newer] is None


# --- found in the review of M4-05 ----------------------------------------------------------------------


def unit_vector(degrees: float) -> list[float]:
    """A vector of eight numbers at an angle: neighbours 30 degrees apart are 0.87 similar."""
    angle = math.radians(degrees)
    return [math.cos(angle), math.sin(angle)] + [0.0] * 6


class Scripted(Gateway):
    """A gateway whose vectors are chosen by the title of the article."""

    def __init__(self, by_title: dict[str, list[float]]) -> None:
        super().__init__()
        self.by_title = by_title

    async def embed(self, *, org_id: uuid.UUID, article: PublicArticleText) -> Embedded:
        self.calls += 1
        title = next(t for t in self.by_title if article.title.startswith(t))
        return Embedded(MODEL, tuple(self.by_title[title]))


async def titled(
    owner_engine: AsyncEngine, org: Org, names: list[str], seconds: int = 1
) -> dict[str, uuid.UUID]:
    found: dict[str, uuid.UUID] = {}
    for position, name in enumerate(names):
        found[name] = await story(owner_engine, org, name, (f"{name} article", "text"))
        async with owner_engine.begin() as connection:
            await connection.execute(
                text(
                    "UPDATE items SET created_at = now() - make_interval(secs => :s) WHERE id = :i"
                ),
                {"s": (len(names) - position) * seconds, "i": found[name]},
            )
    return found


async def test_a_chain_of_near_copies_does_not_pull_a_different_story_into_a_cluster(
    worker_engine: AsyncEngine, owner_engine: AsyncEngine, alpha_beta: tuple[Org, Org]
) -> None:
    """A is like B, B like C, C like D, but A and C are different stories (60 degrees apart): the
    newcomer must be close to the FIRST member too, or the cluster grows one step at a time."""
    alpha, _ = alpha_beta
    ids = await titled(owner_engine, alpha, ["a", "b", "c", "d"])
    gateway = Scripted(
        {"a": unit_vector(0), "b": unit_vector(30), "c": unit_vector(60), "d": unit_vector(90)}
    )
    done = await run(worker_engine, alpha, gateway, threshold=0.85)
    assert done.embedded == 4 and done.failed == 0
    found = await clusters(owner_engine, alpha)
    assert found[ids["a"]] is not None and found[ids["a"]] == found[ids["b"]]
    assert found[ids["c"]] is not None and found[ids["c"]] == found[ids["d"]]
    assert found[ids["a"]] != found[ids["c"]]  # two stories, not one chain


async def test_a_cluster_has_a_size_limit_whatever_the_vectors_say(
    worker_engine: AsyncEngine,
    owner_engine: AsyncEngine,
    alpha_beta: tuple[Org, Org],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    alpha, _ = alpha_beta
    monkeypatch.setattr(clustering, "MAX_CLUSTER_SIZE", 3)
    names = [f"n{i}" for i in range(7)]
    await titled(owner_engine, alpha, names)
    await run(worker_engine, alpha, Scripted({n: unit_vector(0) for n in names}), threshold=0.9)
    found = await clusters(owner_engine, alpha)
    sizes: dict[uuid.UUID, int] = {}
    for cluster in found.values():
        if cluster is not None:
            sizes[cluster] = sizes.get(cluster, 0) + 1
    # With equal vectors the nearest neighbour is chosen by article id, so how many articles end up
    # in clusters varies from run to run. What does not vary: the first two make a cluster, the
    # third always joins it, and a fourth always meets the cap. So the cap is reached exactly.
    assert sizes and max(sizes.values()) == 3


async def test_a_full_cluster_leaves_the_next_copies_to_start_another(
    worker_engine: AsyncEngine,
    owner_engine: AsyncEngine,
    alpha_beta: tuple[Org, Org],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Vectors one degree apart make the nearest neighbour the previous article every time, so the
    outcome is fixed: n0 to n2 fill the first cluster, n3 is refused by it but n4 pairs with n3,
    n5 completes that second cluster, and n6 meets its cap and stands alone."""
    alpha, _ = alpha_beta
    monkeypatch.setattr(clustering, "MAX_CLUSTER_SIZE", 3)
    names = [f"n{i}" for i in range(7)]
    ids = await titled(owner_engine, alpha, names)
    vectors = {name: unit_vector(position) for position, name in enumerate(names)}
    await run(worker_engine, alpha, Scripted(vectors), threshold=0.9)
    found = await clusters(owner_engine, alpha)
    first = {found[ids[n]] for n in names[:3]}
    second = {found[ids[n]] for n in names[3:6]}
    assert len(first) == 1 and None not in first
    assert len(second) == 1 and None not in second
    assert first != second
    assert found[ids["n6"]] is None  # the cap, not the vectors, kept it out


@pytest.mark.parametrize("similarity", [None, math.nan, math.inf, -math.inf, 0.79, "0.99"])
def test_something_that_is_not_a_real_similarity_never_joins(similarity: object) -> None:
    assert clustering._reaches(similarity, 0.8) is False
    assert clustering._reaches(0.8, 0.8) is True and clustering._reaches(1.0, 0.8) is True


async def test_the_database_refuses_a_vector_with_no_length(
    owner_engine: AsyncEngine, alpha_beta: tuple[Org, Org]
) -> None:
    alpha, _ = alpha_beta
    item = await story(owner_engine, alpha, "a", STORY)
    with pytest.raises(DBAPIError):
        async with owner_engine.begin() as connection:
            await insert(connection, alpha, item, v="[0,0,0,0,0,0,0,0]")
    # What 32-bit floats turn a tiny vector into is the same: zeros.
    with pytest.raises(DBAPIError):
        async with owner_engine.begin() as connection:
            await insert(
                connection, alpha, item, v="[1e-50,1e-50,1e-50,1e-50,1e-50,1e-50,1e-50,1e-50]"
            )
    async with owner_engine.begin() as connection:
        await insert(connection, alpha, item)  # positive control


async def test_the_clusters_can_be_undone_and_built_again(
    worker_engine: AsyncEngine, owner_engine: AsyncEngine, alpha_beta: tuple[Org, Org]
) -> None:
    alpha, beta = alpha_beta
    a = await story(owner_engine, alpha, "a", STORY)
    b = await story(owner_engine, alpha, "b", SAME)
    theirs = await story(owner_engine, beta, "theirs", STORY)
    await give_vector(owner_engine, beta, theirs, MODEL, await vector_of(STORY))
    await run(worker_engine, alpha)
    first = await clustering.status(worker_engine, alpha.id)
    assert first == {"vectors": 2, "clustered": 2, "clusters": 1, "largest": 2}
    deleted, cleared = await clustering.reset(worker_engine, alpha.id)
    assert (deleted, cleared) == (2, 2)
    assert await clustering.status(worker_engine, alpha.id) == {
        "vectors": 0, "clustered": 0, "clusters": 0, "largest": 0
    }  # fmt: skip
    # Another organization's vector is still there (the count is of the whole table, as the owner).
    async with owner_engine.connect() as connection:
        left = (await connection.execute(text("SELECT count(*) FROM item_embeddings"))).scalar_one()
    assert left == 1
    # And the next sweep builds the same clusters again.
    again = await run(worker_engine, alpha)
    assert again.embedded == 2 and again.clustered == 1
    found = await clusters(owner_engine, alpha)
    assert found[a] is not None and found[a] == found[b]


async def test_a_reset_for_one_model_leaves_the_vectors_of_another(
    worker_engine: AsyncEngine, owner_engine: AsyncEngine, alpha_beta: tuple[Org, Org]
) -> None:
    alpha, _ = alpha_beta
    item = await story(owner_engine, alpha, "a", STORY)
    await give_vector(owner_engine, alpha, item, MODEL, await vector_of(STORY))
    await give_vector(owner_engine, alpha, item, "other-model", await vector_of(STORY))
    deleted, _ = await clustering.reset(worker_engine, alpha.id, model="other-model")
    assert deleted == 1
    assert (await clustering.status(worker_engine, alpha.id))["vectors"] == 1
    deleted, _ = await clustering.reset(worker_engine, alpha.id)
    assert deleted == 1


async def test_only_the_worker_may_delete_vectors(
    app_engine: AsyncEngine, owner_engine: AsyncEngine, alpha_beta: tuple[Org, Org]
) -> None:
    alpha, _ = alpha_beta
    item = await story(owner_engine, alpha, "a", STORY)
    async with owner_engine.begin() as connection:
        await insert(connection, alpha, item)
    with pytest.raises(DBAPIError):
        async with app_engine.begin() as connection:
            await connection.execute(text("DELETE FROM item_embeddings"))


# --- found in the second review of M4-05 -------------------------------------------------------------


def literal(degrees: float) -> str:
    return clustering.vector_literal(tuple(unit_vector(degrees)))


@pytest.mark.parametrize(("limit", "joins"), [(2, False), (20, True)])
async def test_the_size_cap_holds_when_the_newcomer_already_has_a_cluster(
    worker_engine: AsyncEngine,
    owner_engine: AsyncEngine,
    alpha_beta: tuple[Org, Org],
    monkeypatch: pytest.MonkeyPatch,
    limit: int,
    joins: bool,
) -> None:
    """Clusters left over from another model (no reset) must not grow past the cap when a
    neighbour with no cluster would join them. The second case is the positive control."""
    alpha, _ = alpha_beta
    monkeypatch.setattr(clustering, "MAX_CLUSTER_SIZE", limit)
    ids = await titled(owner_engine, alpha, ["a", "b", "c"])
    old = uuid.uuid4()
    async with owner_engine.begin() as connection:
        await connection.execute(
            text("UPDATE items SET cluster_id = :c WHERE id IN (:a, :b)"),
            {"c": old, "a": ids["a"], "b": ids["b"]},
        )
    await give_vector(owner_engine, alpha, ids["c"], MODEL, literal(0), dim=8)
    gateway = Scripted({"a": unit_vector(0), "b": unit_vector(0), "c": unit_vector(0)})
    await run(worker_engine, alpha, gateway, threshold=0.9)
    found = await clusters(owner_engine, alpha)
    assert (found[ids["c"]] == old) is joins
    if not joins:
        assert found[ids["c"]] is None


@pytest.mark.parametrize(("age_days", "joins"), [(0, True), (20, False)])
async def test_a_vector_stored_long_ago_is_not_searched(
    worker_engine: AsyncEngine,
    owner_engine: AsyncEngine,
    alpha_beta: tuple[Org, Org],
    age_days: int,
    joins: bool,
) -> None:
    """The search is bounded by the window on the vector too, so it can use the index and does not
    scan every vector kept until the article is deleted."""
    alpha, _ = alpha_beta
    ids = await titled(owner_engine, alpha, ["old", "new"])
    await give_vector(owner_engine, alpha, ids["old"], MODEL, literal(0), dim=8)
    async with owner_engine.begin() as connection:
        await connection.execute(
            text(
                "UPDATE item_embeddings SET created_at = now() - make_interval(days => :d) "
                "WHERE item_id = :i"
            ),
            {"d": age_days, "i": ids["old"]},
        )
    done = await run(worker_engine, alpha, Scripted({"new": unit_vector(0)}), threshold=0.9)
    assert done.embedded == 1 and done.clustered == int(joins)
    assert ((await clusters(owner_engine, alpha))[ids["new"]] is not None) is joins
