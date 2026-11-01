"""Importing the ratings of the raters, strictly and all at once.

Every problem is reported with its sheet and row, and nothing is kept unless there are none. The
comments are read for their column but never stored or printed. A sheet that names a model is
refused: the point of blinding is lost the moment a rater can see one.
"""

import csv
import json
from dataclasses import asdict, dataclass
from pathlib import Path

from pharos.evaluation.sheet import COLUMNS, RATED

MAX_RATER = 40
MAX_FILE_BYTES = 5_000_000
MAX_ROWS = 20_000
_MARKS = ("fluency", "accuracy", "usefulness")
_FORBIDDEN_COLUMNS = {"model", "stage", "provider", "cost"}


class RatingsError(ValueError):
    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems[:20]))
        self.problems = problems


@dataclass(frozen=True, slots=True)
class Rating:
    code: str
    rater: str
    fluency: int
    accuracy: int
    usefulness: int
    safe: bool


def _mark(value: str) -> int | None:
    value = value.strip()
    return int(value) if value in {"1", "2", "3", "4", "5"} else None


def parse(paths: list[Path], known_codes: set[str]) -> list[Rating]:
    problems: list[str] = []
    found: list[Rating] = []
    seen: set[tuple[str, str]] = set()
    for path in paths:
        try:
            too_big = path.stat().st_size > MAX_FILE_BYTES
            handle = path.open(encoding="utf-8-sig", newline="")
        except OSError:
            problems.append(f"{path.name}: cannot be read")
            continue
        if too_big:
            handle.close()
            problems.append(f"{path.name}: the file is too large")
            continue
        with handle:
            reader = csv.DictReader(handle)
            header = set(reader.fieldnames or [])
            if header & _FORBIDDEN_COLUMNS:
                problems.append(f"{path.name}: the sheet names a model; it was not blind")
                continue
            if not set(COLUMNS) <= header:
                problems.append(f"{path.name}: columns missing: {sorted(set(COLUMNS) - header)}")
                continue
            for number, row in enumerate(reader, start=2):
                where = f"{path.name} row {number}"
                if number > MAX_ROWS:
                    problems.append(f"{path.name}: more than {MAX_ROWS} rows")
                    break
                code = (row["code"] or "").strip()
                cells = [(row[name] or "").strip() for name in (*_MARKS, "safe")]
                rater = (row["rater"] or "").strip()
                if not any(cells) and not rater:
                    continue  # not rated yet
                if code not in known_codes:
                    problems.append(f"{where}: unknown code")
                    continue
                if not rater or len(rater) > MAX_RATER:
                    problems.append(f"{where}: the rater's name is missing or too long")
                    continue
                marks = [_mark(row[name] or "") for name in _MARKS]
                safe = (row["safe"] or "").strip().casefold()
                if None in marks:
                    problems.append(f"{where}: fluency, accuracy and usefulness must be 1 to 5")
                    continue
                if safe not in {"yes", "no"}:
                    problems.append(f"{where}: safe must be yes or no")
                    continue
                key = (rater.casefold(), code)
                if key in seen:
                    problems.append(f"{where}: this rater already rated this code")
                    continue
                seen.add(key)
                fluency, accuracy, usefulness = (m for m in marks if m is not None)
                found.append(Rating(code, rater, fluency, accuracy, usefulness, safe == "yes"))
    if problems:
        raise RatingsError(problems)
    if not found:
        raise RatingsError(["no ratings found"])
    return found


def save(ratings: list[Rating], directory: Path) -> None:
    (directory / "ratings.json").write_text(
        json.dumps([asdict(r) for r in ratings], indent=2) + "\n", encoding="utf-8"
    )


def load(directory: Path) -> list[Rating]:
    """The imported ratings, checked again: the file is one a person could have edited."""
    try:
        raw = json.loads((directory / "ratings.json").read_text(encoding="utf-8"))
        found = [Rating(**entry) for entry in raw]
    except (ValueError, TypeError, KeyError):
        raise RatingsError(["ratings.json is not a file this tool wrote; import again"]) from None
    for r in found:
        marks = (r.fluency, r.accuracy, r.usefulness)
        if (
            not isinstance(r.code, str)
            or not isinstance(r.rater, str)
            or not all(type(m) is int and 1 <= m <= 5 for m in marks)
            or not isinstance(r.safe, bool)
        ):
            raise RatingsError(["ratings.json holds a mark outside 1 to 5; import again"])
    return found


__all__ = ["RATED", "Rating", "RatingsError", "load", "parse", "save"]
