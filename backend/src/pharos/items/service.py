"""What people read and what editors review (M3-10b).

* Everyone signed in reads the list of articles, but a member sees only the articles that are
  `new`: held and hidden ones are for administrators and editors, who also see which sources
  reported an article and why it is held.
* Review is for administrators and editors. A held article is one a rule flagged (or an editor
  held); approving it clears the flag, hiding it takes it out of the list, restoring brings a hidden
  one back. Each action is one step of a small state machine; asking for a state the article is in
  already changes nothing, and a step that does not exist answers `409 item_state`.
* The worker can neither undo nor repeat these decisions (it has no right to change `status`, and
  a hidden article is never created again), so a decision stays until a person changes it.
* Two editors acting on one article queue up on its row lock; the second decides on what the first
  left behind. Every change is audited with the state it came from.
"""

import uuid
from typing import Literal

from pharos.core.audit import AuditAction, AuditLog
from pharos.core.errors import ApiError, ErrorCode, forbidden, not_found
from pharos.core.principal import Principal, Role
from pharos.core.unit_of_work import UnitOfWork
from pharos.items import repository
from pharos.items.schemas import ItemOut, SourceRef

Action = Literal["hold", "approve", "hide", "restore"]
EDITOR_HOLD = "editor_hold"

# action -> (states it may start from, the state it leads to, the audit event)
STEPS: dict[Action, tuple[frozenset[str], str, AuditAction]] = {
    "hold": (frozenset({"new"}), "held", AuditAction.ITEM_HELD),
    "approve": (frozenset({"held"}), "new", AuditAction.ITEM_APPROVED),
    "hide": (frozenset({"new", "held"}), "hidden", AuditAction.ITEM_HIDDEN),
    "restore": (frozenset({"hidden"}), "new", AuditAction.ITEM_RESTORED),
}


def out(
    view: repository.ItemView, sources: list[tuple[uuid.UUID, str]], *, reviewer: bool
) -> ItemOut:
    """The answer for one article. A member does not get the time it was last changed: a change in
    it would say that an article was reviewed."""
    return ItemOut(
        id=view.id,
        url=view.url,
        title=view.title,
        excerpt=view.excerpt,
        lang=view.lang,
        published_at=view.published_at,
        fetched_at=view.fetched_at,
        status=view.status,  # type: ignore[arg-type]
        hold_reason=view.hold_reason,
        updated_at=view.updated_at if reviewer else None,
        sources=[SourceRef(id=i, name=n) for i, n in sources],
    )


def reviewers(principal: Principal) -> bool:
    return principal.role in (Role.ADMIN, Role.EDITOR)


class ItemService:
    def __init__(self, *, audit: AuditLog) -> None:
        self._audit = audit

    async def _with_sources(
        self,
        uow: UnitOfWork,
        principal: Principal,
        views: list[repository.ItemView],
    ) -> list[tuple[repository.ItemView, list[tuple[uuid.UUID, str]]]]:
        found = (
            await repository.sources_of(uow.session, uow.org_id, [v.id for v in views])
            if reviewers(principal)
            else {}
        )
        return [(v, found.get(v.id, [])) for v in views]

    async def list_items(
        self,
        uow: UnitOfWork,
        principal: Principal,
        *,
        status: str | None,
        source: uuid.UUID | None,
        limit: int,
        offset: int,
    ) -> tuple[list[ItemOut], int]:
        if not reviewers(principal):
            # Members read the list; what is held and which source said what is for reviewers.
            if status not in (None, "new") or source is not None:
                raise forbidden()
            status = "new"
        views, total = await repository.list_items(
            uow.session, uow.org_id, status=status, source=source, limit=limit, offset=offset
        )
        reviewer = reviewers(principal)
        found = await self._with_sources(uow, principal, views)
        return [out(v, s, reviewer=reviewer) for v, s in found], total

    async def get(self, uow: UnitOfWork, principal: Principal, item_id: uuid.UUID) -> ItemOut:
        view = await repository.get_item(uow.session, uow.org_id, item_id)
        if view is None or (not reviewers(principal) and view.status != "new"):
            raise not_found()  # a member is not told that a held or hidden article exists
        [(view, sources)] = await self._with_sources(uow, principal, [view])
        return out(view, sources, reviewer=reviewers(principal))

    async def act(
        self,
        uow: UnitOfWork,
        actor: Principal,
        item_id: uuid.UUID,
        action: Action,
        *,
        ip: str | None,
    ) -> ItemOut:
        starts, target, event = STEPS[action]
        current = await repository.get_item(uow.session, uow.org_id, item_id, lock=True)
        if current is None:
            raise not_found()
        if current.status == target:  # already there: nothing to change, nothing to record
            [(view, sources)] = await self._with_sources(uow, actor, [current])
            return out(view, sources, reviewer=True)
        if current.status not in starts:
            raise ApiError(ErrorCode.ITEM_STATE, 409)
        reason = EDITOR_HOLD if target == "held" else None
        updated = await repository.set_status(
            uow.session, uow.org_id, item_id, status=target, hold_reason=reason
        )
        await self._audit.record(
            uow.session,
            org_id=uow.org_id,
            action=event,
            actor_id=actor.user_id,
            target=f"item:{item_id}",
            ip=ip,
            details={"from": current.status, "was": current.hold_reason},
        )
        [(view, sources)] = await self._with_sources(uow, actor, [updated])
        await uow.commit()
        return out(view, sources, reviewer=True)
