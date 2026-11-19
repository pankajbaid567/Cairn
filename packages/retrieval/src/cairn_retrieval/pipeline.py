"""Search: lexical and dense candidates, fusion, hosted rerank (design §15; PD1-19, PD1-28).

Model calls are awaited with no database connection held: the query is embedded first, the
database work runs in one REPEATABLE READ transaction, and the rerank happens after it. A provider
failure that the search can survive skips that stage and says so in ``degraded`` (dense, rerank);
a configuration or code fault (``not_configured``, ``auth``, ``bad_request``) propagates.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from cairn_retrieval.access import principal_transaction
from cairn_retrieval.cloudflare.client import WorkersAiError
from cairn_retrieval.fusion import rrf
from cairn_retrieval.lexical import lexical_query_text, lexical_terms
from cairn_retrieval.models.protocols import Embedder, Reranker
from cairn_retrieval.registry import AccessMode, run_registered
from cairn_retrieval.types import (
    DEGRADING_ERRORS,
    Candidate,
    Degraded,
    RetrievalConfig,
    SearchResult,
)
from cairn_retrieval.vector import apply_strategy, get_strategy, vector_literal

log = logging.getLogger("cairn.retrieval")

# Importing registers the queries.
from cairn_retrieval import queries as _queries  # noqa: E402, F401


def _degrade(stage: Degraded, exc: WorkersAiError, degraded: list[Degraded]) -> None:
    if exc.code not in DEGRADING_ERRORS:
        raise exc
    degraded.append(stage)
    # Stage and error code only: never the question, a chunk or a prompt (PD1-22).
    log.warning("search.degraded", extra={"stage": stage, "error_code": exc.code})


def _candidates(rows: list[dict[str, Any]], order: list[int]) -> list[Candidate]:
    by_id = {int(r["chunk_id"]): r for r in rows}
    out: list[Candidate] = []
    for rank, chunk_id in enumerate(order, start=1):
        r = by_id.get(chunk_id)
        if r is None:  # not visible any more (cannot happen inside one snapshot) or filtered
            continue
        out.append(
            Candidate(
                chunk_id=chunk_id,
                item_id=int(r["item_id"]),
                external_id=str(r["external_id"]),
                revision_hash=str(r["revision_hash"]),
                start=int(r["start"]),
                end=int(r["end"]),
                text=str(r["text"]),
                title=str(r["title"]),
                source_kind=str(r["source_kind"]),
                fused_rank=rank,
            )
        )
    return out


async def _db_stage(
    conn: AsyncConnection[Any],
    snapshot: Any,
    query: str,
    qvec: list[float] | None,
    model_key: str,
    cfg: RetrievalConfig,
    mode: AccessMode,
    strip_acl: bool,
    recorded_before: datetime | None = None,
) -> tuple[list[int], int, list[Candidate]]:
    terms = lexical_terms(query)
    fts_ids: list[int] = []
    if terms:
        rows = await run_registered(
            conn,
            "fts_candidates" if recorded_before is None else "fts_candidates_asof",
            {"q": lexical_query_text(terms), "k": cfg.lexical_k}
            | ({} if recorded_before is None else {"recorded_before": recorded_before}),
            snapshot,
            mode=mode,
            strip_acl=strip_acl,
        )
        fts_ids = [int(r["chunk_id"]) for r in rows]
    dense_ids: list[int] = []
    if qvec is not None:
        strategy = get_strategy(cfg.vector_strategy)
        await apply_strategy(conn, strategy)
        name = "dense_exact_candidates" if strategy.exact else "dense_candidates"
        extra: dict[str, Any] = {}
        if (
            recorded_before is not None
        ):  # time travel always scans exactly: the visible set is small and filtered
            name, extra = "dense_exact_candidates_asof", {"recorded_before": recorded_before}
        rows = await run_registered(
            conn,
            name,
            {"qv": vector_literal(qvec), "model": model_key, "k": cfg.dense_k} | extra,
            snapshot,
            mode=mode,
            strip_acl=strip_acl,
        )
        dense_ids = [int(r["chunk_id"]) for r in rows]
    filled = 0
    if cfg.variant == "b1":
        order = [i for i, _ in rrf([fts_ids, dense_ids], cfg.rrf_k)]
        top = order[: cfg.rerank_n]
    else:  # b0: dense order; FTS only fills slots when dense returned fewer than `limit` rows
        order = list(dense_ids)
        if len(order) < cfg.limit:
            for chunk_id in fts_ids:
                if chunk_id not in order:
                    order.append(chunk_id)
                    filled += 1
                if len(order) >= cfg.limit:
                    break
        top = order[: cfg.limit]
    if not top:
        return [], filled, []
    details = await run_registered(
        conn, "chunk_details", {"ids": top}, snapshot, mode=mode, strip_acl=strip_acl
    )
    return top, filled, _candidates(details, top)


async def search(
    pool: AsyncConnectionPool[AsyncConnection[Any]],
    embedder: Embedder,
    reranker: Reranker | None,
    *,
    principal_id: int,
    acting_for: int | None = None,
    query: str,
    cfg: RetrievalConfig | None = None,
    mode: AccessMode = AccessMode.RLS_FIRST,
    strip_acl: bool = False,
    recorded_before: datetime | None = None,
) -> SearchResult:
    cfg = cfg or RetrievalConfig()
    degraded: list[Degraded] = []
    qvec: list[float] | None = None
    try:
        qvec = await embedder.embed_query(query)
    except WorkersAiError as exc:
        _degrade("dense", exc, degraded)
    async with principal_transaction(pool, principal_id, acting_for=acting_for, read_only=True) as (
        conn,
        snapshot,
    ):
        _, filled, cands = await _db_stage(
            conn, snapshot, query, qvec, embedder.model_key, cfg, mode, strip_acl, recorded_before
        )
    ranked = list(cands)
    if cfg.variant == "b1" and reranker is not None and cands:
        try:
            scores = await reranker.score(query, [c.text for c in cands])
        except WorkersAiError as exc:
            _degrade("rerank", exc, degraded)
        else:
            scored = [
                Candidate(**{**c.__dict__, "rerank_score": s})  # frozen dataclass copy
                for c, s in zip(cands, scores, strict=True)
            ]
            ranked = sorted(scored, key=lambda c: (-(c.rerank_score or 0.0), c.fused_rank))
    elif cfg.variant == "b1" and reranker is None:
        pass  # CAIRN_RERANKER=none: the fused order stands (DV-05)
    return SearchResult(
        snapshot=snapshot,
        candidates=cands,
        ranked=ranked[: cfg.limit],
        degraded=tuple(degraded),
        b0_fts_filled=filled,
    )
