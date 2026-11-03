"""Tuning the near-duplicate threshold on pairs of articles that a person has labelled (M4-05).

The threshold decides when two articles are one story (ADR-0007, ADR-0021). It depends on the
embedding model and the languages, so it is tuned on the installation's own articles: a file of
pairs, each labelled "same story" or "different stories". For every threshold the report says how
many same-story pairs would be joined (recall), how many joined pairs are really the same story
(precision) and how many different stories would be wrongly joined. A wrong join hides one article
behind another, so the one to watch is the last number. The report holds numbers only.
"""

import json
import math
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from pharos.core.clock import SystemClock
from pharos.evaluation.corpus import MAX_EXCERPT, MAX_FILE_BYTES, MAX_TITLE, CorpusError
from pharos.evaluation.runner import InMemoryBudget
from pharos.llm.corpus_door import check_public_fields, public_text_from_corpus
from pharos.llm.embedding import EmbeddingProvider
from pharos.llm.errors import NotPublicArticle
from pharos.llm.gateway import GatewayConfig, LLMGateway
from pharos.llm.types import Stage
from pharos.llm.usage import UsageRecord

MAX_PAIRS = 400
DEFAULT_THRESHOLDS = (0.80, 0.85, 0.88, 0.90, 0.92, 0.94, 0.96)
_EVAL_ORG = uuid.UUID(int=0)


@dataclass(frozen=True, slots=True)
class Side:
    title: str
    excerpt: str
    url: str


@dataclass(frozen=True, slots=True)
class Pair:
    id: str
    a: Side
    b: Side
    same: bool


@dataclass(frozen=True, slots=True)
class Row:
    threshold: float
    joined: int  # pairs at or above the threshold
    right: int  # of them, the same story
    wrong: int  # of them, different stories (articles that would be hidden behind another)
    missed: int  # same-story pairs below the threshold (shown twice)

    @property
    def precision(self) -> float | None:
        return self.right / self.joined if self.joined else None

    @property
    def recall(self) -> float | None:
        total = self.right + self.missed
        return self.right / total if total else None


def _side(raw: object, where: str) -> Side:
    if not isinstance(raw, dict) or set(raw) != {"title", "excerpt", "url"}:
        raise CorpusError(f"{where}: an article needs exactly title, excerpt and url")
    title, excerpt, url = raw["title"], raw["excerpt"], raw["url"]
    try:
        check_public_fields(title, excerpt, url)
    except NotPublicArticle:
        raise CorpusError(
            f"{where}: the text must be plain ({MAX_TITLE} and {MAX_EXCERPT} characters at most) "
            "with a public http(s) address"
        ) from None
    return Side(title, excerpt, url)


def parse_pairs(raw: object) -> list[Pair]:
    if not isinstance(raw, list) or not raw:
        raise CorpusError("the pairs file must be a non-empty list")
    if len(raw) > MAX_PAIRS:
        raise CorpusError(f"at most {MAX_PAIRS} pairs")
    pairs: list[Pair] = []
    for position, entry in enumerate(raw, start=1):
        where = f"pair {position}"
        if not isinstance(entry, dict) or set(entry) != {"id", "a", "b", "same"}:
            raise CorpusError(f"{where}: the fields must be exactly id, a, b and same")
        ident, same = entry["id"], entry["same"]
        if not isinstance(ident, str) or not 1 <= len(ident) <= 40:
            raise CorpusError(f"{where}: id must be 1 to 40 characters")
        if not isinstance(same, bool):
            raise CorpusError(f"{where}: same must be true or false")
        pairs.append(Pair(ident, _side(entry["a"], where), _side(entry["b"], where), same))
    if len({pair.id for pair in pairs}) != len(pairs):
        raise CorpusError("ids must be unique")
    if not any(p.same for p in pairs) or all(p.same for p in pairs):
        raise CorpusError("the file needs both same-story and different-story pairs")
    return pairs


def load_pairs(path: Path) -> list[Pair]:
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            raise CorpusError("the pairs file is too large")
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        raise CorpusError("cannot read the pairs file") from None
    return parse_pairs(raw)


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    if len(a) != len(b):
        raise ValueError("vectors of different sizes are never compared")
    norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return sum(x * y for x, y in zip(a, b, strict=True)) / norm if norm else 0.0


def table(
    similarities: Sequence[float], pairs: Sequence[Pair], thresholds: Sequence[float]
) -> list[Row]:
    rows = []
    for threshold in sorted(set(thresholds)):
        joined = [(s >= threshold, p.same) for s, p in zip(similarities, pairs, strict=True)]
        right = sum(1 for above, same in joined if above and same)
        wrong = sum(1 for above, same in joined if above and not same)
        missed = sum(1 for above, same in joined if not above and same)
        rows.append(Row(threshold, right + wrong, right, wrong, missed))
    return rows


class _Usage:
    async def record(self, usage: UsageRecord) -> None:
        return None


async def similarities(
    pairs: Sequence[Pair],
    embedder: EmbeddingProvider,
    model: str,
    *,
    max_cost_micro_usd: int,
    embedding_price: int,
) -> list[float]:
    """The cosine similarity of each pair, through the product's own gateway (class A only,
    masked, booked), so the numbers are those the installation would get."""
    gateway = LLMGateway(
        None,
        InMemoryBudget(max_cost_micro_usd),  # type: ignore[arg-type]
        _Usage(),
        GatewayConfig(
            models={Stage.EMBEDDING: model},
            calls_per_minute=10**6,
            max_retries=0,
            embedding_price=embedding_price,
        ),
        SystemClock(),
        embedder=embedder,
    )
    found: list[float] = []
    for pair in pairs:
        vectors = []
        for side in (pair.a, pair.b):
            article = public_text_from_corpus(side.title, side.excerpt, side.url, None)
            vectors.append((await gateway.embed(org_id=_EVAL_ORG, article=article)).vector)
        found.append(cosine(vectors[0], vectors[1]))
    return found


def render(rows: Sequence[Row], pairs: Sequence[Pair]) -> str:
    same = sum(1 for p in pairs if p.same)
    lines = [
        f"{len(pairs)} pairs: {same} same story, {len(pairs) - same} different stories",
        "",
        "| threshold | joined | right | wrong (hidden by mistake) | missed (shown twice) "
        "| precision | recall |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]

    def pct(value: float | None) -> str:
        return "-" if value is None else f"{value * 100:.0f}%"

    for row in rows:
        lines.append(
            f"| {row.threshold:.2f} | {row.joined} | {row.right} | {row.wrong} | {row.missed} "
            f"| {pct(row.precision)} | {pct(row.recall)} |"
        )
    lines += [
        "",
        "Choose the lowest threshold whose `wrong` is still 0 (or as near as you accept) and write "
        "it as PHAROS_EMBEDDING_SIMILARITY. A wrong join hides an article; a missed one only "
        "shows a story twice.",
    ]
    return "\n".join(lines)
