"""Running the candidate models over the corpus with the product's own gateway and prompts.

For every stage, every model of that stage and every article (and, for the two stages that write
text, every language) one call is made through `LLMGateway`, exactly as the worker makes it: the
same prompts, schemas, masking, timeouts and checks. There are no retries (a failure is a result),
and an in-memory budget stops the run at its cost cap. Nothing is stored but the answers, which stay
in files; no database, no Redis.
"""

import json
import re
import secrets
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pharos.core.clock import Clock, SystemClock
from pharos.evaluation.corpus import LANGUAGES, CorpusItem
from pharos.insights.validator import validate_insight
from pharos.llm.budget import Refusal, Reservation
from pharos.llm.corpus_door import public_text_from_corpus
from pharos.llm.enrichment import check_answer
from pharos.llm.errors import BudgetExhausted, LLMError
from pharos.llm.gateway import GatewayConfig, LLMGateway
from pharos.llm.prompt import PROMPT_VERSIONS
from pharos.llm.provider import Provider
from pharos.llm.schemas import EnrichAnswer, FilterAnswer, InsightAnswer
from pharos.llm.types import Stage, Task
from pharos.llm.usage import UsageRecord

# One organization that exists nowhere: the gateway wants an identifier for its budget and records.
EVAL_ORG = uuid.UUID(int=0)
STAGES = ("filter", "enrichment", "insight")
TEXT_STAGES = ("enrichment", "insight")


class InMemoryBudget:
    """The interface of the production budget, with a cost cap and no pace limit."""

    def __init__(self, cap_micro_usd: int) -> None:
        self.cap = cap_micro_usd
        self.total = 0

    async def reserve(self, org: uuid.UUID, estimate: int) -> Reservation | Refusal:
        if self.total + estimate > self.cap:
            return Refusal(exhausted_first=True)
        self.total += estimate
        return Reservation(org, "evaluation", estimate, self.total, False)

    async def settle(self, reservation: Reservation, actual: int) -> None:
        self.total += actual - reservation.amount

    async def release(self, reservation: Reservation) -> None:
        self.total -= reservation.amount

    async def pace(self, org: uuid.UUID, calls_per_minute: int) -> bool:
        return True


class _Usage:
    def __init__(self) -> None:
        self.records: list[UsageRecord] = []

    async def record(self, usage: UsageRecord) -> None:
        self.records.append(usage)


MODEL_NAME = re.compile(r"[A-Za-z0-9._:/@-]{1,100}")


@dataclass(frozen=True, slots=True)
class Plan:
    """Which models to try at each stage."""

    models: dict[str, list[str]]

    def __post_init__(self) -> None:
        for stage, found in self.models.items():
            if stage not in STAGES:
                raise ValueError(f"unknown stage {stage!r}")
            if len(set(found)) != len(found) or not all(found):
                raise ValueError(f"the models of {stage} must be named once each")
            if not all(isinstance(name, str) and MODEL_NAME.fullmatch(name) for name in found):
                raise ValueError(f"a model name of {stage} has characters a model name never has")


@dataclass(slots=True)
class RunResult:
    answers: list[dict[str, Any]] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)


def _stage_of(name: str) -> Stage:
    return {"filter": Stage.FILTER, "enrichment": Stage.ENRICHMENT, "insight": Stage.INSIGHT}[name]


def _failure(error: Exception) -> str:
    code = error.code if isinstance(error, LLMError) else type(error).__name__
    return str(code)


