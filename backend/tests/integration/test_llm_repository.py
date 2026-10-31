"""The only place that makes public text: it reads the database, and the owner and the state are
decided by its SQL, not by the caller (ADR-0015, class A)."""

# ruff: noqa: F811  (fixtures are imported by name)

import dataclasses
import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from pharos.llm.errors import NotPublicArticle
from pharos.llm.repository import load_public_article

from .ingestion_support import Org, add_item, alpha_beta, worker_engine  # noqa: F401

pytestmark = [pytest.mark.integration, pytest.mark.anyio]


async def load(engine: AsyncEngine, org: Org, item: uuid.UUID):  # type: ignore[no-untyped-def]
    async with engine.begin() as connection:
        return await load_public_article(connection, org.id, item)


async def test_an_organization_article_that_is_new_becomes_public_text(
    worker_engine: AsyncEngine, owner_engine: AsyncEngine, alpha_beta: tuple[Org, Org]
) -> None:
    alpha, _ = alpha_beta
    async with owner_engine.begin() as connection:
        item = await add_item(
            connection,
            alpha,
            url="https://News.Example.org/a?utm=1",
            title="T",
            excerpt="E",
            lang="en",
        )
    text = await load(worker_engine, alpha, item)
    assert (text.title, text.excerpt, text.host, text.language) == (
        "T",
        "E",
        "news.example.org",
        "en",
    )
    assert text.is_genuine() and not dataclasses.replace(text, title="other").is_genuine()


@pytest.mark.parametrize("status", ["held", "hidden"])
async def test_a_held_or_hidden_article_is_not_public_text(
    worker_engine: AsyncEngine, owner_engine: AsyncEngine, alpha_beta: tuple[Org, Org], status: str
) -> None:
    alpha, _ = alpha_beta
    async with owner_engine.begin() as connection:
        reason = {"hold_reason": "editor_hold"} if status == "held" else {}
        item = await add_item(connection, alpha, url="https://x.example/1", status=status, **reason)
    with pytest.raises(NotPublicArticle):
        await load(worker_engine, alpha, item)


async def test_a_persons_own_article_is_not_public_text_even_when_asked_for_by_its_owners_org(
    worker_engine: AsyncEngine, owner_engine: AsyncEngine, alpha_beta: tuple[Org, Org]
) -> None:
    alpha, _ = alpha_beta
    async with owner_engine.begin() as connection:
        mine = await add_item(connection, alpha, url="https://x.example/mine", owner=alpha.user)
        shared = await add_item(connection, alpha, url="https://x.example/shared")
    with pytest.raises(NotPublicArticle):
        await load(worker_engine, alpha, mine)
    assert (await load(worker_engine, alpha, shared)).host == "x.example"  # control


async def test_an_article_of_another_organization_or_one_that_does_not_exist_is_refused_alike(
    worker_engine: AsyncEngine, owner_engine: AsyncEngine, alpha_beta: tuple[Org, Org]
) -> None:
    alpha, beta = alpha_beta
    async with owner_engine.begin() as connection:
        theirs = await add_item(connection, beta, url="https://x.example/theirs")
    with pytest.raises(NotPublicArticle):
        await load(worker_engine, alpha, theirs)
    with pytest.raises(NotPublicArticle):
        await load(worker_engine, alpha, uuid.uuid4())
    assert (await load(worker_engine, beta, theirs)).host == "x.example"  # control


async def test_the_query_filters_by_organization_itself_not_only_through_row_security(
    owner_engine: AsyncEngine, alpha_beta: tuple[Org, Org]
) -> None:
    """As the table owner, which row-level security does not apply to: only the statement stands
    between one organization and another's article."""
    alpha, beta = alpha_beta
    async with owner_engine.begin() as connection:
        theirs = await add_item(connection, beta, url="https://x.example/theirs")
    with pytest.raises(NotPublicArticle):
        await load(owner_engine, alpha, theirs)
    assert (await load(owner_engine, beta, theirs)).host == "x.example"  # control
