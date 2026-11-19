"""Statistics and error taxonomy for the evaluation (plan 5.9).

Everything here is deterministic given its seed: the bootstrap uses a seeded generator, so the same
rows always give the same interval. Intervals are percentile bootstrap, 95%, over questions (not
over chunks), which is the unit the metrics are defined on.
"""

from __future__ import annotations

import random
from collections.abc import Sequence
from typing import Final

from cairn_eval.results import Row

RESAMPLES: Final = 2000
SEED: Final = 20261104


def mean(xs: Sequence[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def bootstrap_ci(
    values: Sequence[float], *, resamples: int = RESAMPLES, seed: int = SEED, level: float = 0.95
) -> tuple[float, float, float]:
    """``(mean, low, high)`` by percentile bootstrap. An empty input gives zeros; one value gives a
    degenerate interval (honestly wide intervals need data)."""
    n = len(values)
    if n == 0:
        return 0.0, 0.0, 0.0
    rng = random.Random(seed)  # noqa: S311 - statistics, not security
    means = sorted(mean([values[rng.randrange(n)] for _ in range(n)]) for _ in range(resamples))
    lo = means[int(((1 - level) / 2) * resamples)]
    hi = means[min(resamples - 1, int((1 - (1 - level) / 2) * resamples))]
    return mean(values), lo, hi


def paired_diff_ci(
    a: Sequence[float], b: Sequence[float], *, resamples: int = RESAMPLES, seed: int = SEED
) -> tuple[float, float, float]:
    """Mean of ``a - b`` over the same questions, with a bootstrap interval. Needs equal lengths."""
    if len(a) != len(b):
        raise ValueError("paired samples must have equal length")
    return bootstrap_ci([x - y for x, y in zip(a, b, strict=True)], resamples=resamples, seed=seed)


TAXONOMY: Final = (
    "leak",  # content the asker may not read was retrieved
    "citation_failure",  # a returned citation did not resolve to the stored text
    "degraded",  # a model stage failed and the pipeline fell back
    "not_retrieved",  # eligible question, no gold source in the top 5
    "partially_retrieved",  # some but not all gold sources in the top 5
)


def label(row: Row) -> str | None:
    """The first applicable failure label, in order of severity, or ``None`` for a clean row."""
    if row.leak:
        return "leak"
    if not row.citations_resolve:
        return "citation_failure"
    if row.degraded:
        return "degraded"
    if row.eligible and row.recall_at_5 is not None:
        if row.recall_at_5 == 0.0:
            return "not_retrieved"
        if row.recall_at_5 < 1.0:
            return "partially_retrieved"
    return None


def taxonomy(rows: Sequence[Row]) -> dict[str, list[str]]:
    """Failure label -> question ids, in question order."""
    out: dict[str, list[str]] = {name: [] for name in TAXONOMY}
    for r in rows:
        name = label(r)
        if name is not None:
            out[name].append(r.qid)
    return out
