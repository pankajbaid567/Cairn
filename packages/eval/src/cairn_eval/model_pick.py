"""Pick the answer and judge models (plan 1.14; ADR-0012). Resumable across UTC days.

Each answer candidate answers the same packs of the first 20 dev questions; a judge from a different
model family grades every answer. Rows go to ``rows.jsonl`` as they finish, and ``--resume`` skips
pairs already recorded, so a pick can span days if it would pass the daily allowance.

Choice: highest judged correctness among candidates with p95 answer latency at most 5 s, then the
lower cost per query. The budget is 1.5 times the chosen model's p95 cost, rounded up to the next
10 neurons (Workers AI) or the next $0.001 (paid).
"""

from __future__ import annotations

import json
import math
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cairn_core.model_family import model_family
from cairn_eval.judge import judge_answer
from cairn_eval.metrics import percentile
from cairn_eval.questions import QuestionRecord
from cairn_retrieval.answer import compose_answer
from cairn_retrieval.llm.cost import call_cost
from cairn_retrieval.llm.types import LlmProvider
from cairn_retrieval.types import Candidate

LATENCY_MAX_MS = 5000.0


class SameFamily(Exception):
    """The judge must come from a different model family than the answer model."""


@dataclass
class Pick:
    candidate: str
    correct_rate: float
    p95_ms: float
    cost_p95_neurons: float | None
    cost_p95_usd: float | None
    eligible: bool


def check_families(answer: LlmProvider, judge: LlmProvider) -> None:
    if "fake" in (answer.provider, judge.provider):
        return
    if model_family(answer.provider, answer.model) == model_family(judge.provider, judge.model):
        raise SameFamily(f"{answer.model} and {judge.model} are from the same family")


def load_rows(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


async def run_pick(
    out_dir: Path,
    questions: Sequence[QuestionRecord],
    packs: dict[str, list[Candidate]],
    gold_decisions: dict[str, list[str]],
    answer_candidates: dict[str, LlmProvider],
    judge: LlmProvider,
    searched: Sequence[str],
    *,
    resume: bool = False,
) -> list[dict[str, Any]]:
    out_dir.mkdir(parents=True, exist_ok=True)  # noqa: ASYNC240
    rows_path = out_dir / "rows.jsonl"
    rows = load_rows(rows_path) if resume else []
    if not resume and rows_path.exists():
        rows_path.unlink()
    done = {(r["candidate"], r["qid"]) for r in rows}
    for name, provider in answer_candidates.items():
        check_families(provider, judge)
        for q in questions:
            if (name, q.qid) in done:
                continue
            started = time.perf_counter()
            body, resp = await compose_answer(q.text, packs[q.qid], provider, searched)
            latency = (time.perf_counter() - started) * 1000
            verdict, jin, jout = await judge_answer(
                judge, q, gold_decisions.get(q.qid, []), body.to_json()
            )
            cost = call_cost(resp) if resp is not None else None
            row = {
                "candidate": name,
                "qid": q.qid,
                "correct": verdict.correct,
                "valid_verdict": verdict.valid,
                "supported": verdict.supported_claims,
                "unsupported": verdict.unsupported_claims,
                "abstained": body.abstained,
                "latency_ms": round(latency, 2),
                "neurons": cost.neurons if cost else None,
                "usd": cost.usd if cost else None,
                "judge_tokens_in": jin,
                "judge_tokens_out": jout,
            }
            rows.append(row)
            with rows_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, sort_keys=True) + "\n")
    return rows


def choose(rows: Sequence[dict[str, Any]]) -> tuple[list[Pick], Pick | None]:
    picks: list[Pick] = []
    for name in sorted({r["candidate"] for r in rows}):
        mine = [r for r in rows if r["candidate"] == name]
        lat = [r["latency_ms"] for r in mine]
        n = [r["neurons"] for r in mine if r["neurons"] is not None]
        u = [r["usd"] for r in mine if r["usd"] is not None]
        p95 = percentile(lat, 95)
        picks.append(
            Pick(
                name,
                round(sum(1 for r in mine if r["correct"]) / len(mine), 4),
                round(p95, 2),
                percentile(n, 95) if n else None,
                percentile(u, 95) if u else None,
                p95 <= LATENCY_MAX_MS,
            )
        )
    ok = [p for p in picks if p.eligible]
    if not ok:
        return picks, None
    best = max(p.correct_rate for p in ok)
    top = [p for p in ok if p.correct_rate == best]
    return picks, min(
        top,
        key=lambda p: (
            p.cost_p95_neurons if p.cost_p95_neurons is not None else (p.cost_p95_usd or 0.0) * 1e5
        ),
    )


def budget_for(p: Pick) -> dict[str, float | int]:
    if p.cost_p95_neurons is not None:
        return {"CAIRN_COST_BUDGET_NEURONS": int(math.ceil(1.5 * p.cost_p95_neurons / 10.0) * 10)}
    return {"CAIRN_COST_BUDGET_USD": math.ceil(1.5 * (p.cost_p95_usd or 0.0) * 1000) / 1000}
