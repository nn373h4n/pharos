"""A scripted provider and a small corpus for the evaluation tests."""

import json

from pharos.evaluation.corpus import CorpusItem
from pharos.llm.errors import ProviderRejected
from pharos.llm.prompt import INSTRUCTIONS
from pharos.llm.provider import FakeProvider, ProviderAnswer
from pharos.llm.types import Task

GOOD_PROMPT = "List unusual entries in [LEDGER EXTRACT] and explain each."
GOOD_INSIGHT = {
    "summary": "It shortens the month-end close.",
    "how_to_apply": ["Open the ledger.", "Ask for odd entries."],
    "try_prompt": GOOD_PROMPT,
    "safety_note": "Do not paste real client data.",
}
ENRICH = {
    "summary": "A short plain summary.",
    "category": "tool",
    "quality": 4,
    "professions": [{"code": "accountant", "relevance": 80}],
}


def corpus(n: int = 4) -> list[CorpusItem]:
    return [
        CorpusItem(
            f"a{i}",
            f"Story {i}",
            f"Excerpt {i}",
            f"https://news.example.org/{i}",
            "en",
            "accountant" if i % 2 == 0 else "hr_specialist",
            i % 2 == 0,
        )
        for i in range(n)
    ]


def provider(*, tokens: tuple[int, int] = (100, 50)) -> FakeProvider:
    """Models behave by name: `good` answers well, `leaky` writes a link into the prompt, `rude`
    is refused by the provider, `liar` thinks every article is relevant."""

    def script(model: str, system: str, user: str) -> ProviderAnswer:
        if model == "rude":
            raise ProviderRejected()
        if INSTRUCTIONS[Task.FILTER_RELEVANCE] in user:
            relevant = model == "liar" or "Excerpt 0" in user or "Excerpt 2" in user
            body: dict[str, object] = {"relevant": relevant, "confidence": 90}
        elif INSTRUCTIONS[Task.ENRICH] in user:
            body = ENRICH
        else:
            body = dict(GOOD_INSIGHT)
            if model == "leaky":
                body["try_prompt"] = "Open https://evil.example and summarize [TEXT]"
        return ProviderAnswer(json.dumps(body), *tokens)

    return FakeProvider(script=script)