async def run(
    corpus: Sequence[CorpusItem],
    plan: Plan,
    languages: Sequence[str],
    provider: Provider,
    *,
    provider_name: str,
    max_cost_micro_usd: int,
    prices: dict[str, tuple[int, int]] | None = None,
    clock: Clock | None = None,
    progress: Callable[[str], None] = lambda _line: None,
    on_answer: Callable[[dict[str, Any]], None] = lambda _answer: None,
) -> RunResult:
    bad = [lang for lang in languages if lang not in LANGUAGES]
    if bad or not languages:
        raise ValueError("languages must be some of az, tr, ru, en")
    clock = clock or SystemClock()
    budget = InMemoryBudget(max_cost_micro_usd)
    usage = _Usage()
    known = frozenset(item.profession for item in corpus)
    result = RunResult()
    seen_codes: set[str] = set()
    incomplete = False

    def code() -> str:
        while True:
            fresh = secrets.token_hex(5)
            if fresh not in seen_codes:
                seen_codes.add(fresh)
                return fresh

    for stage_name in STAGES:
        for model in plan.models.get(stage_name, []):
            stage = _stage_of(stage_name)
            gateway = LLMGateway(
                provider,
                budget,  # type: ignore[arg-type]
                usage,
                GatewayConfig(
                    models={stage: model},
                    calls_per_minute=10**6,
                    max_retries=0,
                    prices=dict(prices or {}),
                ),
                clock,
            )
            for item in corpus:
                for language in languages if stage_name in TEXT_STAGES else (None,):
                    record: dict[str, Any] = {
                        "code": code(),
                        "stage": stage_name,
                        "model": model,
                        "item": item.id,
                        "language": language,
                        "profession": item.profession,
                        "output": None,
                        "checked": False,
                        "correct": None,
                        "failed": None,
                    }
                    article = public_text_from_corpus(item.title, item.excerpt, item.url, item.lang)
                    try:
                        if stage_name == "filter":
                            out = await gateway.generate(
                                task=Task.FILTER_RELEVANCE,
                                org_id=EVAL_ORG,
                                article=article,
                                schema=FilterAnswer,
                            )
                            record["output"] = {
                                "relevant": out.relevant,
                                "confidence": out.confidence,
                            }
                            record["correct"] = (
                                None if item.relevant is None else out.relevant == item.relevant
                            )
                            record["checked"] = True
                        elif stage_name == "enrichment":
                            enriched = await gateway.generate(
                                task=Task.ENRICH,
                                org_id=EVAL_ORG,
                                article=article,
                                schema=EnrichAnswer,
                                language=language,
                                professions=sorted(known),
                            )
                            record["output"] = {
                                "summary": enriched.summary,
                                "category": enriched.category,
                            }
                            record["checked"] = check_answer(enriched, known) is not None
                        else:
                            insight = await gateway.generate(
                                task=Task.INSIGHT,
                                org_id=EVAL_ORG,
                                article=article,
                                schema=InsightAnswer,
                                language=language,
                                profession=item.profession,
                            )
                            record["output"] = {
                                "summary": insight.summary,
                                "how_to_apply": insight.how_to_apply,
                                "try_prompt": insight.try_prompt,
                                "safety_note": insight.safety_note,
                            }
                            record["checked"] = (
                                validate_insight(
                                    insight.summary,
                                    insight.how_to_apply,
                                    insight.try_prompt,
                                    insight.safety_note,
                                )
                                is None
                            )
                    except BudgetExhausted:
                        incomplete = True
                        break
                    except Exception as error:  # a failure is a result, not a reason to stop
                        record["failed"] = _failure(error)
                    result.answers.append(record)
                    on_answer(record)  # kept at once: an interrupted run loses nothing paid for
                if incomplete:
                    break
            progress(f"{stage_name}: {model} done")
            if incomplete:
                break
        if incomplete:
            break

    costs: dict[str, int] = {}
    for used in usage.records:
        key = f"{used.stage}:{used.model}"
        costs[key] = costs.get(key, 0) + used.cost_micro_usd
    result.meta = {
        "created": datetime.now(UTC).date().isoformat(),
        "provider": provider_name,
        "languages": list(languages),
        "plan": plan.models,
        "prompt_versions": {task.value: version for task, version in PROMPT_VERSIONS.items()},
        "max_cost_micro_usd": max_cost_micro_usd,
        "spent_micro_usd": sum(costs.values()),
        "costs_micro_usd": costs,
        "incomplete": incomplete,
    }
    return result


def write(result: RunResult, directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "answers.jsonl").open("w", encoding="utf-8") as handle:
        for answer in result.answers:
            handle.write(json.dumps(answer, ensure_ascii=False) + "\n")
    (directory / "run.json").write_text(
        json.dumps(result.meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def read(directory: Path) -> RunResult:
    answers = [
        json.loads(line)
        for line in (directory / "answers.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    meta = json.loads((directory / "run.json").read_text(encoding="utf-8"))
    return RunResult(answers, meta)
