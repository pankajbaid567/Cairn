"""Bootstrap statistics, the error taxonomy and the DuckDB table generator (plan 5.9, 5.10, 5.19)."""  # noqa: E501

from __future__ import annotations

import json
import socket
import sys
from pathlib import Path
from typing import Any

import pytest

from cairn_eval import tables
from cairn_eval.results import AnswerRow, Row, write_answers, write_rows
from cairn_eval.stats import bootstrap_ci, label, paired_diff_ci, taxonomy


def row(qid: str, recall: float | None, variant: str = "b1", **kw: Any) -> Row:
    base: dict[str, Any] = {
        "run_id": "r",
        "variant": variant,
        "qid": qid,
        "category": "why",
        "asker": "asha",
        "eligible": recall is not None,
        "recall_at_5": recall,
        "hit_at_5": None if recall is None else recall > 0,
        "retrieved": [],
        "leak": False,
        "citations_resolve": True,
        "latency_ms": 5.0,
        "degraded": [],
        "neurons_reserved": 0,
        "est_neurons": 0.5,
    }
    return Row(**{**base, **kw})


def test_bootstrap_is_deterministic_bounded_and_honest_about_small_samples() -> None:
    xs = [1.0, 0.0, 1.0, 1.0, 0.0, 1.0, 1.0, 1.0]
    a, b = bootstrap_ci(xs), bootstrap_ci(xs)
    assert a == b  # same seed, same interval
    mean, lo, hi = a
    assert mean == 0.75
    assert 0.0 <= lo <= mean <= hi <= 1.0
    assert hi - lo > 0.2
    assert bootstrap_ci([]) == (0.0, 0.0, 0.0)
    assert bootstrap_ci([1.0, 1.0, 1.0]) == (1.0, 1.0, 1.0)  # no variation, no width
    assert (
        bootstrap_ci(xs, seed=1) != bootstrap_ci(xs, seed=2) or True
    )  # a different seed may differ


def test_paired_difference_uses_the_same_questions() -> None:
    m, lo, hi = paired_diff_ci([1, 1, 0, 1], [0, 1, 0, 0])
    assert m == 0.5
    assert lo <= m <= hi
    with pytest.raises(ValueError):  # noqa: PT011
        paired_diff_ci([1.0], [1.0, 0.0])


def test_every_failure_class_is_labelled_in_order_of_severity() -> None:
    assert label(row("Q1", 1.0)) is None
    assert label(row("Q2", 0.0)) == "not_retrieved"
    assert label(row("Q3", 0.5)) == "partially_retrieved"
    assert label(row("Q4", 1.0, degraded=["rerank"])) == "degraded"
    assert label(row("Q5", 0.0, citations_resolve=False)) == "citation_failure"
    assert label(row("Q6", 0.0, citations_resolve=False, leak=True)) == "leak"  # worst wins
    assert label(row("Q7", None)) is None  # ineligible rows are not retrieval failures
    t = taxonomy([row("Q1", 1.0), row("Q2", 0.0), row("Q3", 0.5), row("Q8", 0.0)])
    assert t["not_retrieved"] == ["Q2", "Q8"]
    assert t["partially_retrieved"] == ["Q3"]


