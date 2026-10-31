"""The shape of each task's answer: a closed table (ADR-0015, point 4).

The schema is put into the prompt by the product, so it must come from here: a class built at run
time from some other text would open a channel for that text into the instruction. The gateway
refuses any class that is not the one registered for the task. The fields are refined with their
tasks (M4-04, M4-06); the rule that every object forbids extra fields stays.
"""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from pharos.llm.types import Task

CATEGORIES = Literal[
    "tool", "technique", "case_study", "research", "policy", "tutorial", "news", "other"
]


class _Answer(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class FilterAnswer(_Answer):
    relevant: bool
    confidence: int = Field(ge=0, le=100)


class ProfessionScore(_Answer):
    code: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,39}$")
    relevance: int = Field(ge=0, le=100)


class EnrichAnswer(_Answer):
    summary: str = Field(min_length=1, max_length=400)
    category: CATEGORIES
    quality: int = Field(ge=1, le=5)
    professions: list[ProfessionScore] = Field(max_length=10)


class InsightAnswer(_Answer):
    summary: str = Field(min_length=1, max_length=400)
    how_to_apply: list[str] = Field(min_length=1, max_length=3)
    try_prompt: str = Field(min_length=1, max_length=800)
    safety_note: str = Field(min_length=1, max_length=300)


SCHEMAS: dict[Task, type[BaseModel]] = {
    Task.FILTER_RELEVANCE: FilterAnswer,
    Task.ENRICH: EnrichAnswer,
    Task.INSIGHT: InsightAnswer,
}


def forbids_extra(schema: type[BaseModel]) -> bool:
    """True if this model and every model inside it forbid extra fields (the JSON schema of a
    model that does says `additionalProperties: false` for each object)."""

    def objects(node: Any) -> list[dict[str, Any]]:
        found: list[dict[str, Any]] = []
        if isinstance(node, dict):
            if node.get("type") == "object" or "properties" in node:
                found.append(node)
            for value in node.values():
                found.extend(objects(value))
        elif isinstance(node, list):
            for value in node:
                found.extend(objects(value))
        return found

    return all(
        item.get("additionalProperties") is False for item in objects(schema.model_json_schema())
    )
