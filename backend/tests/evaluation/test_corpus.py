import json
from pathlib import Path

import pytest

from pharos.evaluation import corpus

GOOD = {
    "id": "a1",
    "title": "A title",
    "excerpt": "An excerpt",
    "url": "https://news.example.org/a1",
    "lang": "en",
    "profession": "accountant",
    "relevant": True,
}


def test_a_good_corpus_loads_and_round_trips(tmp_path: Path) -> None:
    items = corpus.parse([GOOD, {**GOOD, "id": "a2", "lang": None, "relevant": False}])
    assert [i.id for i in items] == ["a1", "a2"] and items[1].relevant is False
    path = tmp_path / "c.json"
    corpus.dump(items, path)
    assert corpus.load(path) == items


@pytest.mark.parametrize(
    "change",
    [
        {"id": ""},
        {"id": "x y"},
        {"id": "x" * 41},
        {"title": ""},
        {"title": "t" * 301},
        {"excerpt": "e" * 501},
        {"url": "javascript:alert(1)"},
        {"url": "https://user:pass@example.org/"},
        {"url": "ftp://example.org/x"},
        {"url": 5},
        {"lang": "fr"},
        {"profession": "Accountant"},
        {"profession": "a b"},
        {"relevant": "yes"},
        {"title": "a\u202eb"},
        {"title": "a\u200bb"},
        {"excerpt": "&lt;script&gt;x"},
        {"title": "<b>bold</b>"},
        {"title": "x\x00y"},
    ],
)
def test_an_entry_that_breaks_a_rule_is_refused(change: dict[str, object]) -> None:
    with pytest.raises(corpus.CorpusError):
        corpus.parse([{**GOOD, **change}])


@pytest.mark.parametrize(
    "raw",
    [[], {}, "x", [1], [{**GOOD, "extra": 1}], [{k: v for k, v in GOOD.items() if k != "id"}]],
)
def test_a_corpus_of_the_wrong_shape_is_refused(raw: object) -> None:
    with pytest.raises(corpus.CorpusError):
        corpus.parse(raw)


def test_ids_must_be_unique_and_the_size_bounded() -> None:
    with pytest.raises(corpus.CorpusError, match="unique"):
        corpus.parse([GOOD, GOOD])
    big = [{**GOOD, "id": f"i{n}"} for n in range(corpus.MAX_ITEMS + 1)]
    with pytest.raises(corpus.CorpusError, match="at most"):
        corpus.parse(big)


def test_a_file_that_cannot_be_read_does_not_leak_its_content(tmp_path: Path) -> None:
    path = tmp_path / "bad.json"
    path.write_text('{"secret": "SENTINEL" ', encoding="utf-8")
    with pytest.raises(corpus.CorpusError) as error:
        corpus.load(path)
    assert "SENTINEL" not in str(error.value)
    with pytest.raises(corpus.CorpusError):
        corpus.load(tmp_path / "missing.json")


def test_the_sample_corpus_of_the_guide_is_valid() -> None:
    path = Path(__file__).resolve().parents[3] / "docs" / "guides" / "evaluation-corpus.sample.json"
    items = corpus.load(path)
    assert len(items) >= 6 and any(not i.relevant for i in items)
    assert json.loads(path.read_text(encoding="utf-8"))  # plain JSON


def test_an_unlabelled_entry_loads_but_a_run_refuses_it() -> None:
    """`relevant: null` is how export-corpus writes an entry nobody has judged yet."""
    items = corpus.parse([{**GOOD, "relevant": None}])
    assert items[0].relevant is None
    with pytest.raises(corpus.CorpusError, match="no label"):
        corpus.require_labels(items)
    corpus.require_labels(corpus.parse([GOOD]))  # positive control
