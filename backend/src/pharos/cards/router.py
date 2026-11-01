"""The Home feed, what people do with the cards, and the counts for administrators."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Query, Request, Response

from pharos.cards.schemas import (
    CardPageOut,
    InteractionIn,
    InteractionKind,
    InteractionSummaryOut,
    Undoable,
)
from pharos.cards.service import CardService
from pharos.core.auth import require_role
from pharos.core.principal import Principal, Role
from pharos.core.unit_of_work import UnitOfWorkDep

router = APIRouter(tags=["cards"])


def _service(request: Request) -> CardService:
    service: CardService = request.app.state.cards
    return service


Service = Annotated[CardService, Depends(_service)]
Anyone = Annotated[Principal, Depends(require_role(Role.ADMIN, Role.EDITOR, Role.MEMBER))]
Admin = Annotated[Principal, Depends(require_role(Role.ADMIN))]


@router.get("/cards")
async def get_cards(
    who: Anyone,
    service: Service,
    uow: UnitOfWorkDep,
    cursor: Annotated[str | None, Query(max_length=200)] = None,
    limit: Annotated[int | None, Query(ge=1, le=50)] = None,
    accept_language: Annotated[str | None, Header()] = None,
) -> CardPageOut:
    """The cards for the asking person, best first. Pass `next_cursor` back for the next page."""
    return await service.cards(
        uow, who, cursor=cursor, limit=limit, accept_language=accept_language
    )


@router.put("/items/{item_id}/interactions/{kind}", status_code=204)
async def put_interaction(
    item_id: uuid.UUID,
    kind: InteractionKind,
    who: Anyone,
    service: Service,
    uow: UnitOfWorkDep,
    body: InteractionIn | None = None,
) -> Response:
    await service.put(uow, who, item_id, kind, body.value if body else None)
    return Response(status_code=204)


@router.delete("/items/{item_id}/interactions/{kind}", status_code=204)
async def delete_interaction(
    item_id: uuid.UUID, kind: Undoable, who: Anyone, service: Service, uow: UnitOfWorkDep
) -> Response:
    await service.remove(uow, who, item_id, kind)
    return Response(status_code=204)


@router.get("/interactions/summary")
async def interaction_summary(
    who: Admin, service: Service, uow: UnitOfWorkDep
) -> InteractionSummaryOut:
    """Counts only: per day and kind, how many people and actions, and nothing about who."""
    return await service.summary(uow)
