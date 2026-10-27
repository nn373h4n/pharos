import pytest

from pharos.core.errors import ApiError
from pharos.profile.catalog import AI_LEVELS, INTEREST_CODES, MAX_INTERESTS
from pharos.profile.service import validate_interests


def test_the_catalogue_fits_the_database_limits() -> None:
    # user_profiles_interests_check: an array of at most 12 strings; ai_level check.
    assert MAX_INTERESTS == 12
    assert len(set(INTEREST_CODES)) == len(INTEREST_CODES)
    assert AI_LEVELS == ("beginner", "intermediate", "advanced")
    for code in INTEREST_CODES:
        assert code.replace("_", "").isalpha()
        assert code == code.lower()


def test_interests_are_validated_and_kept_in_catalogue_order() -> None:
    assert validate_interests(["coding", "writing"]) == ["writing", "coding"]
    assert validate_interests([]) == []
    assert validate_interests(list(INTEREST_CODES)) == list(INTEREST_CODES)


@pytest.mark.parametrize(
    "bad",
    [["unknown"], ["writing", "writing"], ["Writing"], [""], [*INTEREST_CODES, "writing"]],
)
def test_bad_interests_are_refused_with_the_field_named(bad: list[str]) -> None:
    with pytest.raises(ApiError) as caught:
        validate_interests(bad)
    assert caught.value.status == 422
    assert caught.value.extra["errors"][0]["loc"] == ["body", "interests"]
