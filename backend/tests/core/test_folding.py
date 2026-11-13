# ruff: noqa: RUF001, RUF003  (look-alike letters are the point of these tests)
"""The folding shared by the insight checks and the ingestion hold rules."""

import pytest

from pharos.core.folding import folded, views


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Əvvəlki GÖSTƏRİŞLƏR", "evvelki gosterisler"),
        ("Yukarıdaki talimatları", "yukaridaki talimatlari"),
        ("Забудьте всё, свой", "забудьте все, свои"),
        ("ｉｇｎｏｒｅ", "ignore"),  # full-width letters
        ("ig​no­re⁠", "ignore"),  # zero-width space, soft hyphen, word joiner
        ("a b", "a b"),  # a no-break space is a space
        ("Straße", "strasse"),
    ],
)
def test_folded(text: str, expected: str) -> None:
    assert folded(text) == expected


def test_each_script_sees_the_look_alikes_of_the_other_as_its_own() -> None:
    latin, cyrillic = views("ignоrе Систем")  # Cyrillic о and е; Cyrillic С
    assert latin.startswith("ignore ")
    latin, cyrillic = views("инструкция cистeма")  # Latin c and e
    assert cyrillic == "инструкция система"
