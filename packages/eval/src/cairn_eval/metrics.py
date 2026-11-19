"""Retrieval metrics (design §18; Property 24)."""

from __future__ import annotations

from collections.abc import Collection, Sequence


def recall_at_k(gold: Collection[str], ranked: Sequence[str], k: int) -> float:
    """Share of the distinct ``gold`` sources found among the first ``k`` ranked ids.

    Bounded to [0, 1], monotone in ``k``, and order-insensitive within the top ``k``. Raises for an
    empty ``gold``: such a question has no Recall@5 (it is not eligible).
    """
    wanted = set(gold)
    if not wanted:
        raise ValueError("recall needs at least one gold source")
    return len(wanted & set(ranked[:k])) / len(wanted)


def hit_at_k(gold: Collection[str], ranked: Sequence[str], k: int) -> bool:
    """At least one gold source among the first ``k`` ranked ids."""
    return bool(set(gold) & set(ranked[:k]))


def percentile(values: Sequence[float], q: float) -> float:
    """Nearest-rank percentile (0 < q <= 100); 0.0 for no values."""
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(1, -(-len(ordered) * q // 100))
    return float(ordered[min(len(ordered), int(rank)) - 1])
