"""Properties 24 and 25 (design): metrics and the file formats of the eval harness."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from cairn_eval.metrics import hit_at_k, percentile, recall_at_k
from cairn_eval.questions import (
    QuestionRecord,
    export_questions,
    json_schema,
    read_questions,
    write_questions,
)
from cairn_eval.results import Row, read_rows, summarize, write_rows, write_run
from cairn_eval.seed import load_kestrel

IDS = st.lists(st.sampled_from(list("abcdefgh")), min_size=0, max_size=12)
GOLD = st.sets(st.sampled_from(list("abcdefgh")), min_size=1, max_size=5)


@given(GOLD, IDS, st.integers(min_value=1, max_value=12))
def test_recall_is_bounded_and_monotone_in_k(gold: set[str], ranked: list[str], k: int) -> None:
    r1, r2 = recall_at_k(gold, ranked, k), recall_at_k(gold, ranked, k + 1)
    assert 0.0 <= r1 <= r2 <= 1.0
    assert hit_at_k(gold, ranked, k) == (r1 > 0)


@given(GOLD, IDS)
def test_recall_ignores_order_inside_the_top_k(gold: set[str], ranked: list[str]) -> None:
    top = ranked[:5]
    assert recall_at_k(gold, top, 5) == recall_at_k(gold, list(reversed(top)), 5)
    assert recall_at_k(gold, ranked, 5) == recall_at_k(gold, [*ranked, "zzz"], 5)


def test_recall_needs_gold() -> None:
    with pytest.raises(ValueError):  # noqa: PT011
        recall_at_k([], ["a"], 5)
    assert recall_at_k(["a", "b"], ["a", "x", "b"], 2) == 0.5


def test_percentile() -> None:
    assert percentile([], 95) == 0.0
    assert percentile([5.0], 50) == 5.0
    assert percentile([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 50) == 5
    assert percentile([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 95) == 10


def _manifest(rendered: bool) -> dict:
    scenario = load_kestrel()
    return {
        "sources": {
            s.id: {"status": "rendered" if rendered else "skipped_github_object"}
            for s in scenario.sources
        }
    }


def test_question_export_round_trips_and_keeps_only_rendered_gold(tmp_path: Path) -> None:
    scenario = load_kestrel()
    records = export_questions(scenario, _manifest(True), "dev")
    assert len(records) == 30
    assert [r.qid for r in records] == sorted(r.qid for r in records)
    path = tmp_path / "q.jsonl"
    write_questions(path, records)
    assert read_questions(path) == records
    assert json.loads(path.read_text().splitlines()[0])["format"] == "cairn-questions-v1"
    nothing = export_questions(scenario, _manifest(False), "dev")
    assert all(r.gold_in_scope == [] for r in nothing)
    assert not any(r.eligible for r in nothing)
    assert "qid" in json_schema()["properties"]


def test_agent_askers_act_for_their_person() -> None:
    q = QuestionRecord(qid="Q001", split="dev", category="why", asker="agent:asha", text="t")
    assert q.person == "asha"


def test_bad_question_line_names_the_line(tmp_path: Path) -> None:
    p = tmp_path / "q.jsonl"
    p.write_text('{"nope": 1}\n', encoding="utf-8")
    with pytest.raises(ValueError, match=r"q\.jsonl:1"):
        read_questions(p)


def _row(qid: str, recall: float | None, **kw: object) -> Row:
    base = dict(
        run_id="r",
        variant="b1",
        qid=qid,
        category="why",
        asker="asha",
        eligible=recall is not None,
        recall_at_5=recall,
        hit_at_5=None if recall is None else recall > 0,
        retrieved=["a", "b"],
        leak=False,
        citations_resolve=True,
        latency_ms=10.0,
    )
    return Row(**{**base, **kw})  # type: ignore[arg-type]


def test_results_round_trip_through_parquet_and_summarize(tmp_path: Path) -> None:
    rows = [
        _row("Q001", 1.0),
        _row("Q002", 0.5, latency_ms=30.0),
        _row("Q003", None, leak=True),
        _row("Q004", 0.0, degraded=["dense"], neurons_reserved=7),
    ]
    write_rows(tmp_path / "r.parquet", rows)
    assert read_rows(tmp_path / "r.parquet") == rows
    s = summarize(rows, fts_filled=2)
    assert s["eligible"] == 3
    assert s["recall_at_5"] == 0.5
    assert s["leaks"] == 1
    assert s["degraded_rows"] == 1
    assert s["neurons_reserved"] == 7
    assert s["b0_fts_filled"] == 2
    write_run(tmp_path / "out", {"run_id": "r", "variant": "b1", "model_mode": "fake"}, rows, s)
    assert {p.name for p in (tmp_path / "out").iterdir()} == {
        "results.parquet",
        "run.json",
        "summary.json",
        "summary.md",
    }
    assert json.loads((tmp_path / "out" / "summary.json").read_text())["recall_at_5"] == 0.5
