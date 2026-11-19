"""Token-budget packing of the evidence an answer may use (design §15; plan 1.10).

First pass: up to ``per_item`` chunks per item in ranked order. Second pass: the rest. A chunk
is added only if it fits the remaining budget and its rerank score is at least ``min_score``.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

from cairn_retrieval.types import Candidate


def estimate_tokens(text: str) -> int:
    """``ceil(1.3 * words)``: the same estimate the chunker stores (kept local so retrieval does
    not depend on connectors)."""
    words = len(text.split())
    return math.ceil(1.3 * words) if words else 0


def pack(
    cands: Sequence[Candidate],
    *,
    budget: int,
    max_chunks: int,
    per_item: int,
    min_score: float | None,
) -> list[Candidate]:
    chosen: list[Candidate] = []
    taken: set[int] = set()
    per: dict[int, int] = {}
    used = 0

    def admissible(c: Candidate) -> bool:
        return min_score is None or c.rerank_score is None or c.rerank_score >= min_score

    for first_pass in (True, False):
        for c in cands:
            if len(chosen) >= max_chunks:
                return chosen
            if c.chunk_id in taken or not admissible(c):
                continue
            if first_pass and per.get(c.item_id, 0) >= per_item:
                continue
            cost = max(1, estimate_tokens(c.text))
            if used + cost > budget:
                continue
            chosen.append(c)
            taken.add(c.chunk_id)
            per[c.item_id] = per.get(c.item_id, 0) + 1
            used += cost
    return chosen
