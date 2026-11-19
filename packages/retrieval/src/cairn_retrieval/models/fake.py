"""Deterministic embedder and reranker for tests, CI and local runs (design §13, FT-23).

No network, no randomness, no clock: the same input always gives the same output
(Requirements 16.3 and 16.4). They are not good models; they only keep the pipeline testable.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections import Counter
from collections.abc import Sequence
from typing import Final

__all__ = ["FAKE_EMBED_KEY", "FAKE_RERANK_KEY", "STOPWORDS", "FakeEmbedder", "FakeReranker"]

FAKE_EMBED_KEY: Final = "fake-embed-v1"
FAKE_RERANK_KEY: Final = "fake-rerank-v1"

_WORD: Final = re.compile(r"\w+")

STOPWORDS: Final = frozenset(
    {
        *("a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "has", "have"),
        *("in", "is", "it", "its", "of", "on", "or", "that", "the", "this", "to", "was"),
        *("were", "what", "when", "where", "which", "who", "why", "with"),
    }
)
"""Words the fake reranker ignores when it counts shared tokens."""


def _tokens(text: str) -> list[str]:
    return _WORD.findall(text.lower())


class FakeEmbedder:
    """Hashed bag of lowercase words, scaled to unit length (L2 norm).

    Each token is hashed with blake2b into one of ``dim`` buckets and the bucket counts form the
    vector. A text with no word token (empty, or only punctuation) maps to the unit vector ``e0``.
    ``embed_query`` embeds the text as it is: the fake has no query prefix.
    """

    def __init__(self, dim: int = 384) -> None:
        if dim < 1:
            raise ValueError("dim must be at least 1")
        self.dim = dim
        self.model_key = FAKE_EMBED_KEY

    def _vector(self, text: str) -> list[float]:
        counts: Counter[int] = Counter()
        for token in _tokens(text):
            digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
            counts[int.from_bytes(digest, "big") % self.dim] += 1
        if not counts:
            return [1.0] + [0.0] * (self.dim - 1)
        norm = math.sqrt(sum(c * c for c in counts.values()))
        vector = [0.0] * self.dim
        for bucket, count in counts.items():
            vector[bucket] = count / norm
        return vector

    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._vector(t) for t in texts]

    async def embed_query(self, text: str) -> list[float]:
        return self._vector(text)


class FakeReranker:
    """Score of a passage: distinct query tokens (stopwords removed) that the passage also has,
    divided by ``1 + `` the passage's token count. Each score depends on the query and that
    passage only, never on the other passages or on the position (Requirement 16.4)."""

    def __init__(self) -> None:
        self.model_key = FAKE_RERANK_KEY

    async def score(self, query: str, passages: Sequence[str]) -> list[float]:
        wanted = {t for t in _tokens(query) if t not in STOPWORDS}
        scores: list[float] = []
        for passage in passages:
            tokens = _tokens(passage)
            shared = len(wanted.intersection(tokens))
            scores.append(shared / (1 + len(tokens)))
        return scores
