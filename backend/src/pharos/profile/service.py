"""Profile and onboarding rules. No web framework in here; the router adapts it.

* Everything a person may choose comes from a fixed catalogue (AI level, interests, languages) or
  from rows that belong to the installation (professions, departments). Free text is not accepted.
* Onboarding is complete when all four answers exist: profession, interests, language and AI level.
  The first save stamps `onboarding_started_at` and completing stamps `onboarding_completed_at`
  once, so the time people need can be measured later.
"""

import uuid
from typing import Any

from pharos.core.errors import ApiError, ErrorCode
from pharos.core.locale import SUPPORTED_LOCALES, resolve_locale
from pharos.core.unit_of_work import UnitOfWork
from pharos.profile import repository
from pharos.profile.catalog import AI_LEVELS, INTEREST_CODES, MAX_INTERESTS
from pharos.profile.schemas import ProfileIn, ProfileOut


def _invalid(field: str, kind: str) -> ApiError:
    return ApiError(
        ErrorCode.VALIDATION_FAILED, 422, errors=[{"loc": ["body", field], "type": kind}]
    )


def validate_interests(interests: list[str]) -> list[str]:
    if len(interests) > MAX_INTERESTS or len(set(interests)) != len(interests):
        raise _invalid("interests", "invalid_selection")
    if any(code not in INTEREST_CODES for code in interests):
        raise _invalid("interests", "unknown_value")
    return [code for code in INTEREST_CODES if code in interests]  # stable order


class ProfileService:
    def __init__(self, default_locale: str) -> None:
        self._default_locale = default_locale

    def effective_locale(self, user_locale: str | None, accept_language: str | None) -> str:
        return resolve_locale(user_locale, accept_language, self._default_locale)

    async def get(
        self, uow: UnitOfWork, user_id: uuid.UUID, accept_language: str | None
    ) -> ProfileOut:
        row = await repository.get_profile(uow.session, uow.org_id, user_id)
        return self._out(row, accept_language)

    def _out(self, row: repository.ProfileRow, accept_language: str | None) -> ProfileOut:
        return ProfileOut(
            profession_id=row.profession_id,
            ai_level=row.ai_level,
            interests=row.interests,
            department_id=row.department_id,
            locale=row.locale,
            effective_locale=self.effective_locale(row.locale, accept_language),
            onboarding_started_at=row.onboarding_started_at,
            onboarding_completed_at=row.onboarding_completed_at,
        )

    async def update(
        self, uow: UnitOfWork, user_id: uuid.UUID, body: ProfileIn, accept_language: str | None
    ) -> ProfileOut:
        given = body.model_fields_set
        current = await repository.get_profile(uow.session, uow.org_id, user_id)

        profession_id = body.profession_id if "profession_id" in given else current.profession_id
        ai_level = body.ai_level if "ai_level" in given else current.ai_level
        interests = current.interests
        if "interests" in given:
            interests = validate_interests(body.interests or [])

        if (
            "profession_id" in given
            and profession_id is not None
            and not await repository.profession_exists(uow.session, uow.org_id, profession_id)
        ):
            raise _invalid("profession_id", "unknown_value")
        if "ai_level" in given and ai_level is not None and ai_level not in AI_LEVELS:
            raise _invalid("ai_level", "unknown_value")
        if "locale" in given and body.locale is not None and body.locale not in SUPPORTED_LOCALES:
            raise _invalid("locale", "unknown_value")
        if (
            "department_id" in given
            and body.department_id is not None
            and not await repository.department_exists(uow.session, uow.org_id, body.department_id)
        ):
            raise _invalid("department_id", "unknown_value")

        await repository.save_profile(
            uow.session,
            uow.org_id,
            user_id,
            profession_id=profession_id,
            ai_level=ai_level,
            interests=interests,
        )
        await repository.set_user_choices(
            uow.session,
            uow.org_id,
            user_id,
            locale=body.locale,
            department_id=body.department_id,
            set_locale="locale" in given,
            set_department="department_id" in given,
        )
        result = await repository.get_profile(uow.session, uow.org_id, user_id)
        await uow.commit()
        return self._out(result, accept_language)

    async def complete(
        self, uow: UnitOfWork, user_id: uuid.UUID, accept_language: str | None
    ) -> ProfileOut:
        current = await repository.get_profile(uow.session, uow.org_id, user_id)
        missing: list[str] = []
        if current.profession_id is None:
            missing.append("profession_id")
        if not current.interests:
            missing.append("interests")
        if current.locale is None:
            missing.append("locale")
        if current.ai_level is None:
            missing.append("ai_level")
        if missing:
            raise ApiError(ErrorCode.ONBOARDING_INCOMPLETE, 409, missing=missing)
        await repository.complete_onboarding(uow.session, uow.org_id, user_id)
        result = await repository.get_profile(uow.session, uow.org_id, user_id)
        await uow.commit()
        return self._out(result, accept_language)

    async def professions(self, uow: UnitOfWork) -> list[repository.ProfessionRow]:
        return await repository.list_professions(uow.session, uow.org_id)

    async def departments(self, uow: UnitOfWork) -> list[repository.DepartmentRow]:
        return await repository.list_departments(uow.session, uow.org_id)


def options() -> dict[str, Any]:
    return {
        "ai_levels": list(AI_LEVELS),
        "interests": list(INTEREST_CODES),
        "locales": list(SUPPORTED_LOCALES),
    }
