"""Run results: Parquet rows, ``run.json`` and a summary (design §18; Property 25).

The Parquet file holds one row per question. ``summary.json`` is derived from the rows only, so
a table can be regenerated from the Parquet file without a provider call.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from cairn_eval.metrics import percentile

_FIELDS: list[tuple[str, pa.DataType]] = [
    ("run_id", pa.string()),
    ("variant", pa.string()),
    ("qid", pa.string()),
    ("category", pa.string()),
    ("asker", pa.string()),
    ("eligible", pa.bool_()),
    ("recall_at_5", pa.float64()),
    ("hit_at_5", pa.bool_()),
    ("retrieved", pa.list_(pa.string())),
    ("leak", pa.bool_()),
    ("citations_resolve", pa.bool_()),
    ("latency_ms", pa.float64()),
    ("degraded", pa.list_(pa.string())),
    ("neurons_reserved", pa.int64()),
    ("est_neurons", pa.float64()),
]
SCHEMA = pa.schema(_FIELDS)


@dataclass
class Row:
    run_id: str
    variant: str
    qid: str
    category: str
    asker: str
    eligible: bool
    recall_at_5: float | None
    hit_at_5: bool | None
    retrieved: list[str]
    leak: bool
    citations_resolve: bool
    latency_ms: float
    degraded: list[str] = field(default_factory=list)
    neurons_reserved: int = 0
    est_neurons: float = 0.0  # estimate from token counts and the published rates (cost.py)


def write_rows(path: Path, rows: list[Row]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    cols = {name: [getattr(r, name) for r in rows] for name in SCHEMA.names}
    pq.write_table(pa.table(cols, schema=SCHEMA), path)


def read_rows(path: Path) -> list[Row]:
    return [Row(**r) for r in pq.read_table(path).to_pylist()]


def summarize(rows: list[Row], *, fts_filled: int = 0) -> dict[str, Any]:
    eligible = [r for r in rows if r.eligible and r.recall_at_5 is not None]
    by_cat: dict[str, list[float]] = {}
    for r in eligible:
        by_cat.setdefault(r.category, []).append(float(r.recall_at_5 or 0.0))
    lat = [r.latency_ms for r in rows]
    mean = lambda xs: (sum(xs) / len(xs)) if xs else 0.0  # noqa: E731
    return {
        "questions": len(rows),
        "eligible": len(eligible),
        "recall_at_5": round(mean([float(r.recall_at_5 or 0.0) for r in eligible]), 4),
        "hit_at_5": round(mean([1.0 if r.hit_at_5 else 0.0 for r in eligible]), 4),
        "by_category": {k: round(mean(v), 4) for k, v in sorted(by_cat.items())},
        "latency_p50_ms": round(percentile(lat, 50), 2),
        "latency_p95_ms": round(percentile(lat, 95), 2),
        "leaks": sum(1 for r in rows if r.leak),
        "citation_failures": sum(1 for r in rows if not r.citations_resolve),
        "degraded_rows": sum(1 for r in rows if r.degraded),
        "b0_fts_filled": fts_filled,
        "neurons_reserved": sum(r.neurons_reserved for r in rows),
        "est_neurons_per_query": round(mean([r.est_neurons for r in rows]), 3),
    }


def write_run(
    out: Path, manifest: dict[str, Any], rows: list[Row], summary: dict[str, Any]
) -> None:
    out.mkdir(parents=True, exist_ok=True)
    write_rows(out / "results.parquet", rows)
    (out / "run.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (out / "summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    lines = [
        f"# Run {manifest.get('run_id', '')} ({manifest.get('variant', '')}, {manifest.get('model_mode', '')})",  # noqa: E501
        "",
    ]
    lines += [f"- {k}: {v}" for k, v in summary.items() if k != "by_category"]
    lines += ["", "| category | Recall@5 |", "|---|---|"]
    lines += [f"| {k} | {v} |" for k, v in summary["by_category"].items()]
    (out / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


_ANSWER_FIELDS: list[tuple[str, pa.DataType]] = [
    ("run_id", pa.string()),
    ("variant", pa.string()),
    ("qid", pa.string()),
    ("category", pa.string()),
    ("asker", pa.string()),
    ("expect_abstain", pa.bool_()),
    ("abstained", pa.bool_()),
    ("cited", pa.list_(pa.string())),
    ("gold_cited_recall", pa.float64()),
    ("leak", pa.bool_()),
    ("dropped_claims", pa.int64()),
    ("latency_ms", pa.float64()),
    ("judge_score", pa.float64()),  # null until a judged run fills it (plan 5.5)
]
ANSWER_SCHEMA = pa.schema(_ANSWER_FIELDS)


@dataclass
class AnswerRow:
    """One composed answer (B3). Judged correctness is ``None`` until a live, calibrated judge runs;
    everything else here is deterministic."""

    run_id: str
    variant: str
    qid: str
    category: str
    asker: str
    expect_abstain: bool
    abstained: bool
    cited: list[str]
    gold_cited_recall: float
    leak: bool
    dropped_claims: int
    latency_ms: float
    judge_score: float | None = None

    @property
    def abstention_correct(self) -> bool:
        return self.abstained == self.expect_abstain


def write_answers(path: Path, rows: list[AnswerRow]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    cols = {name: [getattr(r, name) for r in rows] for name in ANSWER_SCHEMA.names}
    pq.write_table(pa.table(cols, schema=ANSWER_SCHEMA), path)


def read_answers(path: Path) -> list[AnswerRow]:
    return [AnswerRow(**r) for r in pq.read_table(path).to_pylist()]


def summarize_answers(rows: list[AnswerRow]) -> dict[str, Any]:
    """Counts for the composed answers of one B3 run. Correctness by a judge is reported elsewhere."""  # noqa: E501
    eligible = [r for r in rows if not r.expect_abstain]
    return {
        "answers": len(rows),
        "answered": sum(1 for r in rows if not r.abstained),
        "abstention_correct": sum(1 for r in rows if r.abstention_correct),
        "gold_cited_recall": round(
            sum(r.gold_cited_recall for r in eligible) / len(eligible) if eligible else 0.0, 4
        ),
        "leaks": sum(1 for r in rows if r.leak),
        "dropped_claims": sum(r.dropped_claims for r in rows),
    }
