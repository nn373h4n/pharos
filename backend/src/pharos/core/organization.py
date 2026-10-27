"""The organization of this installation (ADR-0005, ADR-0011).

Before sign-in the application does not know its organization id and RLS hides every row, so it is
looked up through `pharos_org_id_for_slug`, a SECURITY DEFINER function that answers that one
question. The result is cached once found; a missing organization is not cached, so the operator can
create it while the stack is running.
"""

import logging
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

logger = logging.getLogger(__name__)


class OrganizationNotConfigured(Exception):
    """No organization with the configured slug exists (yet)."""


class OrganizationResolver:
    def __init__(self, engine: AsyncEngine, slug: str) -> None:
        self._engine = engine
        self._slug = slug
        self._org_id: uuid.UUID | None = None

    async def resolve(self) -> uuid.UUID:
        if self._org_id is not None:
            return self._org_id
        async with self._engine.connect() as connection:
            found: uuid.UUID | None = (
                await connection.execute(
                    text("SELECT pharos_org_id_for_slug(:slug)"), {"slug": self._slug}
                )
            ).scalar_one()
        if found is None:
            raise OrganizationNotConfigured(self._slug)
        self._org_id = found
        return found
