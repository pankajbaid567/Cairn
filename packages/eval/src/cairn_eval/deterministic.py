"""Deterministic suites (plan 5.13): permissions, as-of and leaks.

These have exact expected answers in the scenario, so they report pass/fail counts and never need
a model judge. They are reported separately from the judged metrics so a reader cannot mistake a
judge's opinion for a checked fact.

* **as-of**: for each question with an expected lifecycle, ask the decision service for the status
  at the question's valid and recorded dates *as the asker* and compare. A mismatch is listed with
  its reason; one reason is real and expected: the asker cannot read the source of the event that
  changes the status, so Cairn (correctly) answers from what they can see.
* **must-not-cite**: an as-of search with ``recorded_before`` must not return sources recorded
  after that date.
* **permissions**: no retrieved source outside the asker's oracle-visible set, on any question.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any

from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from cairn_eval.runners import principal_ids
from cairn_eval.scenario.model import Question, Scenario
from cairn_retrieval.access import principal_transaction
from cairn_retrieval.decisions import service
from cairn_retrieval.models.protocols import Embedder, Reranker
from cairn_retrieval.pipeline import search
from cairn_retrieval.temporal import day_end
from cairn_retrieval.types import RetrievalConfig


@dataclass
class AsOfResult:
    qid: str
    split: str
    asker: str
    decision: str
    expected: str
    got: str
    reason: str = ""

    @property
    def ok(self) -> bool:
        return self.expected == self.got


def _person(q: Question) -> str:
    return q.asker.removeprefix("agent:")


def _hidden_sources(
    scenario: Scenario,
    manifest: dict[str, Any],
    person: str,
    decision: str,
    valid: dt.date,
    recorded: dt.date,
) -> list[str]:
    """Sources of the lifecycle events that count at (valid, recorded) which ``person`` cannot read."""  # noqa: E501
    from cairn_eval.scenario.access import readers
    from cairn_eval.scenario.oracle import is_lifecycle, recorded_time, valid_time

    dec = next(d for d in scenario.decisions if d.id == decision)
    hidden = []
    for e in dec.timeline:
        if not is_lifecycle(e) or valid_time(e) > valid or recorded_time(e, scenario) > recorded:
            continue
        # the scenario knows every source's scope, rendered or not (pull requests are not imported
        # until the GitHub connector runs, but their events are still stored with the repo's access)
        scope = (
            manifest["sources"].get(e.source, {}).get("scope") or scenario.source(e.source).scope
        )
        if scope and person not in readers(scenario, scope, scenario.meta.reference_date):
            hidden.append(e.source)
    return hidden


async def as_of_accuracy(
    pool: AsyncConnectionPool[AsyncConnection[Any]],
    scenario: Scenario,
    manifest: dict[str, Any] | None = None,
) -> list[AsOfResult]:
    ids = await principal_ids(pool)
    out: list[AsOfResult] = []
    for q in scenario.questions:
        if q.as_of is None or not q.expected.lifecycle:
            continue
        valid, recorded = q.as_of.valid, q.as_of.recorded
        async with principal_transaction(pool, ids[_person(q)], read_only=True) as (conn, snap):
            for key, expected in q.expected.lifecycle.items():
                state = await service.state_as_of(conn, snap, key, valid, recorded)
                got = "not_visible" if state is None else state.status
                result = AsOfResult(q.id, q.split, q.asker, key, expected, got)
                if not result.ok and manifest is not None:
                    hidden = _hidden_sources(scenario, manifest, _person(q), key, valid, recorded)
                    result.reason = (
                        f"the asker cannot read {', '.join(hidden)}, which carries the status change"  # noqa: E501
                        if hidden
                        else "unexplained: investigate"
                    )
                out.append(result)
    return out


async def must_not_cite(
    pool: AsyncConnectionPool[AsyncConnection[Any]],
    scenario: Scenario,
    manifest: dict[str, Any],
    embedder: Embedder,
    reranker: Reranker | None,
) -> list[dict[str, Any]]:
    """Per question with ``must_not_cite``: did an as-of search return any forbidden source?"""
    ids = await principal_ids(pool)
    by_external = {
        info["external_id"]: sid
        for sid, info in manifest["sources"].items()
        if info.get("external_id")
    }
    out = []
    for q in scenario.questions:
        if not q.expected.must_not_cite or q.as_of is None:
            continue
        result = await search(
            pool,
            embedder,
            reranker,
            principal_id=ids[_person(q)],
            query=q.text,
            cfg=RetrievalConfig(variant="b1"),
            recorded_before=day_end(q.as_of.recorded),
        )
        cited = {by_external.get(c.external_id, c.external_id) for c in result.ranked}
        bad = sorted(cited & set(q.expected.must_not_cite))
        out.append({"qid": q.id, "split": q.split, "forbidden_returned": bad, "ok": not bad})
    return out


def summarize(
    as_of: list[AsOfResult], cites: list[dict[str, Any]], leaks_by_variant: dict[str, int]
) -> dict[str, Any]:
    def acc(rows: list[AsOfResult]) -> dict[str, Any]:
        return {
            "n": len(rows),
            "correct": sum(r.ok for r in rows),
            "accuracy": round(sum(r.ok for r in rows) / len(rows), 4) if rows else None,
        }

    return {
        "as_of": {
            "all": acc(as_of),
            "dev": acc([r for r in as_of if r.split == "dev"]),
            "test": acc([r for r in as_of if r.split == "test"]),
            "misses": [r.__dict__ for r in as_of if not r.ok],
        },
        "must_not_cite": {
            "n": len(cites),
            "violations": [c for c in cites if not c["ok"]],
        },
        "permissions": {
            "leaks_by_variant": leaks_by_variant,
            "total_leaks": sum(leaks_by_variant.values()),
        },
        "reference_date": dt.date.today().isoformat(),
    }
