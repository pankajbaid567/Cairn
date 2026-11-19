"""Run the PEP benchmark: retrieval ablation, then answers and an AI judge.

Needs the local database (make db-up), the corpus and questions (scripts/realbench_corpus.py,
scripts/realbench_questions.py) and AWS credentials for Bedrock. It seeds a throwaway database,
embeds every chunk with both Titan v2 and Cohere v3, and writes var/realbench/results.json.

    AWS_PROFILE=... CAIRN_BEDROCK_DAILY_USD=10 uv run --with awscrt python scripts/realbench_run.py
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import random
import re
import sys
import time
from pathlib import Path
from typing import Any

import psycopg
from psycopg_pool import AsyncConnectionPool

from cairn_connectors import docs_folder
from cairn_connectors.formats.directory import parse_directory
from cairn_connectors.store import apply_directory, import_document
from cairn_core.bootstrap import bootstrap
from cairn_core.config import SearchSettings, load_settings
from cairn_core.embed_dim import ensure_embedding_dimension
from cairn_core.testing import connect_admin, load_config, temporary_database
from cairn_eval.seed import _embed
from cairn_retrieval.bedrock.adapters import BedrockEmbedder, BedrockLlmReranker
from cairn_retrieval.bedrock.client import BedrockClient
from cairn_retrieval.citations import parse_citation
from cairn_retrieval.pipeline import search
from cairn_retrieval.registry import AccessMode
from cairn_retrieval.runtime import build_providers
from cairn_retrieval.types import RetrievalConfig
from cairn_retrieval.workflow import answer_workflow

TITAN = "amazon.titan-embed-text-v2:0"
COHERE = "cohere.embed-english-v3"
JUDGE = "qwen.qwen3-32b-v1:0"
RERANK = "openai.gpt-oss-20b-1:0"
OUT = Path("var/realbench")

VARIANTS: list[dict[str, Any]] = [
    {"name": "lexical", "emb": "titan", "variant": "b1", "rerank": False, "dense_k": 0},
    {"name": "dense_titan", "emb": "titan", "variant": "b0", "rerank": False},
    {"name": "dense_cohere", "emb": "cohere", "variant": "b0", "rerank": False},
    {"name": "hybrid_titan", "emb": "titan", "variant": "b1", "rerank": False},
    {"name": "hybrid_cohere", "emb": "cohere", "variant": "b1", "rerank": False},
    {"name": "hybrid_titan_rerank", "emb": "titan", "variant": "b1", "rerank": True},
    {"name": "hybrid_cohere_rerank", "emb": "cohere", "variant": "b1", "rerank": True},
]


def pep_of(external_id: str) -> int:
    m = re.search(r"pep-(\d{4})", external_id)
    return int(m.group(1)) if m else -1


def docs_in_order(cands: list[Any]) -> list[int]:
    return list(dict.fromkeys(pep_of(c.external_id) for c in cands))


def rank_metrics(ranked: list[int], gold: list[int]) -> dict[str, float]:
    pos = next((i + 1 for i, n in enumerate(ranked) if n in gold), None)
    return {
        "hit5": 1.0 if pos and pos <= 5 else 0.0,
        "hit10": 1.0 if pos and pos <= 10 else 0.0,
        "mrr10": 1.0 / pos if pos and pos <= 10 else 0.0,
        "ndcg10": 1.0 / math.log2(pos + 1) if pos and pos <= 10 else 0.0,
    }


def boot_ci(values: list[float], seed: int = 1, n: int = 2000) -> tuple[float, float, float]:
    rng = random.Random(seed)
    m = sum(values) / len(values)
    means = sorted(sum(rng.choices(values, k=len(values))) / len(values) for _ in range(n))
    return m, means[int(0.025 * n)], means[int(0.975 * n)]


def paired_diff(
    a: list[float], b: list[float], seed: int = 2, n: int = 2000
) -> tuple[float, float, float]:
    d = [x - y for x, y in zip(a, b, strict=True)]
    return boot_ci(d, seed, n)


async def gather_limited(coros: list[Any], limit: int = 4) -> list[Any]:
    gate = asyncio.Semaphore(limit)

    async def run(c: Any) -> Any:
        async with gate:
            return await c

    return await asyncio.gather(*(run(c) for c in coros))


JUDGE_PROMPT = (
    "You grade an answer from a question-answering system about Python Enhancement Proposals.\n"
    "Question: {q}\nReference answer: {gold}\nSystem answer: {ans}\n"
    "Evidence the system cited (quotes):\n{quotes}\n\n"
    "Grade two things. correct: does the system answer agree with the reference answer on the "
    "point asked (yes, partial, or no)? grounded: is every claim of the system answer supported by "
    "the cited quotes (yes or no)? Reply with JSON only: "
    '{{"correct":"yes|partial|no","grounded":"yes|no"}}'
)


async def judge(
    client: BedrockClient, q: dict[str, Any], ans: str, quotes: list[str]
) -> dict[str, str]:
    prompt = JUDGE_PROMPT.format(
        q=q["text"],
        gold=q["gold_answer"],
        ans=ans,
        quotes="\n".join(f"- {t[:400]}" for t in quotes) or "none",
    )
    for _ in range(2):
        try:
            r = await client.converse(
                JUDGE,
                system="You output JSON only.",
                user=prompt,
                max_tokens=300,
                temperature=0.0,
                estimate_micro_usd=1500,
            )
            m = re.search(
                r"\{.*\}", re.sub(r"<reasoning>.*?</reasoning>", "", r.text, flags=re.S), re.S
            )
            d = json.loads(m.group(0)) if m else {}
            if d.get("correct") in ("yes", "partial", "no") and d.get("grounded") in ("yes", "no"):
                return {"correct": d["correct"], "grounded": d["grounded"]}
        except Exception:
            continue
    return {"correct": "error", "grounded": "error"}


async def main_async(args: argparse.Namespace) -> None:
    peps = json.loads((OUT / "peps.json").read_text())
    questions = json.loads((OUT / "questions.json").read_text())
    s = load_settings(SearchSettings)
    client = BedrockClient(s)
    titan = BedrockEmbedder(client, TITAN, 1024)
    cohere = BedrockEmbedder(client, COHERE, 1024)
    embs = {"titan": titan, "cohere": cohere}
    reranker = BedrockLlmReranker(client, RERANK, reasoning_effort="low")
    providers = build_providers(s)
    config = load_config()
    admin = connect_admin(config)
    started = time.time()
    results: dict[str, Any] = {
        "date": time.strftime("%Y-%m-%d"),
        "n_questions": len(questions),
        "n_peps": len(peps),
    }
    with admin:
        bootstrap(admin, config.settings)
        with temporary_database(admin, config) as db:
            with db.connect_owner() as owner:
                ensure_embedding_dimension(owner, 1024)
            assert db.ingest_dsn is not None
            with psycopg.connect(db.ingest_dsn, prepare_threshold=None) as conn:
                apply_directory(
                    conn, parse_directory((OUT / "corpus/directory/directory.json").read_text())
                )
                outcomes = [import_document(conn, d) for d in docs_folder.load(OUT / "corpus/docs")]
            bad = [o.error for o in outcomes if o.error]
            if bad:
                raise RuntimeError(f"rejected: {bad[:3]}")
            with db.connect_owner() as owner:
                results["chunks"] = owner.execute(
                    "SELECT count(*) FROM cairn.chunks WHERE is_current"
                ).fetchone()[0]
                ids = {
                    str(r[0]).removeprefix("user:"): int(r[1])
                    for r in owner.execute(
                        "SELECT external_id, principal_id FROM cairn.principals WHERE external_id LIKE 'user:%'"
                    ).fetchall()
                }
            print("chunks", results["chunks"], flush=True)
            for name, e in embs.items():
                t0 = time.time()
                n = await _embed(db.ingest_dsn, e)
                print(f"embedded {n} with {name} in {time.time() - t0:.0f}s", flush=True)
            results["embed_usd_so_far"] = None
            pool: AsyncConnectionPool[Any] = AsyncConnectionPool(
                db.app_dsn, min_size=1, max_size=6, kwargs={"prepare_threshold": None}, open=False
            )
            await pool.open()
            try:
                rows: dict[str, list[dict[str, Any]]] = {}
                for v in VARIANTS:
                    cfg_kwargs: dict[str, Any] = {"variant": v["variant"], "limit": 10}
                    if "dense_k" in v:
                        cfg_kwargs["dense_k"] = v["dense_k"]
                    cfg = RetrievalConfig(**cfg_kwargs)
                    rr = reranker if v["rerank"] else None

                    async def one(
                        q: dict[str, Any],
                        v: dict[str, Any] = v,
                        cfg: RetrievalConfig = cfg,
                        rr: Any = rr,
                    ) -> dict[str, Any]:
                        t0 = time.perf_counter()
                        res = await search(
                            pool,
                            embs[v["emb"]],
                            rr,
                            principal_id=ids[q["asker"]],
                            query=q["text"],
                            cfg=cfg,
                        )
                        ranked = docs_in_order(res.ranked)
                        return {
                            "id": q["id"],
                            "ranked": ranked,
                            "ms": (time.perf_counter() - t0) * 1000,
                            "degraded": list(res.degraded),
                            "leak": any(n in ranked for n in q["must_not_cite"]),
                        }

                    t0 = time.time()
                    rows[v["name"]] = await gather_limited([one(q) for q in questions])
                    print(f"variant {v['name']} done in {time.time() - t0:.0f}s", flush=True)
                results["rows"] = rows
                # answers with the full system (Titan or Cohere is chosen by the flag), judged afterwards
                emb = embs[args.answer_embedder]
                cfg = RetrievalConfig(variant="b1", limit=10, min_rerank_score=args.min_score)

                async def answer(q: dict[str, Any]) -> dict[str, Any]:
                    t0 = time.perf_counter()
                    try:
                        flow = await answer_workflow(
                            pool,
                            emb,
                            reranker,
                            providers.answer,
                            principal_id=ids[q["asker"]],
                            question=q["text"],
                            cfg=cfg,
                            mode=AccessMode.RLS_FIRST,
                            searched=("docs_folder",),
                            max_tokens=600,
                        )
                    except Exception as exc:
                        return {"id": q["id"], "error": type(exc).__name__}
                    a = flow.answer
                    cited = [pep_of(parse_citation(c.id).external_id) for c in a.citations]
                    return {
                        "id": q["id"],
                        "abstained": a.abstained,
                        "answer": a.answer,
                        "cited": cited,
                        "quotes": [c.quote for c in a.citations],
                        "degraded": list(flow.search.degraded),
                        "retrieved": docs_in_order(flow.search.ranked),
                        "packed": [pep_of(c.external_id) for c in flow.packed],
                        "ms": (time.perf_counter() - t0) * 1000,
                    }

                t0 = time.time()
                answers = await gather_limited([answer(q) for q in questions], limit=3)
                print(f"answers done in {time.time() - t0:.0f}s", flush=True)
                by_id = {q["id"]: q for q in questions}

                async def grade(r: dict[str, Any]) -> None:
                    q = by_id[r["id"]]
                    if "error" in r or r["abstained"] or q["expected"] != "answer":
                        return
                    r["judge"] = await judge(client, q, r["answer"], r["quotes"])

                await gather_limited([grade(r) for r in answers], limit=4)
                results["answers"] = answers
                results["answer_embedder"] = args.answer_embedder
                results["min_rerank_score"] = args.min_score
            finally:
                await pool.close()
    results["seconds"] = round(time.time() - started)
    Path(args.out).write_text(json.dumps(results, indent=1) + "\n")
    print("wrote", args.out, flush=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(OUT / "results.json"))
    ap.add_argument("--answer-embedder", default="titan", choices=["titan", "cohere"])
    ap.add_argument("--min-score", default=2.0, type=float)
    asyncio.run(main_async(ap.parse_args()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
