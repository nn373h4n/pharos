"""The rating sheet: what a native-speaking rater sees.

One CSV per language, one row per answer that was written. The model is NOT named: each row has a
random code, rows are shuffled, and the file holds nothing that tells models apart. The rater adds
a name of their own choosing, three marks from 1 to 5 (fluency, accuracy, usefulness) and whether
they would show it to a colleague. UTF-8 with a byte-order mark, so a spreadsheet opens Azerbaijani
letters correctly.
"""

import csv
import random
import secrets
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from pharos.evaluation.corpus import CorpusItem

COLUMNS = (
    "code",
    "profession",
    "article",
    "excerpt",
    "output",
    "rater",
    "fluency",
    "accuracy",
    "usefulness",
    "safe",
    "comment",
)
RATED = ("fluency", "accuracy", "usefulness", "safe")
_FORMULA_STARTS = ("=", "+", "-", "@", "\t", "\r")


def safe_cell(value: str) -> str:
    """A spreadsheet runs a cell that starts with = + - @ as a formula; a leading quote stops it.
    Article and model text are not trusted."""
    return "'" + value if value.startswith(_FORMULA_STARTS) else value


def _output(answer: Mapping[str, Any]) -> str:
    out = answer["output"]
    if answer["stage"] == "enrichment":
        return f"Summary: {out['summary']}\nCategory: {out['category']}"
    steps = "\n".join(f"{n}. {step}" for n, step in enumerate(out["how_to_apply"], start=1))
    return (
        f"Summary: {out['summary']}\nHow it applies:\n{steps}\n"
        f"Prompt to try: {out['try_prompt']}\nNote: {out['safety_note']}"
    )


def rows(
    answers: Sequence[Mapping[str, Any]],
    corpus: Sequence[CorpusItem],
    language: str,
    *,
    seed: int | None = None,
) -> list[list[str]]:
    """The rows of the sheet of one language, blinded and shuffled."""
    by_id = {item.id: item for item in corpus}
    chosen = [
        a
        for a in answers
        if a["language"] == language and a["stage"] != "filter" and a["output"] is not None
    ]
    # The order only has to be unrelated to the model; a seed makes it repeatable in tests.
    order: random.Random = (
        secrets.SystemRandom() if seed is None else random.Random(seed)  # noqa: S311
    )
    order.shuffle(chosen)
    table = []
    for answer in chosen:
        item = by_id[answer["item"]]
        table.append(
            [
                answer["code"],
                answer["profession"],
                safe_cell(item.title),
                safe_cell(item.excerpt),
                safe_cell(_output(answer)),
                "",
                "",
                "",
                "",
                "",
                "",
            ]
        )
    return table


def write(table: Sequence[Sequence[str]], path: Path) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(COLUMNS)
        writer.writerows(table)
