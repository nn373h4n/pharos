"""Counting the ratings and deciding which model clears the bar.

Only numbers leave this module: no article, no model output, no comment and no name of a rater.
A model is recommended only if it clears the bar in every cell (profession and language) it was
run on; among those the cheapest wins. If none does, the report says so and names the first cells
that failed. The result holds for the prompt versions it prints, and for nothing else.
"""

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from itertools import combinations
from typing import Any

from pharos.evaluation.ratings import Rating

CRITERIA = ("fluency", "accuracy", "usefulness")
ENV_NAMES = {
    "filter": "PHAROS_LLM_MODEL_FILTER",
    "enrichment": "PHAROS_LLM_MODEL_ENRICHMENT",
    "insight": "PHAROS_LLM_MODEL_INSIGHT",
}
DISAGREEMENT = 2.0  # raters this far apart on average are flagged


@dataclass(frozen=True, slots=True)
class Bar:
    """The plan's defaults (D6); every one is a flag of the command."""

    mean: float = 4.0
    safe_share: float = 0.9
    checked_share: float = 0.9
    filter_accuracy: float = 0.9
    min_items: int = 30  # distinct articles rated in each profession and language
    min_raters: int = 2  # in each language


@dataclass(slots=True)
class Report:
    markdown: str
    recommendations: dict[str, str | None]
    failures: dict[tuple[str, str], list[str]] = field(default_factory=dict)


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _verdict_cells(
    answers: Sequence[Mapping[str, Any]],
    ratings: Mapping[str, list[Rating]],
    stage: str,
    model: str,
    bar: Bar,
) -> tuple[list[dict[str, Any]], list[str]]:
    """The numbers of every (profession, language) cell of one model, and why it fails."""
    cells: dict[tuple[str, str], list[Mapping[str, Any]]] = defaultdict(list)
    for answer in answers:
        if answer["stage"] == stage and answer["model"] == model:
            cells[(answer["profession"], answer["language"])].append(answer)
    rows: list[dict[str, Any]] = []
    why: list[str] = []
    for (profession, language), group in sorted(cells.items()):
        given = [r for a in group for r in ratings.get(a["code"], [])]
        rated_items = {a["code"] for a in group if a["code"] in ratings}
        row: dict[str, Any] = {
            "profession": profession,
            "language": language,
            "answers": len(group),
            "rated": len(rated_items),
            "checked": sum(1 for a in group if a["checked"]) / len(group),
            "failed": sum(1 for a in group if a["failed"]),
            "safe": _mean([1.0 if r.safe else 0.0 for r in given]),
            **{name: _mean([getattr(r, name) for r in given]) for name in CRITERIA},
        }
        rows.append(row)
        cell = f"{profession}/{language}"
        if len(rated_items) < bar.min_items:
            why.append(f"{cell}: only {len(rated_items)} articles rated (need {bar.min_items})")
            continue
        for name in CRITERIA:
            if row[name] < bar.mean:
                why.append(f"{cell}: mean {name} {row[name]:.2f} is below {bar.mean}")
        if row["safe"] < bar.safe_share:
            why.append(f"{cell}: only {row['safe']:.0%} judged safe")
        if row["checked"] < bar.checked_share:
            why.append(f"{cell}: only {row['checked']:.0%} passed the product's own checks")
    return rows, why


