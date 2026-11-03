"""Tuning the near-duplicate threshold on labelled pairs (M4-05): a strict file, honest numbers,
and the product's own gateway, so a run costs what an installation would pay."""

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from pharos.core.config import get_settings
from pharos.evaluation import __main__ as cli
from pharos.evaluation import dedup
from pharos.evaluation.corpus import CorpusError
from pharos.llm.embedding import DemoEmbedder
from pharos.llm.errors import BudgetExhausted


def side(title: str, excerpt: str = "An excerpt.") -> dict[str, str]:
    return {"title": title, "excerpt": excerpt, "url": "https://news.example.org/a"}


SAME = ("Acme ships Widget 2 with faster sync", "Faster sync: Acme ships Widget 2")
OTHER = ("City council approves bicycle lane budget", "New lanes for the city")


def entry(ident: str, a: str, b: str, same: bool) -> dict[str, Any]:
    return {"id": ident, "a": side(a), "b": side(b), "same": same}


def good() -> list[dict[str, Any]]:
    return [
        entry("p1", SAME[0], SAME[1], True),
        entry("p2", SAME[0], OTHER[0], False),
        entry("p3", OTHER[0], OTHER[1] + " tonight", True),
    ]


class TestTheFileIsStrict:
    def test_a_good_file_loads(self, tmp_path: Path) -> None:
        path = tmp_path / "pairs.json"
        path.write_text(json.dumps(good()), encoding="utf-8")
        assert [p.id for p in dedup.load_pairs(path)] == ["p1", "p2", "p3"]

    @pytest.mark.parametrize(
        "change",
        [
            lambda e: e[0].pop("same"),
            lambda e: e[0].update(extra=1),
            lambda e: e[0].update(same="yes"),
            lambda e: e[0].update(id=""),
            lambda e: e[0].update(id="x" * 41),
            lambda e: e[1].update(id="p1"),  # not unique
            lambda e: e[0]["a"].update(url="http://127.0.0.1/x"),  # inside a network
            lambda e: e[0]["a"].update(url="javascript:alert(1)"),
            lambda e: e[0]["a"].update(title="a‮b"),  # hidden characters
            lambda e: e[0]["a"].update(title="<b>bold</b>"),
            lambda e: e[0]["a"].update(title="t" * 301),
            lambda e: e[0]["a"].pop("excerpt"),
            lambda e: e[0].update(a="not an article"),
            lambda e: [x.update(same=True) for x in e],  # needs both kinds
            lambda e: [x.update(same=False) for x in e],
        ],
    )
    def test_anything_else_is_refused(self, change: Any) -> None:
        entries = good()
        change(entries)
        with pytest.raises(CorpusError):
            dedup.parse_pairs(entries)

    def test_the_size_is_bounded_and_garbage_is_a_message(self, tmp_path: Path) -> None:
        many = [entry(f"p{i}", SAME[0], SAME[1], i % 2 == 0) for i in range(dedup.MAX_PAIRS + 1)]
        with pytest.raises(CorpusError, match="at most"):
            dedup.parse_pairs(many)
        broken = tmp_path / "broken.json"
        broken.write_text("{not json", encoding="utf-8")
        with pytest.raises(CorpusError, match="cannot read"):
            dedup.load_pairs(broken)
        with pytest.raises(CorpusError):
            dedup.load_pairs(tmp_path / "missing.json")


def test_the_sample_file_in_the_guides_is_a_valid_pairs_file() -> None:
    sample = Path(__file__).resolve().parents[3] / "docs" / "guides" / "dedup-pairs.sample.json"
    pairs = dedup.load_pairs(sample)
    assert len(pairs) >= 4 and any(p.same for p in pairs) and not all(p.same for p in pairs)


