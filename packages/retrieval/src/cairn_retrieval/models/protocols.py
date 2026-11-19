"""Embedder and reranker protocols (design §13, FT-23; Requirement 16.1).

Both are async so that the Workers AI adapters and the fakes are interchangeable.
``model_key`` identifies the vector space: Phase 1 stores it next to every embedding so a model
change is visible and old vectors are never compared with new ones.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, runtime_checkable

__all__ = ["Embedder", "Reranker"]


@runtime_checkable
class Embedder(Protocol):
    """Turns texts into vectors of a fixed dimension."""

    model_key: str
    """``workers-ai:<model id>`` or ``fake-embed-v1``."""
    dim: int

    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        """One vector per text, in input order."""
        ...

    async def embed_query(self, text: str) -> list[float]:
        """The vector of one search query (the adapter adds the model's query prefix)."""
        ...


@runtime_checkable
class Reranker(Protocol):
    """Scores passages against a query; a higher score is more relevant."""

    model_key: str

    async def score(self, query: str, passages: Sequence[str]) -> list[float]:
        """One score per passage, in input order."""
        ...
