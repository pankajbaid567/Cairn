"""Per-model concurrency limits (design §14): one asyncio semaphore per provider object.

The Render service has 0.1 CPU and Workers AI rate limits; two concurrent calls per model is
plenty. ``limited`` wraps an embedder, reranker or LLM and keeps its attributes.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Any

from cairn_retrieval.llm.types import LlmProvider, LlmRequest, LlmResponse
from cairn_retrieval.models.protocols import Embedder, Reranker


class LimitedEmbedder:
    def __init__(self, inner: Embedder, concurrency: int) -> None:
        self._inner = inner
        self._sem = asyncio.Semaphore(concurrency)
        self.model_key = inner.model_key
        self.dim = inner.dim

    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        async with self._sem:
            return await self._inner.embed_documents(texts)

    async def embed_query(self, text: str) -> list[float]:
        async with self._sem:
            return await self._inner.embed_query(text)


class LimitedReranker:
    def __init__(self, inner: Reranker, concurrency: int) -> None:
        self._inner = inner
        self._sem = asyncio.Semaphore(concurrency)
        self.model_key = inner.model_key

    async def score(self, query: str, passages: Sequence[str]) -> list[float]:
        async with self._sem:
            return await self._inner.score(query, passages)


class LimitedLlm:
    def __init__(self, inner: LlmProvider, concurrency: int) -> None:
        self._inner = inner
        self._sem = asyncio.Semaphore(concurrency)
        self.provider = inner.provider
        self.model = inner.model

    async def complete(self, req: LlmRequest) -> LlmResponse:
        async with self._sem:
            return await self._inner.complete(req)


def limited(provider: Any, concurrency: int) -> Any:
    """Wrap an embedder, reranker or LLM with a semaphore of ``concurrency`` slots."""
    if hasattr(provider, "embed_documents"):
        return LimitedEmbedder(provider, concurrency)
    if hasattr(provider, "score"):
        return LimitedReranker(provider, concurrency)
    if hasattr(provider, "complete"):
        return LimitedLlm(provider, concurrency)
    raise TypeError("not an embedder, reranker or LLM provider")
