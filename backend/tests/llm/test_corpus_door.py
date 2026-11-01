"""The corpus door checks its own input; it is not a way to turn any string into class A."""

from pathlib import Path

import pytest

from pharos.llm.corpus_door import public_text_from_corpus
from pharos.llm.errors import NotPublicArticle

GOOD = ("A title", "An excerpt.", "https://news.example.org/a", "en")


def test_a_plain_public_entry_is_accepted() -> None:
    assert public_text_from_corpus(*GOOD).is_genuine()


@pytest.mark.parametrize(
    "bad",
    [
        ("A title", "x", "http://10.0.0.5/x", None),
        ("A title", "x", "http://127.1/", None),
        ("A title", "x", "https://user:pw@news.example.org/", None),
        ("A title", "x", "javascript:alert(1)", None),
        ("", "x", "https://news.example.org/", None),
        ("t" * 301, "x", "https://news.example.org/", None),
        ("t", "e" * 501, "https://news.example.org/", None),
        ("a‮b", "x", "https://news.example.org/", None),
        ("t", "<script>x</script>", "https://news.example.org/", None),
    ],
)
def test_anything_else_is_refused(bad: tuple[str, str, str, str | None]) -> None:
    with pytest.raises(NotPublicArticle):
        public_text_from_corpus(*bad)


def test_only_the_evaluation_runner_uses_the_door() -> None:
    root = Path(__file__).resolve().parents[2] / "src" / "pharos"
    users = sorted(
        str(path.relative_to(root))
        for path in root.rglob("*.py")
        if "public_text_from_corpus" in path.read_text()
    )
    assert users == ["evaluation/runner.py", "llm/corpus_door.py"]
