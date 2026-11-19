"""``cairn-s3 run``: measure embedding and rerank candidates (design §14; plan 1.2).

For each embedder: dense Recall@5 over the parsed and chunked corpus with each asker's visibility
from the scenario oracle, batch latency and the estimated neurons. For each reranker: Recall@5 of
the dense top 30 after rerank. The choice rule (Proposed targets): among embedders within 0.03
Recall@5 of the best take the fewest neurons per 1,000 input tokens, then the lower p95; keep a
reranker only if its Recall@5 is at least the no-rerank value and its p95 for 50 passages is at
most 1,500 ms.

Modes: ``fake`` and ``replay`` (read-only model cache) make no network call; ``live`` (user-run)
calls Workers AI, writes every vector and score to the cache and exits 3 without a call when the
estimate does not fit the laptop allowance (PD1-27).
"""

from __future__ import annotations

import asyncio
import json
import math
import time
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from cairn_connectors import chat_export, docs_folder, meeting_notes
from cairn_connectors.chunking import chunk_chat, chunk_markdown
from cairn_eval.metrics import percentile, recall_at_k
from cairn_eval.questions import QuestionRecord
from cairn_eval.runners import RunContext, visible_sources
from cairn_retrieval.cloudflare.budget import NEURON_RATES, estimate_neurons
from cairn_retrieval.models.protocols import Embedder, Reranker

EMBED_TOLERANCE = 0.03
RERANK_P95_MAX_MS = 1500.0
RERANK_PASSAGES = 50


@dataclass
class EmbedResult:
    model_key: str
    dim: int
    recall_at_5: float
    batch_p50_ms: float
    batch_p95_ms: float
    neurons_corpus: int
    neurons_per_1k_tokens: float | None


@dataclass
class RerankResult:
    model_key: str
    recall_no_rerank: float
    recall_reranked: float
    p95_ms: float
    keep: bool


def corpus_chunks(corpus: Path, manifest: dict[str, Any]) -> list[tuple[str, str]]:
    """``(source id, text to embed)`` for every chunk of the rendered corpus."""
    by_external = {
        i["external_id"]: sid for sid, i in manifest["sources"].items() if i.get("external_id")
    }
    docs = [
        *chat_export.load(corpus / "chat")[0],
        *meeting_notes.load(corpus / "meetings"),
        *docs_folder.load(corpus / "docs"),
    ]
    out: list[tuple[str, str]] = []
    for d in docs:
        chunker = chunk_chat if d.source_kind == "chat_thread" else chunk_markdown
        for c in chunker(d.text):
            out.append((by_external.get(d.external_id, d.external_id), f"{d.title}\n{c.text}"))
    return out


