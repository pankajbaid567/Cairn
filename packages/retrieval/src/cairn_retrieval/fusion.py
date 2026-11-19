"""Reciprocal rank fusion (design §15; PD1-19)."""

from __future__ import annotations

from collections.abc import Sequence


def rrf(rankings: Sequence[Sequence[int]], k: int = 60) -> list[tuple[int, float]]:
    """Fuse ranked id lists: ``score(id) = sum 1 / (k + rank)`` with ranks starting at 1.

    Deterministic: ties on score are broken by the best (lowest) rank any list gave the id, then by
    the id. An id repeated inside one list counts once, at its first position.
    """
    score: dict[int, float] = {}
    best: dict[int, int] = {}
    for ranking in rankings:
        seen: set[int] = set()
        for position, item in enumerate(ranking, start=1):
            if item in seen:
                continue
            seen.add(item)
            score[item] = score.get(item, 0.0) + 1.0 / (k + position)
            best[item] = min(best.get(item, position), position)
    return sorted(score.items(), key=lambda kv: (-kv[1], best[kv[0]], kv[0]))
