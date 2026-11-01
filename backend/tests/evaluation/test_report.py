# ruff: noqa: E501  (hand-made tables of ratings read best on one line)
"""The arithmetic of the report on ratings made by hand, so every number can be checked."""

import json
from pathlib import Path
from typing import Any

import pytest

from pharos.evaluation.ratings import Rating
from pharos.evaluation.report import Bar, build

SMALL = Bar(min_items=2, min_raters=2)


def answer(code: str, model: str, language: str = "az", *, profession: str = "accountant",
           stage: str = "insight", checked: bool = True, failed: str | None = None) -> dict[str, Any]:  # fmt: skip
    return {
        "code": code, "stage": stage, "model": model, "item": f"i-{code}", "language": language,
        "profession": profession, "output": {"summary": "ARTICLE-TEXT-CANARY"}, "checked": checked,
        "correct": None, "failed": failed,
    }  # fmt: skip


def meta(models: list[str], **changes: Any) -> dict[str, Any]:
    return {
        "created": "2026-11-01", "provider": "anthropic", "languages": ["az"],
        "plan": {"insight": models}, "prompt_versions": {"insight": "v2", "enrich": "v1"},
        "costs_micro_usd": {f"insight:{m}": cost for m, cost in zip(models, (4000, 1000, 2000), strict=False)},
        "incomplete": False, **changes,
    }  # fmt: skip


def marks(code: str, rater: str, f: int, a: int, u: int, safe: bool = True) -> Rating:
    return Rating(code, rater, f, a, u, safe)


def everyone_rates(codes: list[str], *, f: int, a: int, u: int, safe: bool = True) -> list[Rating]:
    return [marks(c, r, f, a, u, safe) for c in codes for r in ("Anna", "Vusal")]


def test_means_are_over_every_rating_of_the_cell() -> None:
    answers = [answer("a", "m"), answer("b", "m")]
    ratings = [
        marks("a", "Anna", 5, 4, 3), marks("a", "Vusal", 3, 4, 5),
        marks("b", "Anna", 4, 4, 4), marks("b", "Vusal", 4, 4, 4, safe=False),
    ]  # fmt: skip
    report = build(answers, meta(["m"]), ratings, SMALL)
    assert (
        "| `m` | accountant | az | 2 | 2 | 4.00 | 4.00 | 4.00 | 75% | 100% | 0 |" in report.markdown
    )


def test_a_model_that_clears_the_bar_is_recommended_and_the_env_lines_are_given() -> None:
    codes = ["a", "b"]
    answers = [answer(c, "m") for c in codes]
    report = build(answers, meta(["m"]), everyone_rates(codes, f=4, a=5, u=4), SMALL)
    assert report.recommendations["insight"] == "m"
    assert "PHAROS_LLM_MODEL_INSIGHT=m" in report.markdown
    assert "prompt versions: " in report.markdown.lower() and "insight v2" in report.markdown


def test_the_cheapest_model_that_clears_the_bar_wins_and_a_dearer_one_does_not() -> None:
    codes = ["a", "b"]
    answers = [answer(c, m) for m in ("dear", "cheap") for c in codes]
    answers = [{**a, "code": f"{a['code']}-{a['model']}"} for a in answers]
    ratings = [
        r
        for a in answers
        for r in (marks(a["code"], "Anna", 5, 5, 5), marks(a["code"], "Vusal", 5, 5, 5))
    ]
    report = build(answers, meta(["dear", "cheap"]), ratings, SMALL)
    assert report.recommendations["insight"] == "cheap"  # costs 1000 against 4000


@pytest.mark.parametrize(
    ("marks_given", "safe", "checked", "why"),
    [
        ((3, 5, 5), True, True, "mean fluency 3.00 is below 4.0"),
        ((5, 3, 5), True, True, "mean accuracy 3.00"),
        ((5, 5, 3), True, True, "mean usefulness 3.00"),
        ((5, 5, 5), False, True, "judged safe"),
        ((5, 5, 5), True, False, "passed the product's own checks"),
    ],
)
def test_one_weak_criterion_keeps_a_cheap_model_out(
    marks_given: tuple[int, int, int], safe: bool, checked: bool, why: str
) -> None:
    codes = ["a", "b"]
    answers = [answer(c, "m", checked=checked) for c in codes]
    ratings = everyone_rates(codes, f=marks_given[0], a=marks_given[1], u=marks_given[2], safe=safe)
    report = build(answers, meta(["m"]), ratings, SMALL)
    assert report.recommendations["insight"] is None
    assert any(why in reason for reason in report.failures[("insight", "m")])
    assert (
        "No model clears the bar" in report.markdown and "PHAROS_LLM_MODEL" not in report.markdown
    )


