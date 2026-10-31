"""The provider side of the gateway: a small interface, and a fake for tests, CI and demo mode."""

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
