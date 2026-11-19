"""Run the B0 and B1 variants through the same library the API uses (design §18; PD1-19).

The searches run as ``cairn_app`` under RLS, so what is measured is the real access path. Each
question is asked as its asker (``agent:x`` acts for ``x``). A degraded row, a leak or a citation
that does not resolve makes the run fail: such a result must never become a baseline.
"""

from __future__ import annotations

import re
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from cairn_eval.cost import query_neurons
from cairn_eval.metrics import hit_at_k, recall_at_k
from cairn_eval.questions import QuestionRecord
from cairn_eval.results import AnswerRow, Row
from cairn_eval.scenario.access import readers
from cairn_eval.scenario.canary import canary_token
from cairn_eval.scenario.model import Scenario
from cairn_retrieval.access import principal_transaction
from cairn_retrieval.answer import build_prompt, citation_id
from cairn_retrieval.citations import parse_citation
from cairn_retrieval.models.protocols import Embedder, Reranker
from cairn_retrieval.pipeline import search
from cairn_retrieval.registry import AccessMode, run_registered
from cairn_retrieval.types import RetrievalConfig

TOP = 10


@dataclass
class RunContext:
    scenario: Scenario
    manifest: dict[str, Any]
    pool: AsyncConnectionPool[AsyncConnection[Any]]
    embedder: Embedder
    reranker: Reranker | None
    answer_provider: Any = None  # an LlmProvider; B3 needs it
    answers: list[AnswerRow] = field(default_factory=list)

    def source_for_external(self) -> dict[str, str]:
        return {
            info["external_id"]: sid
            for sid, info in self.manifest["sources"].items()
            if info.get("external_id")
        }


async def principal_ids(pool: AsyncConnectionPool[AsyncConnection[Any]]) -> dict[str, int]:
    async with pool.connection() as conn:
        rows = await (
            await conn.execute(
                "SELECT external_id, principal_id FROM cairn.principals WHERE kind = 'user'"
            )
        ).fetchall()
        await conn.rollback()
    return {str(r[0]).removeprefix("user:"): int(r[1]) for r in rows}


def visible_sources(ctx: RunContext, person: str) -> set[str]:
    """Source ids the asker may read on the scenario's reference date (the oracle)."""
    out = set()
    for sid, info in ctx.manifest["sources"].items():
        scope = info.get("scope")
        if (
            info["status"] == "rendered"
            and scope
            and person in readers(ctx.scenario, scope, ctx.scenario.meta.reference_date)
        ) or (info["status"] == "rendered" and not scope):
            out.add(sid)
    return out


async def _citations_resolve(ctx: RunContext, principal: int, cands: Sequence[Any]) -> bool:
    if not cands:
        return True
    async with principal_transaction(ctx.pool, principal, read_only=True) as (conn, snap):
        for c in cands:
            cit = parse_citation(citation_id(c))
            rows = await run_registered(
                conn,
                "resolve_citation",
                {
                    "start": cit.start,
                    "len": cit.end - cit.start,
                    "external_id": cit.external_id,
                    "hash8": cit.hash8,
                },
                snap,
            )
            if not rows or rows[0]["quote"] != c.text:
                return False
    return True


_WORD = re.compile(r"[A-Za-z][A-Za-z0-9-]{4,}")
PROMOTE = 2


async def decision_source_refs(ctx: RunContext, principal: int, question: str) -> list[str]:
    """Scenario source ids of the decisions a question is about (B2: the decision index).

    A decision matches when the question names its key, or when at least two distinct words of the
    question appear in its title, question or outcome. Only decisions and events the asker may see
    are read; the sources returned are further limited to those the oracle says the asker may read.
    """
    from cairn_retrieval.decisions import service
    from cairn_retrieval.workflow import analyze

    named = list(analyze(question).decision_keys)
    words = sorted({w.casefold() for w in _WORD.findall(question)}, key=len, reverse=True)[:6]
    hits: dict[str, set[str]] = {}
    async with principal_transaction(ctx.pool, principal, read_only=True) as (conn, snap):
        for w in words:
            for h in await service.search_decisions(conn, snap, w, limit=5):
                hits.setdefault(str(h["key"]), set()).add(w)
        chosen = named + [
            k for k, ws in sorted(hits.items(), key=lambda kv: -len(kv[1])) if len(ws) >= 2
        ]
        refs: list[str] = []
        for key in list(dict.fromkeys(chosen))[:PROMOTE]:
            for e in await service.timeline(conn, snap, key) or []:
                if e["source_ref"]:
                    refs.append(str(e["source_ref"]))
    return list(dict.fromkeys(refs))


