"""Judge calibration (plan 5.5): how often a model judge agrees with a human.

A person labels at least 50 answers as correct (1) or not (0); the judge labels the same answers.
The file ``<runs>/<label>/judge-labels.csv`` has the columns ``qid,human,judge``. This module reads
it and reports raw agreement and Cohen's kappa (agreement beyond chance), each with a bootstrap
interval. Until the file exists, and holds at least ``MIN_LABELS`` rows, the tables say so and
report no judged metric: an uncalibrated judge is not evidence.
"""

from __future__ import annotations

import csv
import random
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

MIN_LABELS: Final = 50
RESAMPLES: Final = 2000
SEED: Final = 20261104


@dataclass(frozen=True)
class Agreement:
    n: int
    agree: float
    kappa: float
    kappa_low: float
    kappa_high: float

    @property
    def enough(self) -> bool:
        return self.n >= MIN_LABELS


def kappa(human: Sequence[int], judge: Sequence[int]) -> float:
    """Cohen's kappa for two binary raters. 1.0 when both always agree; 0.0 for chance-level."""
    n = len(human)
    if n == 0 or n != len(judge):
        raise ValueError("need two equal, non-empty label lists")
    observed = sum(h == j for h, j in zip(human, judge, strict=True)) / n
    p_h, p_j = sum(human) / n, sum(judge) / n
    expected = p_h * p_j + (1 - p_h) * (1 - p_j)
    if expected == 1.0:
        return 1.0  # both raters gave one constant label: perfect agreement, kappa undefined
    return (observed - expected) / (1 - expected)


def agreement(human: Sequence[int], judge: Sequence[int]) -> Agreement:
    n = len(human)
    k = kappa(human, judge)
    rng = random.Random(SEED)  # noqa: S311 - statistics, not security
    ks = sorted(
        kappa(
            *zip(*[(human[i], judge[i]) for i in (rng.randrange(n) for _ in range(n))], strict=True)
        )
        for _ in range(RESAMPLES)
    )
    return Agreement(
        n,
        sum(h == j for h, j in zip(human, judge, strict=True)) / n,
        k,
        ks[int(0.025 * RESAMPLES)],
        ks[int(0.975 * RESAMPLES) - 1],
    )


def read_labels(path: Path) -> tuple[list[int], list[int]]:
    human: list[int] = []
    judge: list[int] = []
    with path.open(newline="", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            h, j = int(r["human"]), int(r["judge"])
            if h not in (0, 1) or j not in (0, 1):
                raise ValueError(f"labels must be 0 or 1 (question {r.get('qid')!r})")
            human.append(h)
            judge.append(j)
    return human, judge
