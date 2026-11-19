"""Plain data of the retrieval pipeline (design §15)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal

from cairn_retrieval.access import PrincipalSnapshot

Degraded = Literal["dense", "rerank"]

DEGRADING_ERRORS: Final = frozenset(
    {
        "budget_exhausted",
        "daily_limit",
        "rate_limited",
        "server",
        "timeout",
        "transport",
        "bad_response",
    }
)
"""Provider error codes a search survives by skipping that stage (PD1-28). ``not_configured``,
``auth`` and ``bad_request`` are configuration or code faults and propagate."""


@dataclass(frozen=True)
class RetrievalConfig:
    variant: Literal["b0", "b1"] = "b1"
    lexical_k: int = 50
    dense_k: int = 50
    rrf_k: int = 60
    rerank_n: int = 30
    limit: int = 10
    pack_budget_tokens: int = 1800
    pack_max_chunks: int = 8
    pack_per_item_first_pass: int = 2
    min_rerank_score: float | None = 0.0
    vector_strategy: str = "iterative_strict_ef100"


@dataclass(frozen=True)
class Candidate:
    chunk_id: int
    item_id: int
    external_id: str
    revision_hash: str
    start: int
    end: int
    text: str
    title: str
    source_kind: str
    fused_rank: int
    rerank_score: float | None = None


@dataclass(frozen=True)
class SearchResult:
    snapshot: PrincipalSnapshot
    candidates: list[Candidate]
    ranked: list[Candidate]
    degraded: tuple[Degraded, ...] = ()
    b0_fts_filled: int = 0