async def run_variant(
    ctx: RunContext,
    run_id: str,
    variant: str,
    questions: Sequence[QuestionRecord],
    *,
    cfg: RetrievalConfig | None = None,
    neurons_used: Any = None,
) -> tuple[list[Row], int]:
    """Rows for every question, and how many B0 slots FTS filled."""
    base = cfg or RetrievalConfig()
    search_variant = "b1" if variant in ("b2", "b3") else variant
    cfg = RetrievalConfig(**{**base.__dict__, "variant": search_variant, "limit": TOP})
    ids = await principal_ids(ctx.pool)
    by_external = ctx.source_for_external()
    rows: list[Row] = []
    filled = 0
    for q in questions:
        principal = ids[q.person]
        before = await neurons_used() if neurons_used else 0
        started = time.perf_counter()
        flow = None
        if variant == "b3":
            if ctx.answer_provider is None:
                raise ValueError("variant b3 needs an answer provider")
            from cairn_retrieval.workflow import answer_workflow

            flow = await answer_workflow(
                ctx.pool,
                ctx.embedder,
                ctx.reranker,
                ctx.answer_provider,
                principal_id=principal,
                question=q.text,
                cfg=cfg,
                mode=AccessMode.RLS_FIRST,
                searched=("chat_export", "meeting_notes", "docs_folder"),
                max_tokens=600,
            )
            result = flow.search
        else:
            result = await search(
                ctx.pool, ctx.embedder, ctx.reranker, principal_id=principal, query=q.text, cfg=cfg
            )
        latency = (time.perf_counter() - started) * 1000
        after = await neurons_used() if neurons_used else 0
        filled += result.b0_fts_filled
        retrieved_ext = list(dict.fromkeys(c.external_id for c in result.ranked))
        retrieved = [by_external.get(e, e) for e in retrieved_ext]
        top5 = [by_external.get(c.external_id, c.external_id) for c in result.ranked[:5]]
        allowed = visible_sources(ctx, q.person)
        if variant in ("b2", "b3"):
            promoted = [
                s for s in await decision_source_refs(ctx, principal, q.text) if s in allowed
            ]
            retrieved = list(dict.fromkeys(promoted + retrieved))
            top5 = promoted[:PROMOTE] + top5
        top5_items = list(dict.fromkeys(top5))[:5]
        families = {c.id: c for c in ctx.scenario.canaries}
        needles = []
        for cid in q.must_not_contain:
            c = families.get(cid)
            if c:
                needles.append(
                    c.phrase
                    if c.kind == "phrase"
                    else canary_token(ctx.scenario.meta.canary_seed, c.id, c.family)
                )
        leak = any(sid not in allowed for sid in retrieved) or any(
            n and n in c.text for c in result.ranked for n in needles
        )
        eligible = q.eligible
        rerank_passages = (
            [] if search_variant == "b0" else [c.text for c in result.candidates[: cfg.rerank_n]]
        )
        est = query_neurons(
            q.text,
            rerank_passages,
            answer=variant == "b3",
            answer_prompt=build_prompt(q.text, flow.packed) if flow is not None else "",
        )
        if flow is not None:
            cited_ext = [parse_citation(c.id).external_id for c in flow.answer.citations]
            cited = list(dict.fromkeys(by_external.get(e, e) for e in cited_ext))
            ctx.answers.append(
                AnswerRow(
                    run_id=run_id,
                    variant=variant,
                    qid=q.qid,
                    category=q.category,
                    asker=q.asker,
                    expect_abstain=q.expect_abstain,
                    abstained=flow.answer.abstained,
                    cited=cited,
                    gold_cited_recall=recall_at_k(q.gold_in_scope, cited, max(len(cited), 1))
                    if eligible
                    else 0.0,
                    leak=any(sid not in allowed for sid in cited),
                    dropped_claims=sum(
                        int(v) for v in flow.answer.verification.get("dropped", {}).values()
                    ),
                    latency_ms=latency,
                )
            )
        rows.append(
            Row(
                run_id=run_id,
                variant=variant,
                qid=q.qid,
                category=q.category,
                asker=q.asker,
                eligible=eligible,
                recall_at_5=recall_at_k(q.gold_in_scope, top5_items, 5) if eligible else None,
                hit_at_5=hit_at_k(q.gold_in_scope, top5_items, 5) if eligible else None,
                retrieved=retrieved,
                leak=leak,
                citations_resolve=await _citations_resolve(ctx, principal, result.ranked),
                latency_ms=latency,
                degraded=list(result.degraded),
                neurons_reserved=max(0, after - before),
                est_neurons=est,
            )
        )
    return rows, filled