class TestTheNumbers:
    def test_each_threshold_counts_joins_mistakes_and_misses(self) -> None:
        pairs = dedup.parse_pairs([*good(), entry("p4", SAME[0], OTHER[1], False)])
        # similarities: a same pair at 0.95, a different pair at 0.91, a same pair at 0.70,
        # a different pair at 0.20
        rows = dedup.table([0.95, 0.91, 0.70, 0.20], pairs, [0.90, 0.80, 0.96])
        by = {row.threshold: row for row in rows}
        assert (by[0.90].joined, by[0.90].right, by[0.90].wrong, by[0.90].missed) == (2, 1, 1, 1)
        assert (by[0.96].joined, by[0.96].wrong, by[0.96].missed) == (0, 0, 2)
        assert (by[0.80].joined, by[0.80].wrong, by[0.80].missed) == (2, 1, 1)
        assert by[0.90].precision == 0.5 and by[0.90].recall == 0.5
        assert by[0.96].precision is None  # nothing joined: no precision to speak of

    def test_a_pair_exactly_at_the_threshold_is_joined(self) -> None:
        pairs = dedup.parse_pairs(good())
        [row] = dedup.table([0.9, 0.2, 0.5], pairs, [0.9])
        assert (row.joined, row.right, row.wrong, row.missed) == (1, 1, 0, 1)

    def test_the_report_is_numbers_and_no_text_of_any_article(self) -> None:
        pairs = dedup.parse_pairs(good())
        text = dedup.render(dedup.table([0.9, 0.2, 0.8], pairs, [0.85]), pairs)
        assert "0.85" in text and "wrong" in text
        for forbidden in ("Acme", "Widget", "council", "https://"):
            assert forbidden not in text

    def test_vectors_of_different_sizes_are_never_compared(self) -> None:
        with pytest.raises(ValueError, match="different sizes"):
            dedup.cosine([1.0, 0.0], [1.0, 0.0, 0.0])


class TestThroughTheGateway:
    def test_a_rewritten_copy_is_closer_than_another_story(self) -> None:
        pairs = dedup.parse_pairs(good())
        closeness = asyncio.run(
            dedup.similarities(
                pairs, DemoEmbedder(), "m", max_cost_micro_usd=10**9, embedding_price=0
            )
        )
        assert closeness[0] > 0.6 > closeness[1] + 0.1

    def test_the_cost_cap_stops_a_run(self) -> None:
        pairs = dedup.parse_pairs(good())
        with pytest.raises(BudgetExhausted):
            asyncio.run(
                dedup.similarities(
                    pairs, DemoEmbedder(), "m", max_cost_micro_usd=1, embedding_price=50
                )
            )


class TestTheCommand:
    def run(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        *extra: str,
        provider: str = "fake",
    ) -> tuple[int, str, str]:
        monkeypatch.setenv("PHAROS_ENVIRONMENT", "test")
        monkeypatch.setenv("PHAROS_EMBEDDING_PROVIDER", provider)
        monkeypatch.setenv("PHAROS_EMBEDDING_MODEL", "stand-in")
        get_settings.cache_clear()
        path = tmp_path / "pairs.json"
        path.write_text(json.dumps(good()), encoding="utf-8")
        try:
            code = cli.main(["dedup", "--pairs", str(path), *extra])
        finally:
            get_settings.cache_clear()
        captured = capsys.readouterr()
        return code, captured.out, captured.err

    def test_it_prints_a_table_for_the_chosen_thresholds(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code, out, _ = self.run(tmp_path, monkeypatch, capsys, "--thresholds", "0.6,0.9")
        assert code == 0 and "| 0.60 |" in out and "| 0.90 |" in out and "Acme" not in out

    def test_embeddings_that_are_off_are_a_message_not_a_traceback(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code, _, err = self.run(tmp_path, monkeypatch, capsys, provider="off")
        assert code == 1 and "embeddings are off" in err

    @pytest.mark.parametrize("bad", ["nan", "inf", "-1"])
    def test_a_cost_cap_that_is_not_a_sum_of_money_is_refused(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        bad: str,
    ) -> None:
        code, _, err = self.run(tmp_path, monkeypatch, capsys, "--max-cost-usd", bad)
        assert code == 1 and "max-cost-usd" in err

    @pytest.mark.parametrize("bad", ["abc", "0.2", "1", "1.5", ""])
    def test_thresholds_outside_the_useful_range_are_refused(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        bad: str,
    ) -> None:
        code, _, err = self.run(tmp_path, monkeypatch, capsys, "--thresholds", bad)
        assert code == 1 and "thresholds" in err
