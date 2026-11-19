"""Embedder and reranker protocols, their Workers AI adapters and deterministic fakes."""

from cairn_retrieval.models.factory import build_embedder, build_reranker
from cairn_retrieval.models.fake import FakeEmbedder, FakeReranker
from cairn_retrieval.models.protocols import Embedder, Reranker
from cairn_retrieval.models.workers_ai import EMBED_BATCH_SIZE, WorkersAiEmbedder, WorkersAiReranker

__all__ = [
    "EMBED_BATCH_SIZE",
    "Embedder",
    "FakeEmbedder",
    "FakeReranker",
    "Reranker",
    "WorkersAiEmbedder",
    "WorkersAiReranker",
    "build_embedder",
    "build_reranker",
]