def _cos(a: Sequence[float], b: Sequence[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na, nb = math.sqrt(sum(x * x for x in a)) or 1.0, math.sqrt(sum(y * y for y in b)) or 1.0
    return dot / (na * nb)


async def measure_embedder(
    ctx: RunContext,
    chunks: list[tuple[str, str]],
    questions: Sequence[QuestionRecord],
    embedder: Embedder,
    model_id: str | None,
) -> tuple[EmbedResult, dict[str, list[tuple[float, int]]]]:
    lat: list[float] = []
    vectors: list[list[float]] = []
    texts = [t for _, t in chunks]
    for i in range(0, len(texts), 50):
        t0 = time.perf_counter()
        vectors.extend(await embedder.embed_documents(texts[i : i + 50]))
        lat.append((time.perf_counter() - t0) * 1000)
    qvecs = [await embedder.embed_query(q.text) for q in questions]
    recalls: list[float] = []
    ranked_per_q: dict[str, list[tuple[float, int]]] = {}
    for q, qv in zip(questions, qvecs, strict=True):
        allowed = visible_sources(ctx, q.person)
        scored = sorted(
            ((_cos(qv, v), i) for i, v in enumerate(vectors) if chunks[i][0] in allowed),
            reverse=True,
        )
        ranked_per_q[q.qid] = scored
        if q.eligible:
            top5 = list(dict.fromkeys(chunks[i][0] for _, i in scored))[:5]
            recalls.append(recall_at_k(q.gold_in_scope, top5, 5))
    tokens = sum(max(1, -(-len(t.encode("utf-8")) // 3)) for t in texts)
    known = model_id is not None and model_id in NEURON_RATES
    neurons = estimate_neurons(model_id, tokens) if model_id is not None and known else 0
    rate = NEURON_RATES.get(model_id) if model_id is not None else None
    per_1k = None
    if rate is not None:
        per_1k = round(estimate_neurons(model_id or "", 1000), 3)
    res = EmbedResult(
        embedder.model_key,
        embedder.dim,
        round(sum(recalls) / max(1, len(recalls)), 4),
        round(percentile(lat, 50), 2),
        round(percentile(lat, 95), 2),
        neurons,
        per_1k,
    )
    return res, ranked_per_q


async def measure_reranker(
    ctx: RunContext,
    chunks: list[tuple[str, str]],
    questions: Sequence[QuestionRecord],
    ranked_per_q: dict[str, list[tuple[float, int]]],
    reranker: Reranker,
) -> RerankResult:
    base: list[float] = []
    after: list[float] = []
    lat: list[float] = []
    for q in questions:
        if not q.eligible:
            continue
        top = [i for _, i in ranked_per_q[q.qid][:RERANK_PASSAGES]]
        base.append(
            recall_at_k(q.gold_in_scope, list(dict.fromkeys(chunks[i][0] for i in top))[:5], 5)
        )
        t0 = time.perf_counter()
        scores = await reranker.score(q.text, [chunks[i][1] for i in top])
        lat.append((time.perf_counter() - t0) * 1000)
        order = [i for _, i in sorted(zip(scores, top, strict=True), key=lambda p: (-p[0], p[1]))]
        after.append(
            recall_at_k(q.gold_in_scope, list(dict.fromkeys(chunks[i][0] for i in order))[:5], 5)
        )
    r0, r1 = sum(base) / max(1, len(base)), sum(after) / max(1, len(after))
    p95 = percentile(lat, 95)
    return RerankResult(
        reranker.model_key,
        round(r0, 4),
        round(r1, 4),
        round(p95, 2),
        r1 >= r0 and p95 <= RERANK_P95_MAX_MS,
    )


def choose_embedder(results: Sequence[EmbedResult]) -> EmbedResult:
    best = max(r.recall_at_5 for r in results)
    near = [r for r in results if r.recall_at_5 >= best - EMBED_TOLERANCE]
    return min(
        near,
        key=lambda r: (
            r.neurons_per_1k_tokens if r.neurons_per_1k_tokens is not None else 0.0,
            r.batch_p95_ms,
        ),
    )


def choose_reranker(results: Sequence[RerankResult]) -> RerankResult | None:
    keep = [r for r in results if r.keep]
    return max(keep, key=lambda r: r.recall_reranked) if keep else None


def write_report(
    out: Path, embeds: list[EmbedResult], reranks: list[RerankResult], mode: str, date: str
) -> dict[str, Any]:
    chosen = choose_embedder(embeds)
    rr = choose_reranker(reranks)
    report = {
        "date": date,
        "mode": mode,
        "embedders": [asdict(e) for e in embeds],
        "rerankers": [asdict(r) for r in reranks],
        "chosen_embedder": chosen.model_key,
        "chosen_reranker": rr.model_key if rr else "none",
        "note": "fake models measure nothing about Workers AI" if mode == "fake" else "",
    }
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{date}-{mode}-results.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    lines: list[str] = [
        f"# S3 report {date} ({mode})",
        "",
        str(report["note"]),
        "",
        "| embedder | dim | Recall@5 | batch p50 ms | batch p95 ms | neurons (corpus) |",
        "|---|---|---|---|---|---|",
    ]
    lines += [
        f"| {e.model_key} | {e.dim} | {e.recall_at_5} | {e.batch_p50_ms} | {e.batch_p95_ms} | {e.neurons_corpus} |"  # noqa: E501
        for e in embeds
    ]
    lines += [
        "",
        "| reranker | Recall@5 without | Recall@5 with | p95 ms (50 passages) | keep |",
        "|---|---|---|---|---|",
    ]
    lines += [
        f"| {r.model_key} | {r.recall_no_rerank} | {r.recall_reranked} | {r.p95_ms} | {r.keep} |"
        for r in reranks
    ]
    lines += [
        "",
        f"Chosen embedder: {report['chosen_embedder']}. Chosen reranker: {report['chosen_reranker']}.",  # noqa: E501
    ]
    (out / f"{date}-{mode}-report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report


def run_sync(*args: Any, **kwargs: Any) -> Any:
    return asyncio.run(*args, **kwargs)
