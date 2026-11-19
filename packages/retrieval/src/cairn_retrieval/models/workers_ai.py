"""Workers AI embedder and reranker (design §13, FT-23; Requirements 16.1 and 16.2).

Request bodies and result shapes are in FV8 (``docs/evidence/free-tier/verify/FV8.md``). Each
call reserves its neuron estimate through the client (FT-22); an estimate is computed here
because only the adapter knows how many tokens it sends. The models reject an empty text (the
schema requires at least one character), so the adapters raise ``ValueError`` for one before any
call; the fakes accept it.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

from cairn_retrieval.cloudflare.adapt import bad_response
from cairn_retrieval.cloudflare.budget import (
    estimate_input_tokens,
    estimate_neurons,
    estimate_tokens,
)
from cairn_retrieval.cloudflare.client import WorkersAiClient
from cairn_retrieval.cloudflare.parse import MalformedResult, parse_embeddings, parse_rerank

__all__ = ["EMBED_BATCH_SIZE", "WorkersAiEmbedder", "WorkersAiReranker"]

EMBED_BATCH_SIZE: Final = 50
"""Texts per embedding call. The schema allows 100 (FV8); 50 keeps a request small."""


class WorkersAiEmbedder:
    """Embeds through ``@cf/baai/bge-small-en-v1.5`` (or the configured model)."""

    def __init__(
        self, client: WorkersAiClient, model: str, dim: int, query_prefix: str = ""
    ) -> None:
        self._client = client
        self._model = model
        self._query_prefix = query_prefix
        self.dim = dim
        self.model_key = f"workers-ai:{model}"

    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        """Vectors in input order, one call per batch of at most ``EMBED_BATCH_SIZE`` texts."""
        if any(t == "" for t in texts):
            raise ValueError("an empty text cannot be embedded")
        vectors: list[list[float]] = []
        for start in range(0, len(texts), EMBED_BATCH_SIZE):
            vectors.extend(await self._embed_batch(list(texts[start : start + EMBED_BATCH_SIZE])))
        return vectors

    async def embed_query(self, text: str) -> list[float]:
        """The vector of ``query_prefix + text``, which the model was trained to match."""
        if text == "":
            raise ValueError("an empty query cannot be embedded")
        (vector,) = await self._embed_batch([self._query_prefix + text])
        return vector

    async def _embed_batch(self, batch: list[str]) -> list[list[float]]:
        neurons = estimate_neurons(self._model, estimate_input_tokens(batch))
        run = await self._client.run(self._model, {"text": batch}, estimate_neurons=neurons)
        try:
            return parse_embeddings(run.result, len(batch), self.dim)
        except MalformedResult:
            raise bad_response(self._model, run.attempts) from None


class WorkersAiReranker:
    """Scores passages through ``@cf/baai/bge-reranker-base`` (or the configured model)."""

    def __init__(self, client: WorkersAiClient, model: str) -> None:
        self._client = client
        self._model = model
        self.model_key = f"workers-ai:{model}"

    async def score(self, query: str, passages: Sequence[str]) -> list[float]:
        """One call per query: ``{"query": q, "contexts": [{"text": p}, ...]}``. Scores come back
        in input order whatever order the service lists them in."""
        if not passages:
            return []
        if query == "" or any(p == "" for p in passages):
            raise ValueError("an empty query or passage cannot be reranked")
        # The model reads the query once per passage, so the estimate counts it per pair.
        tokens = sum(estimate_tokens(query) + estimate_tokens(p) for p in passages)
        neurons = estimate_neurons(self._model, tokens)
        payload = {"query": query, "contexts": [{"text": p} for p in passages]}
        run = await self._client.run(self._model, payload, estimate_neurons=neurons)
        try:
            return parse_rerank(run.result, len(passages))
        except MalformedResult:
            raise bad_response(self._model, run.attempts) from None
