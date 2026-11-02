"""The provider side of the gateway: a small interface, and a fake for tests, CI and demo mode."""

import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

from pharos.llm.errors import ProviderFailed


@dataclass(frozen=True, slots=True)
class ProviderAnswer:
    text: str
    tokens_in: int
    tokens_out: int


class Provider(Protocol):
    name: str

    async def complete(
        self, *, model: str, system: str, user: str, max_tokens: int, deadline_seconds: float
    ) -> ProviderAnswer:
        """One request, one answer. No tools, no streaming. Raises ProviderFailed (worth a retry)
        or ProviderRejected (not)."""
        ...


Script = Callable[[str, str, str], Awaitable[ProviderAnswer] | ProviderAnswer]


class FakeProvider:
    """A deterministic provider: it answers from a script, or with a fixed JSON text.

    It records what it was asked, so tests can look at the exact messages, the model and the limits
    the gateway used. It is the provider of the demo mode and of every test: the project never needs
    a paid account to be tried or tested.
    """

    name = "fake"

    def __init__(
        self,
        answer: str = "{}",
        *,
        script: Script | None = None,
        fail: bool = False,
        timeout: bool = False,
    ) -> None:
        self.answer = answer
        self.script = script
        self.fail = fail  # every call fails before anything is billed (a provider that is down)
        self.timeout = timeout  # every call fails after the request was sent (a timeout)
        self.calls: list[dict[str, object]] = []

    async def complete(
        self, *, model: str, system: str, user: str, max_tokens: int, deadline_seconds: float
    ) -> ProviderAnswer:
        self.calls.append(
            {
                "model": model,
                "system": system,
                "user": user,
                "max_tokens": max_tokens,
                "deadline_seconds": deadline_seconds,
            }
        )
        if self.script is not None:
            result = self.script(model, system, user)
            if isinstance(result, ProviderAnswer):
                return result
            return await result
        if self.timeout:
            raise ProviderFailed(sent=True)
        if self.fail:
            raise ProviderFailed(sent=False)
        return ProviderAnswer(
            self.answer, tokens_in=len(system + user) // 4, tokens_out=len(self.answer) // 4
        )


_CODES = re.compile(r"Professions \(use only these codes\): ([^\n]+)")


def _demo_answer(_model: str, _system: str, user: str) -> ProviderAnswer:
    """A schema-valid answer for each task, read from the schema the product put into the prompt.

    Deterministic and free: the first catalogue code (in the prompt's own sorted order) is the fit,
    an article that says OFFTOPIC is not relevant. It is what a demo or a stack test needs; it is
    not a model, and it says nothing about quality."""
    # The task is read from the part the product wrote (before the article); only the OFFTOPIC
    # switch looks inside the article, and it is a test aid, not a decision.
    task, _, article = user.partition("[ARTICLE ")
    if '"try_prompt"' in task:
        body: dict[str, object] = {
            "summary": "A short way to use this at work.",
            "how_to_apply": ["Pick one recurring task.", "Try the prompt on a harmless example."],
            "try_prompt": "Summarise this in three bullets: [paste a short harmless text]",
            "safety_note": "Do not paste confidential or personal data.",
        }
    elif '"professions"' in task and '"category"' in task:
        found = _CODES.search(task)
        codes = [code.strip() for code in found.group(1).split(",")] if found else []
        body = {
            "summary": "A demo summary of the article.",
            "category": "tutorial",
            "quality": 4,
            "professions": [
                {"code": code, "relevance": 90 if position == 0 else 10}
                for position, code in enumerate(codes[:10])
            ],
        }
    else:
        body = {"relevant": "OFFTOPIC" not in article, "confidence": 90}
    text = json.dumps(body)
    return ProviderAnswer(text, tokens_in=len(user) // 4, tokens_out=len(text) // 4)


class DemoProvider(FakeProvider):
    """The provider of `PHAROS_LLM_PROVIDER=fake`: schema-valid answers without a model."""

    def __init__(self) -> None:
        super().__init__(script=_demo_answer)
