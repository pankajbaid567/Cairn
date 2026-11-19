"""Builders that pick the embedder and reranker from ``ModelSettings`` (design §13)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from cairn_core.config import BedrockSettings, ModelSettings
from cairn_retrieval.cloudflare.client import WorkersAiClient
from cairn_retrieval.models.fake import FakeEmbedder, FakeReranker
from cairn_retrieval.models.protocols import Embedder, Reranker

if TYPE_CHECKING:
    from cairn_retrieval.bedrock.client import BedrockClient
from cairn_retrieval.models.workers_ai import WorkersAiEmbedder, WorkersAiReranker

__all__ = ["build_embedder", "build_reranker"]


def build_embedder(
    models: ModelSettings, client: WorkersAiClient | None, bedrock: BedrockClient | None = None
) -> Embedder:
    """The fake embedder, the Bedrock one (``bedrock``) or the Workers AI one (``client``)."""
    if models.embedder == "fake":
        return FakeEmbedder(models.embed_dim)
    if models.embedder == "bedrock":
        if bedrock is None:
            raise ValueError("CAIRN_EMBEDDER=bedrock needs a BedrockClient")
        from cairn_retrieval.bedrock.adapters import BedrockEmbedder  # lazy: avoids an import cycle

        return BedrockEmbedder(bedrock, models.embed_model, models.embed_dim)
    if client is None:
        raise ValueError("CAIRN_EMBEDDER=workers_ai needs a WorkersAiClient")
    return WorkersAiEmbedder(
        client, models.embed_model, models.embed_dim, models.embed_query_prefix
    )


def build_reranker(
    models: ModelSettings, client: WorkersAiClient | None, bedrock: BedrockClient | None = None
) -> Reranker | None:
    """``None`` for ``CAIRN_RERANKER=none``, the fake, the Bedrock LLM reranker, or Workers AI."""
    if models.reranker == "none":
        return None
    if models.reranker == "fake":
        return FakeReranker()
    if models.reranker == "bedrock":
        if bedrock is None:
            raise ValueError("CAIRN_RERANKER=bedrock needs a BedrockClient")
        from cairn_retrieval.bedrock.adapters import BedrockLlmReranker

        effort = models.bedrock_reasoning_effort if isinstance(models, BedrockSettings) else ""
        return BedrockLlmReranker(bedrock, models.rerank_model, reasoning_effort=effort)
    if client is None:
        raise ValueError("CAIRN_RERANKER=workers_ai needs a WorkersAiClient")
    return WorkersAiReranker(client, models.rerank_model)