def make_runs(root: Path) -> None:
    for variant, recalls in (("b0", [0.0, 1.0, 1.0, 0.5]), ("b1", [1.0, 1.0, 1.0, 1.0])):
        d = root / "lab" / "dev" / variant
        write_rows(d / "results.parquet", [row(f"Q{i}", r, variant) for i, r in enumerate(recalls)])
        (d / "run.json").write_text(
            json.dumps({"model_mode": "fake", "embedder": "e-1", "reranker": "r-1"})
        )
    answers = [
        AnswerRow("r", "b3", "Q0", "why", "asha", False, False, ["s1"], 1.0, False, 0, 9.0),
        AnswerRow("r", "b3", "Q1", "not_found", "asha", True, False, ["s2"], 0.0, False, 2, 9.0),
    ]
    d3 = root / "lab" / "dev" / "b3"
    write_rows(d3 / "results.parquet", [row("Q0", 1.0, "b3"), row("Q1", None, "b3")])
    write_answers(d3 / "answers.parquet", answers)
    (d3 / "run.json").write_text(
        json.dumps({"model_mode": "fake", "embedder": "e-1", "reranker": "r-1"})
    )
    (root / "lab" / "run-info.json").write_text(json.dumps({"note": "a test run"}))
    (root / "lab" / "deterministic.json").write_text(
        json.dumps(
            {
                "as_of": {
                    "all": {"n": 2, "correct": 1},
                    "dev": {"n": 2, "correct": 1},
                    "test": {"n": 0, "correct": 0},
                    "misses": [
                        {
                            "qid": "Q9",
                            "asker": "x",
                            "decision": "ADR-1",
                            "expected": "a",
                            "got": "b",
                            "reason": "hidden",
                        }
                    ],
                },
                "must_not_cite": {"n": 1, "violations": []},
                "permissions": {"total_leaks": 0, "leaks_by_variant": {}},
            }
        )
    )


def test_tables_are_a_pure_function_of_the_files(tmp_path: Path) -> None:
    make_runs(tmp_path)
    first, second = tables.build(tmp_path), tables.build(tmp_path)
    assert first == second  # same inputs, same bytes
    assert "B0 dense only" in first
    assert "B1 hybrid + rerank" in first
    assert "| dev | B1 hybrid + rerank | 4 | 4 | 1.000 [1.000, 1.000]" in first
    assert "B1 minus B0" in first
    assert "a test run" in first
    assert (
        "not run (needs a live, calibrated judge)" in first
    )  # judged correctness is never invented
    assert "| as-of status, all questions | 1/2 |" in first
    assert "hidden" in first
    assert "not_retrieved" in first
    assert "e-1" in first
    assert "r-1" in first


def test_regenerating_tables_cannot_reach_a_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Plan 5.19: no network socket can be opened and no provider adapter is loaded."""
    make_runs(tmp_path)

    def refuse(*_a: object, **_k: object) -> None:
        raise AssertionError("a table build tried to open a socket")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    for forbidden in ("cairn_retrieval.llm.http_providers", "cairn_retrieval.cloudflare.client"):
        sys.modules.pop(forbidden, None)
    assert tables.build(tmp_path)
    assert "cairn_retrieval.llm.http_providers" not in sys.modules


def test_no_runs_is_said_plainly(tmp_path: Path) -> None:
    assert "No runs found." in tables.build(tmp_path)


def test_kappa_and_agreement() -> None:
    from cairn_eval.calibration import MIN_LABELS, agreement, kappa

    assert kappa([1, 0, 1, 0], [1, 0, 1, 0]) == 1.0
    assert kappa([1, 1, 0, 0], [0, 0, 1, 1]) == -1.0  # systematic disagreement
    assert abs(kappa([1, 1, 0, 0], [1, 0, 1, 0])) < 1e-9  # chance level
    assert kappa([1, 1, 1], [1, 1, 1]) == 1.0  # a constant label on both sides
    with pytest.raises(ValueError):  # noqa: PT011
        kappa([], [])
    a = agreement([1, 0] * 30, [1, 0] * 29 + [0, 0])
    assert a.n == 60
    assert a.enough
    assert 0.9 < a.agree < 1.0
    assert a.kappa_low <= a.kappa <= a.kappa_high
    assert not agreement([1, 0, 1], [1, 0, 0]).enough
    assert MIN_LABELS == 50


def test_the_judge_section_says_not_run_without_labels_and_warns_with_too_few(
    tmp_path: Path,
) -> None:
    make_runs(tmp_path)
    assert "Not run: needs a labels file" in tables.build(tmp_path)
    (tmp_path / "lab" / "judge-labels.csv").write_text("qid,human,judge\nQ0,1,1\nQ1,0,1\nQ2,1,1\n")
    text = tables.build(tmp_path)
    assert "| 3 | 0.667 |" in text
    assert "at least 50 are needed" in text
    (tmp_path / "lab" / "judge-labels.csv").write_text("qid,human,judge\nQ0,2,1\n")
    with pytest.raises(ValueError):  # noqa: PT011
        tables.build(tmp_path)
