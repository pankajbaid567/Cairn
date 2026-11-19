"""Vector search strategies (design §13; PD1-11, ADR-0008)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Final

from psycopg import AsyncConnection

from cairn_core.db import aset_local


@dataclass(frozen=True)
class VectorStrategy:
    name: str
    settings: Mapping[str, str] = field(default_factory=dict)
    exact: bool = False  # True: dense_exact_candidates (planner-forced exact scan)


STRATEGIES: Final[Mapping[str, VectorStrategy]] = {
    s.name: s
    for s in (
        VectorStrategy("post_filter_ef40", {"hnsw.iterative_scan": "off", "hnsw.ef_search": "40"}),
        VectorStrategy(
            "post_filter_ef200", {"hnsw.iterative_scan": "off", "hnsw.ef_search": "200"}
        ),
        VectorStrategy(
            "iterative_strict", {"hnsw.iterative_scan": "strict_order", "hnsw.ef_search": "40"}
        ),
        VectorStrategy(
            "iterative_relaxed", {"hnsw.iterative_scan": "relaxed_order", "hnsw.ef_search": "40"}
        ),
        VectorStrategy(
            "iterative_strict_ef100",
            {"hnsw.iterative_scan": "strict_order", "hnsw.ef_search": "100"},
        ),
        VectorStrategy("exact_prefilter", {}, exact=True),
    )
}

DEFAULT_STRATEGY: Final = "iterative_strict_ef100"


def get_strategy(name: str) -> VectorStrategy:
    try:
        return STRATEGIES[name]
    except KeyError:
        raise ValueError(f"unknown vector strategy {name!r}") from None


async def apply_strategy(conn: AsyncConnection[Any], strategy: VectorStrategy) -> None:
    """Write the strategy's session settings for the current transaction."""
    for name, value in strategy.settings.items():
        await aset_local(conn, name, value)


def vector_literal(vector: list[float]) -> str:
    """pgvector text input: ``[0.1,0.2,...]``, cast with ``::vector`` in SQL."""
    return "[" + ",".join(f"{x:.8g}" for x in vector) + "]"