def _agreement(
    ratings: Sequence[Rating], answers: Sequence[Mapping[str, Any]]
) -> dict[str, list[tuple[str, str, float, int]]]:
    """Per language, per pair of raters, the mean absolute difference over the criteria they both
    rated. Raters are named R1, R2, ... in order, never by the name they gave."""
    language_of = {a["code"]: a["language"] for a in answers}
    names = sorted({r.rater.casefold() for r in ratings})
    label = {name: f"R{n}" for n, name in enumerate(names, start=1)}
    by_language: dict[str, dict[str, dict[str, Rating]]] = defaultdict(lambda: defaultdict(dict))
    for r in ratings:
        by_language[language_of.get(r.code, "?")][r.code][r.rater.casefold()] = r
    result: dict[str, list[tuple[str, str, float, int]]] = {}
    for language, per_code in sorted(by_language.items()):
        sums: dict[tuple[str, str], list[float]] = defaultdict(list)
        for given in per_code.values():
            for a, b in combinations(sorted(given), 2):
                sums[(a, b)].extend(
                    abs(getattr(given[a], name) - getattr(given[b], name)) for name in CRITERIA
                )
        result[language] = [
            (label[a], label[b], _mean(diffs), len(diffs) // len(CRITERIA))
            for (a, b), diffs in sorted(sums.items())
        ]
    return result


def build(
    answers: Sequence[Mapping[str, Any]],
    meta: Mapping[str, Any],
    ratings: Sequence[Rating],
    bar: Bar | None = None,
) -> Report:
    bar = bar or Bar()
    by_code: dict[str, list[Rating]] = defaultdict(list)
    for r in ratings:
        by_code[r.code].append(r)
    languages = sorted({a["language"] for a in answers if a["language"]})
    raters_per_language: dict[str, set[str]] = defaultdict(set)
    language_of = {a["code"]: a["language"] for a in answers}
    for r in ratings:
        raters_per_language[language_of.get(r.code, "?")].add(r.rater.casefold())

    lines: list[str] = []
    recommendations: dict[str, str | None] = {}
    failures: dict[tuple[str, str], list[str]] = {}
    costs: Mapping[str, int] = meta.get("costs_micro_usd", {})

    lines.append(f"# Evaluation of the model output, {meta.get('created', 'undated')}")
    lines.append("")
    if meta.get("provider") == "fake":
        lines += ["> **This run used the fake provider. It is not an evaluation.**", ""]
    if meta.get("incomplete"):
        lines += ["> **The run stopped at its cost cap; models are compared on partial data.**", ""]
    versions = ", ".join(
        f"{task} {v}" for task, v in sorted(meta.get("prompt_versions", {}).items())
    )
    lines += [
        f"Provider: {meta.get('provider', 'unknown')}. Prompt versions: {versions}. "
        "The result holds for these prompts only; run it again when one changes.",
        "",
        f"The bar: mean at least {bar.mean} on fluency, accuracy and usefulness in every "
        f"profession and language; at least {bar.safe_share:.0%} judged safe and "
        f"{bar.checked_share:.0%} passing the product's own checks; at least {bar.min_items} "
        f"articles rated in each profession and language, by at least {bar.min_raters} raters "
        f"in each language; the cheap filter at least {bar.filter_accuracy:.0%} right.",
        "",
    ]
    for language in languages:
        n = len(raters_per_language.get(language, set()))
        if n < bar.min_raters:
            lines.append(f"- {language}: {n} rater(s); at least {bar.min_raters} needed.")
    lines.append("")

    for stage in ("filter", "enrichment", "insight"):
        models = (meta.get("plan") or {}).get(stage, [])
        if not models:
            continue
        lines += [f"## {stage}", ""]
        clearing: list[tuple[float, str]] = []
        lines += ["| model | cost per 100 answers (USD) | clears the bar |", "| --- | --- | --- |"]
        details: list[str] = []
        for model in models:
            group = [a for a in answers if a["stage"] == stage and a["model"] == model]
            cost = costs.get(f"{stage}:{model}", 0) / max(1, len(group)) * 100 / 1_000_000
            if stage == "filter":
                accuracy = _mean([1.0 if a["correct"] else 0.0 for a in group])
                why = (
                    [] if accuracy >= bar.filter_accuracy
                    else [f"accuracy {accuracy:.0%} is below {bar.filter_accuracy:.0%}"]
                )  # fmt: skip
                if meta.get("incomplete") and not group:
                    why.append("no answers (the run stopped first)")
                details.append(
                    f"- `{model}`: filter accuracy {accuracy:.1%} on {len(group)} articles."
                )
            else:
                rows, why = _verdict_cells(answers, by_code, stage, model, bar)
                for language in languages:
                    if len(raters_per_language.get(language, set())) < bar.min_raters:
                        why.append(f"{language}: fewer than {bar.min_raters} raters")
                if not rows:
                    why.append("no answers (the run stopped first)")
                details += [
                    f"| `{model}` | {r['profession']} | {r['language']} | {r['answers']} | "
                    f"{r['rated']} | {r['fluency']:.2f} | {r['accuracy']:.2f} | "
                    f"{r['usefulness']:.2f} | {r['safe']:.0%} | {r['checked']:.0%} | "
                    f"{r['failed']} |"
                    for r in rows
                ]
            failures[(stage, model)] = why
            if not why:
                clearing.append((cost, model))
            lines.append(f"| `{model}` | {cost:.4f} | {'yes' if not why else 'no'} |")
        lines.append("")
        if stage == "filter":
            lines += [*details, ""]
        else:
            lines += [
                "| model | profession | language | answers | rated | fluency | accuracy | "
                "usefulness | safe | checked | failed |",
                "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
                *details,
                "",
            ]
        chosen = min(clearing)[1] if clearing else None
        recommendations[stage] = chosen
        if chosen is None:
            lines.append("**No model clears the bar at this stage.** The first reasons:")
            for model in models:
                for reason in failures[(stage, model)][:3]:
                    lines.append(f"- `{model}`: {reason}")
        else:
            lines.append(f"**Recommended: `{chosen}`** (the cheapest that clears the bar).")
        lines.append("")

    agreement = _agreement(ratings, answers)
    if agreement:
        lines += ["## Agreement between raters", ""]
        for language, pairs in agreement.items():
            for first, second, diff, shared in pairs:
                flag = " **(far apart)**" if diff >= DISAGREEMENT else ""
                lines.append(
                    f"- {language}, {first} and {second}: mean difference {diff:.2f} "
                    f"over {shared} answers{flag}"
                )
        lines.append("")

    usable = meta.get("provider") != "fake" and not meta.get("incomplete")
    chosen_lines = [
        f"{ENV_NAMES[stage]}={model}" for stage, model in recommendations.items() if model
    ]
    lines += ["## What to set", ""]
    if usable and chosen_lines:
        lines += ["In the worker's `.env`:", "", "```", *chosen_lines, "```", ""]
    else:
        lines += ["Nothing: there is no result that can be used to choose a model yet.", ""]
    if not usable:
        recommendations = {stage: None for stage in recommendations}
    return Report("\n".join(lines) + "\n", recommendations, failures)
