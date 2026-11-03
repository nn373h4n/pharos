"""Embeddings of public article text (M4-05, ADR-0007).

An embedding turns a title and an excerpt into a vector, so that near-duplicates can be found
without a model call that writes text. Only `PublicArticleText` goes in (class A, ADR-0015), after
the same masking as a prompt, and only a list of numbers comes back; nothing of it is executed or
shown. The model is the operator's choice and is never named in code (ADR-0007); a model change is
a recompute, and vectors of different models are never compared.
"""

import hashlib
import math
import re
from dataclasses import dataclass
from typing import Protocol

from pharos.llm.errors import SchemaRejected
from pharos.llm.masking import mask
from pharos.llm.types import MAX_EXCERPT, MAX_TITLE, PublicArticleText

MIN_DIM = 8
MAX_DIM = 4096
FLOAT32_MAX = 3.0e38
FLOAT32_USABLE = 1.0e-30


@dataclass(frozen=True, slots=True)
class EmbedAnswer:
    """What a provider returns: one vector per text sent, and the tokens it counted."""

    vectors: list[list[float]]
    tokens_in: int


@dataclass(frozen=True, slots=True)
class Embedded:
    model: str
    vector: tuple[float, ...]

    @property
    def dim(self) -> int:
        return len(self.vector)


class EmbeddingProvider(Protocol):
    name: str

    async def embed(self, *, model: str, texts: list[str], deadline_seconds: float) -> EmbedAnswer:
        """One request, one vector per text. Raises ProviderFailed (worth a retry) or
        ProviderRejected (not)."""
        ...


def embedding_text(article: PublicArticleText) -> str:
    """What is embedded: the masked title and excerpt, cut to the same bounds as a prompt."""
    title = mask(article.title).text[:MAX_TITLE]
    excerpt = mask(article.excerpt).text[:MAX_EXCERPT]
    return f"{title}\n{excerpt}".strip()


def check_vector(answer: EmbedAnswer, *, expected: int = 1) -> tuple[float, ...]:
    """The one vector of an answer, or SchemaRejected. A provider's answer is data: a wrong count,
    a size outside the bounds, a value that is not a finite number, or a vector of zeros is
    refused, never stored."""
    if len(answer.vectors) != expected or expected != 1:
        raise SchemaRejected()
    vector = answer.vectors[0]
    if not isinstance(vector, list) or not MIN_DIM <= len(vector) <= MAX_DIM:
        raise SchemaRejected()
    for value in vector:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise SchemaRejected()
        if not math.isfinite(value) or abs(value) > FLOAT32_MAX:
            raise SchemaRejected()
    # The database keeps 32-bit floats, so what matters is whether anything is left after that: a
    # vector of values too small for it would be stored as zeros (and every similarity to it would
    # be "not a number").
    if max(abs(value) for value in vector) < FLOAT32_USABLE:
        raise SchemaRejected()
    return tuple(float(value) for value in vector)


_WORD = re.compile(r"\w+", re.UNICODE)
DEMO_DIM = 64


class DemoEmbedder:
    """The embedder of `PHAROS_EMBEDDING_PROVIDER=fake`: a bag of hashed words, normalised.

    Deterministic and free, and good enough to tell a rewritten copy of an article from a different
    article by the words they share. It is not a language model and says nothing about meaning or
    about any language; it exists so that the whole path can be tried and tested without a model.
    """

    name = "fake"

    async def embed(self, *, model: str, texts: list[str], deadline_seconds: float) -> EmbedAnswer:
        vectors = [self._one(text) for text in texts]
        return EmbedAnswer(vectors, tokens_in=sum(len(text) // 4 + 1 for text in texts))

    @staticmethod
    def _one(text: str) -> list[float]:
        vector = [0.0] * DEMO_DIM
        for word in _WORD.findall(text.casefold()):
            digest = hashlib.sha256(word.encode()).digest()
            slot = int.from_bytes(digest[:4], "big") % DEMO_DIM
            vector[slot] += 1.0 if digest[4] % 2 == 0 else -1.0
        norm = math.sqrt(sum(value * value for value in vector))
        if norm == 0:
            vector[0] = 1.0  # no word at all: still a vector, and one every empty text shares
            return vector
        return [value / norm for value in vector]
