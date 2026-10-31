"""The only place that makes `PublicArticleText` (ADR-0015, point 2).

An article is read from the database, in the organization of the call, and only if it has no owner
and is `new`. The row is not handed over by the caller, so nothing but this query decides what is
class A. Runs as the worker role, which already may read `items`.
"""

import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from pharos.llm.errors import NotPublicArticle
from pharos.llm.types import PublicArticleText, mint_public_text


async def load_public_article(
    connection: AsyncConnection, org_id: uuid.UUID, item_id: uuid.UUID
) -> PublicArticleText:
    """The public text of one organization article, or NotPublicArticle (also when it does not
    exist or belongs to another organization: the answer is the same)."""
    await connection.execute(
        text("SELECT set_config('app.org_id', :org, true)"), {"org": str(org_id)}
    )
    row = (
        await connection.execute(
            text(
                "SELECT title, excerpt, canonical_url, lang FROM items "
                "WHERE org_id = :org AND id = :id AND owner_user_id IS NULL AND status = 'new'"
            ),
            {"org": org_id, "id": item_id},
        )
    ).first()
    if row is None:
        raise NotPublicArticle()
    return mint_public_text(row.title, row.excerpt, row.canonical_url, row.lang)
