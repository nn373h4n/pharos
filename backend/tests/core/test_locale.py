import pytest

from pharos.core.locale import SUPPORTED_LOCALES, resolve_locale


def test_the_supported_languages_are_the_four_of_the_product() -> None:
    assert SUPPORTED_LOCALES == ("en", "az", "tr", "ru")


@pytest.mark.parametrize("saved", SUPPORTED_LOCALES)
def test_the_persons_own_choice_beats_everything(saved: str) -> None:
    assert resolve_locale(saved, "ru,en;q=0.5", "tr") == saved


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("az", "az"),
        ("AZ-az", "az"),
        ("de, tr;q=0.8, en;q=0.5", "tr"),
        ("en;q=0.5, ru;q=0.9", "ru"),
        ("ru;q=0.9, az;q=0.9", "ru"),  # equal weight: the one listed first
        ("tr-TR,tr;q=0.9,en-US;q=0.8", "tr"),
        ("*", "en"),
        ("de, fr;q=0.9", "en"),
        ("az;q=0", "en"),  # q=0 means "not acceptable"
        ("az;q=banana", "en"),
        ("", "en"),
        (None, "en"),
    ],
)
def test_the_browser_decides_when_the_person_has_not(header: str | None, expected: str) -> None:
    assert resolve_locale(None, header, "en") == expected


def test_the_installation_default_is_the_last_resort() -> None:
    assert resolve_locale(None, "de, fr", "az") == "az"
    assert resolve_locale(None, None, "ru") == "ru"


@pytest.mark.parametrize("saved", ["", "de", "EN", "en-US", "../etc"])
def test_an_unsupported_saved_value_is_ignored(saved: str) -> None:
    assert resolve_locale(saved, "tr", "en") == "tr"


def test_a_huge_header_is_not_a_problem() -> None:
    header = ",".join(["de"] * 5000) + ",ru"
    assert resolve_locale(None, header, "en") == "en"  # the useful part is beyond the cut-off
    assert resolve_locale(None, "ru," + "x" * 100_000, "en") == "ru"