def test_every_profession_and_language_must_clear_it_not_the_average() -> None:
    answers = [
        answer("a", "m", "az"),
        answer("b", "m", "az"),
        answer("c", "m", "tr"),
        answer("d", "m", "tr"),
    ]
    good = everyone_rates(["a", "b"], f=5, a=5, u=5)
    poor = everyone_rates(["c", "d"], f=2, a=2, u=2)
    report = build(answers, meta(["m"], languages=["az", "tr"]), good + poor, SMALL)
    assert report.recommendations["insight"] is None
    assert any("accountant/tr" in r for r in report.failures[("insight", "m")])
    assert not any("accountant/az" in r for r in report.failures[("insight", "m")])  # fine in az


def test_too_few_articles_or_raters_is_not_enough_however_good_the_marks() -> None:
    answers = [answer("a", "m")]
    report = build(answers, meta(["m"]), everyone_rates(["a"], f=5, a=5, u=5), SMALL)
    assert any("only 1 articles rated" in r for r in report.failures[("insight", "m")])
    answers = [answer("a", "m"), answer("b", "m")]
    one_rater = [marks(c, "Anna", 5, 5, 5) for c in "ab"]
    report = build(answers, meta(["m"]), one_rater, SMALL)
    assert report.recommendations["insight"] is None
    assert any("fewer than 2 raters" in r for r in report.failures[("insight", "m")])


def test_a_failed_call_counts_against_the_checks() -> None:
    answers = [
        answer("a", "m"),
        answer("b", "m"),
        answer("c", "m", failed="provider_failed", checked=False),
    ]
    report = build(answers, meta(["m"]), everyone_rates(["a", "b"], f=5, a=5, u=5), SMALL)
    assert any("passed the product's own checks" in r for r in report.failures[("insight", "m")])


def test_the_filter_is_judged_by_accuracy_against_the_labels() -> None:
    right = [
        {**answer(f"f{i}", "good", stage="filter"), "language": None, "correct": True}
        for i in range(9)
    ]
    wrong = [{**answer("w", "good", stage="filter"), "language": None, "correct": False}]
    m = {**meta([]), "plan": {"filter": ["good"]}, "costs_micro_usd": {"filter:good": 10}}
    assert build(right + wrong, m, [], SMALL).recommendations["filter"] == "good"  # 90%
    assert build(right[:8] + wrong, m, [], SMALL).recommendations["filter"] is None  # 88.9%


def test_a_fake_provider_or_an_incomplete_run_gives_no_advice() -> None:
    codes = ["a", "b"]
    answers = [answer(c, "m") for c in codes]
    ratings = everyone_rates(codes, f=5, a=5, u=5)
    fake = build(answers, meta(["m"], provider="fake"), ratings, SMALL)
    assert "not an evaluation" in fake.markdown and "PHAROS_LLM_MODEL" not in fake.markdown
    assert fake.recommendations["insight"] is None
    partial = build(answers, meta(["m"], incomplete=True), ratings, SMALL)
    assert "cost cap" in partial.markdown and "PHAROS_LLM_MODEL" not in partial.markdown


def test_raters_far_apart_are_flagged_and_named_only_by_number() -> None:
    answers = [answer("a", "m"), answer("b", "m")]
    ratings = [marks("a", "Anna", 5, 5, 5), marks("a", "Vusal", 2, 2, 2),
               marks("b", "Anna", 5, 5, 5), marks("b", "Vusal", 2, 2, 2)]  # fmt: skip
    report = build(answers, meta(["m"]), ratings, SMALL)
    assert "az, R1 and R2: mean difference 3.00 over 2 answers **(far apart)**" in report.markdown
    close = build(answers, meta(["m"]), everyone_rates(["a", "b"], f=4, a=4, u=4), SMALL)
    assert "mean difference 0.00" in close.markdown and "far apart" not in close.markdown


def test_the_report_holds_only_numbers_no_text_no_comment_no_name() -> None:
    answers = [answer("a", "m"), answer("b", "m")]
    ratings = [marks(c, name, 5, 5, 5) for c in "ab" for name in ("Anna-Marie", "Vusal-Quliyev")]
    markdown = build(answers, meta(["m"]), ratings, SMALL).markdown
    for forbidden in ("ARTICLE-TEXT-CANARY", "Anna", "Vusal", "Marie", "Quliyev"):
        assert forbidden not in markdown


def test_a_tampered_ratings_file_is_refused_not_counted(tmp_path: Path) -> None:
    from pharos.evaluation import ratings

    def write(entry: dict[str, object]) -> None:
        (tmp_path / "ratings.json").write_text(json.dumps([entry]), encoding="utf-8")

    base = {"code": "c1", "rater": "r", "fluency": 5, "accuracy": 5, "usefulness": 5, "safe": True}
    write(base)
    assert len(ratings.load(tmp_path)) == 1  # positive control
    for change in ({"fluency": True}, {"code": 5}, {"rater": None}, {"safe": 1}):
        write({**base, **change})
        with pytest.raises(ratings.RatingsError):
            ratings.load(tmp_path)
