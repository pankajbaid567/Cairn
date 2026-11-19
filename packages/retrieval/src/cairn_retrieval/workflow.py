"""The bounded answer workflow (plan 4.1, 4.10; D14; ADR-0017).

Seven steps in a fixed order, each a plain async function: analyze, plan, retrieve, expand,
rerank, compose, verify. There is no loop, no agent choosing tools and no action tool: the model is
called once, with only the evidence the caller may see, passed as delimited data with source and
tier labels. Each step runs inside an observer context (the API supplies one that opens a trace
span), and its name and duration are returned so a slow answer can be explained.

``retrieve`` is the whole search pipeline: the ACL-filtered database stage, fusion and the model
rerank are one function, so its time includes the rerank model call. The ``rerank`` step here is
what follows it: applying the rerank score threshold and packing the evidence into the budget.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass, replace
from datetime import date
from typing import Any, Final

from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from cairn_retrieval.access import principal_transaction
from cairn_retrieval.answer import AnswerBody, compose_answer, not_found
from cairn_retrieval.cloudflare.client import WorkersAiError
from cairn_retrieval.llm.types import LlmProvider
from cairn_retrieval.models.protocols import Embedder, Reranker
from cairn_retrieval.packing import pack
from cairn_retrieval.pipeline import search as run_search
from cairn_retrieval.registry import AccessMode
from cairn_retrieval.types import Candidate, RetrievalConfig, SearchResult
from cairn_retrieval.verify import DECISION_KEY, verify_answer

STEPS: Final = ("analyze", "plan", "retrieve", "expand", "rerank", "compose", "verify")
Observer = Callable[[str], AbstractContextManager[Any]]
_YEAR_DATE: Final = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")


class WorkflowError(Exception):
    """A model call failed in ``step``. ``cause`` is the provider error (its code is safe to log)."""  # noqa: E501

    def __init__(self, step: str, cause: WorkersAiError) -> None:
        super().__init__(f"{step}: {cause.code}")
        self.step, self.cause = step, cause


@dataclass(frozen=True)
class Analysis:
    question: str
    decision_keys: tuple[str, ...]
    dates: tuple[str, ...]


@dataclass(frozen=True)
class Plan:
    cfg: RetrievalConfig
    expand_decisions: bool


@dataclass
class WorkflowResult:
    answer: AnswerBody
    packed: list[Candidate]
    search: SearchResult
    steps: list[tuple[str, float]]


def analyze(question: str) -> Analysis:
    """Pull out what the question names: decision keys and ISO dates. Pure, no model."""
    keys = tuple(dict.fromkeys(f"ADR-{n}" for n in DECISION_KEY.findall(question)))
    return Analysis(question, keys, tuple(dict.fromkeys(_YEAR_DATE.findall(question))))


def plan(analysis: Analysis, cfg: RetrievalConfig) -> Plan:
    """Choose the retrieval settings. Questions that name decisions also look up those decisions."""
    return Plan(cfg, expand_decisions=bool(analysis.decision_keys))


async def expand(
    pool: AsyncConnectionPool[AsyncConnection[Any]],
    principal_id: int,
    keys: Sequence[str],
    *,
    acting_for: int | None = None,
) -> dict[str, date]:
    """First valid date of each named decision the caller can see (the verifier's date floor)."""
    if not keys:
        return {}
    from cairn_retrieval.decisions import service

    out: dict[str, date] = {}
    async with principal_transaction(pool, principal_id, acting_for=acting_for, read_only=True) as (
        conn,
        snap,
    ):
        for key in keys:
            events = await service.timeline(conn, snap, key)
            starts = [
                e["valid_at"].date() for e in events or [] if e["event"] in service.STATE_AFTER
            ]
            if starts:
                out[key] = min(starts)
    return out


async def answer_workflow(
    pool: AsyncConnectionPool[AsyncConnection[Any]],
    embedder: Embedder,
    reranker: Reranker | None,
    provider: LlmProvider,
    *,
    principal_id: int,
    acting_for: int | None = None,
    question: str,
    cfg: RetrievalConfig,
    mode: AccessMode,
    searched: Sequence[str],
    max_tokens: int,
    recorded_before: Any = None,
    observer: Observer | None = None,
) -> WorkflowResult:
    """Run the seven steps. Model errors propagate (the API maps them); nothing is retried here."""
    watch = observer or (lambda _name: nullcontext())
    steps: list[tuple[str, float]] = []

    def timed(name: str) -> _Timer:
        return _Timer(name, steps, watch(name))

    with timed("analyze"):
        analysis = analyze(question)
    with timed("plan"):
        the_plan = plan(analysis, cfg)
    with timed("retrieve"):
        try:
            result = await run_search(
                pool,
                embedder,
                reranker,
                principal_id=principal_id,
                query=question,
                cfg=the_plan.cfg,
                mode=mode,
                recorded_before=recorded_before,
            )
        except WorkersAiError as exc:
            raise WorkflowError("retrieve", exc) from exc
    with timed("expand"):
        starts = (
            await expand(pool, principal_id, analysis.decision_keys, acting_for=acting_for)
            if the_plan.expand_decisions
            else {}
        )
    with timed("rerank"):
        degraded_rerank = "rerank" in result.degraded or reranker is None
        min_score = None if degraded_rerank else cfg.min_rerank_score
        packed = pack(
            result.ranked,
            budget=cfg.pack_budget_tokens,
            max_chunks=cfg.pack_max_chunks,
            per_item=cfg.pack_per_item_first_pass,
            min_score=min_score,
        )
    with timed("compose"):
        try:
            answer, _ = await compose_answer(
                question,
                packed,
                provider,
                searched,
                degraded=result.degraded,
                max_tokens=max_tokens,
            )
        except WorkersAiError as exc:
            raise WorkflowError("compose", exc) from exc
    with timed("verify"):
        answer, _report = verify_answer(answer, packed, decision_starts=starts)
    if answer.abstained:
        # The not-found reply is one constant (PD1-14): no timings or verifier details ride on it.
        return WorkflowResult(not_found(searched, result.degraded), packed, result, steps)
    return WorkflowResult(replace(answer, trace=tuple(steps)), packed, result, steps)


class _Timer:
    def __init__(
        self, name: str, sink: list[tuple[str, float]], ctx: AbstractContextManager[Any]
    ) -> None:
        self._name, self._sink, self._ctx = name, sink, ctx
        self._t0 = 0.0

    def __enter__(self) -> None:
        self._t0 = time.perf_counter()
        self._ctx.__enter__()

    def __exit__(self, *exc: object) -> None:
        self._sink.append((self._name, round((time.perf_counter() - self._t0) * 1000, 1)))
        self._ctx.__exit__(None, None, None)
