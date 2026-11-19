"""B2 and B3 against a real database, and re-run reproducibility (plan 5.4, 5.12)."""

from __future__ import annotations

import json
from typing import Any

import pytest

from cairn_eval.cli import _on_fresh_db
from cairn_eval.seed import DEFAULT_SCENARIO

pytestmark = pytest.mark.integration
DROP = {"latency_p50_ms", "latency_p95_ms"}


def stable(summaries: dict[str, dict[str, Any]]) -> dict[str, Any]:
    return json.loads(
        json.dumps({v: {k: x for k, x in s.items() if k not in DROP} for v, s in summaries.items()})
    )


def test_b2_and_b3_run_and_a_rerun_reproduces_every_deterministic_metric() -> None:
    variants = ["b0", "b1", "b2", "b3"]
    first = _on_fresh_db(variants, 14, "fake", None, DEFAULT_SCENARIO)
    second = _on_fresh_db(variants, 14, "fake", None, DEFAULT_SCENARIO)
    assert stable(first) == stable(second)  # same seeds, same cached/fake models, same numbers
    for v in variants:
        assert first[v]["leaks"] == 0
        assert first[v]["citation_failures"] == 0
    b3 = first["b3"]["answers"]
    assert b3["answers"] == 14
    assert b3["leaks"] == 0
    assert (
        first["b2"]["recall_at_5"] >= first["b1"]["recall_at_5"] - 1e-9
    )  # promotion never hurts here
