"""``cairn-s1 bench``: vector ACL-filter strategies at 1, 5, 20 and 50% selectivity (design §13).

Loads synthetic clustered unit vectors into ``cairn.embeddings`` of a throwaway database that has
the real Phase 1 schema, HNSW index and RLS policy, then runs every named strategy as
``cairn_app`` with ``prepare_threshold=None`` through the registered ``dense_candidates`` query.
Recall@10 is measured against ``exact_prefilter`` under the same visibility. Nothing here calls a
model: vectors are synthetic, so the run spends no neurons, and the command refuses a non-local
database host without ``--allow-remote`` so it can never fill Neon (PD1-29).

Success (Appendix C, Proposed target): recall@10 ratio at least 0.95 at 5% selectivity with p95 at
most 150 ms.
"""

from __future__ import annotations

import datetime as dt
import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import numpy as np

from cairn_core.hosts import require_local_or_allowed
from cairn_core.testing import connect_admin, load_config, temporary_database
from cairn_retrieval import queries  # noqa: F401  (registers the queries)
from cairn_retrieval.registry import registered, render
from cairn_retrieval.vector import STRATEGIES, vector_literal

MODEL_KEY = "bench-synthetic"
NOISE_GROUP = 9_999_999
BASE_GROUP = 1_000_000


@dataclass
class Result:
    strategy: str
    selectivity: float
    visible_rows: int
    recall_at_10: float
    rows_returned_mean: float
    p50_ms: float
    p95_ms: float


def _vectors(rng: np.random.Generator, n: int, dim: int, centers: np.ndarray) -> np.ndarray:
    pick = rng.integers(0, len(centers), n)
    v = centers[pick] + rng.normal(0, 0.35, (n, dim))
    return (v / np.linalg.norm(v, axis=1, keepdims=True)).astype(np.float32)


def run(
    rows: int,
    dim: int,
    queries_n: int,
    selectivities: tuple[float, ...],
    seed: int,
    out_dir: Path,
    allow_remote: bool,
) -> dict[str, Any]:
    config = load_config()
    require_local_or_allowed("cairn-s1 bench", config.target.host, allow_remote=allow_remote)
    rng = np.random.default_rng(seed)
    centers = rng.normal(0, 1, (20, dim))
    data = _vectors(rng, rows, dim, centers)
    queries_v = _vectors(rng, queries_n, dim, centers)
    groups = {s: BASE_GROUP + i for i, s in enumerate(selectivities)}
    draws = rng.random((rows, len(selectivities)))
    with (
        connect_admin(config) as admin,
        temporary_database(admin, config, apply_migrations=True) as db,
    ):
        if dim != 384:
            raise SystemExit("the Phase 1 schema is vector(384); use --dim 384")
        started = time.perf_counter()
        with db.connect_admin() as owner:  # the admin bypasses RLS; COPY FROM is refused under it
            with (
                owner.cursor() as cur,
                cur.copy(
                    "COPY cairn.embeddings (chunk_id, model_key, embedding, acl) FROM STDIN"
                ) as copy,
            ):
                for i in range(rows):
                    acl = [NOISE_GROUP] + [
                        groups[s] for j, s in enumerate(selectivities) if draws[i, j] < s
                    ]
                    copy.write_row(
                        (
                            i + 1,
                            MODEL_KEY,
                            vector_literal(data[i].tolist()),
                            "{" + ",".join(map(str, sorted(acl))) + "}",
                        )
                    )
            owner.execute("ANALYZE cairn.embeddings")
            size_row = owner.execute(
                "SELECT pg_relation_size('cairn.embeddings_hnsw_idx')"
            ).fetchone()
            if size_row is None:
                raise RuntimeError("pg_relation_size returned no row")
            index_mb = size_row[0] / 1e6
        load_s = time.perf_counter() - started
        results: list[Result] = []
        with db.connect_app() as app:
            app.prepare_threshold = None
            exact_q = render(registered()["dense_exact_candidates"])
            hnsw_q = render(registered()["dense_candidates"])
            for s in selectivities:
                group = [groups[s]]
                app.execute(
                    "SELECT set_config('cairn.principal_groups', %s, false)",
                    ("{" + str(group[0]) + "}",),
                )
                visible = int(app.execute("SELECT count(*) FROM cairn.embeddings").fetchone()[0])  # type: ignore[index]
                truth = []
                for q in queries_v:
                    params = {
                        "qv": vector_literal(q.tolist()),
                        "model": MODEL_KEY,
                        "k": 10,
                        "acl_groups": group,
                    }
                    truth.append({r[0] for r in app.execute(exact_q, params).fetchall()})
                for strat in STRATEGIES.values():
                    lat: list[float] = []
                    rec: list[float] = []
                    ret: list[int] = []
                    for q, expect in zip(queries_v, truth, strict=True):
                        params = {
                            "qv": vector_literal(q.tolist()),
                            "model": MODEL_KEY,
                            "k": 10,
                            "acl_groups": group,
                        }
                        with app.transaction():
                            for name, value in strat.settings.items():
                                app.execute("SELECT set_config(%s, %s, true)", (name, value))
                            t0 = time.perf_counter()
                            got = {
                                r[0]
                                for r in app.execute(
                                    exact_q if strat.exact else hnsw_q, params
                                ).fetchall()
                            }
                            lat.append((time.perf_counter() - t0) * 1000)
                        ret.append(len(got))
                        rec.append(len(got & expect) / max(1, len(expect)))
                    lat.sort()
                    results.append(
                        Result(
                            strat.name,
                            s,
                            visible,
                            round(sum(rec) / len(rec), 4),
                            round(sum(ret) / len(ret), 2),
                            round(lat[len(lat) // 2], 2),
                            round(lat[max(0, int(len(lat) * 0.95) - 1)], 2),
                        )
                    )
    report = {
        "date": dt.date.today().isoformat(),
        "rows": rows,
        "dim": dim,
        "queries": queries_n,
        "seed": seed,
        "selectivities": list(selectivities),
        "hnsw_index_mb": round(index_mb, 1),
        "load_seconds": round(load_s, 1),
        "image": "pgvector/pgvector:0.8.6-pg18",
        "results": [asdict(r) for r in results],
        "note": "synthetic vectors, no model calls; local Docker VM, not Neon: latency on Neon can differ",  # noqa: E501
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = report["date"]
    (out_dir / f"{stamp}-results.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    lines = [
        f"# S1 report {stamp}",
        "",
        f"{rows} rows, {dim} dimensions, {queries_n} queries per cell, seed {seed}. "
        f"HNSW index {report['hnsw_index_mb']} MB, load {report['load_seconds']} s. {report['note']}.",  # noqa: E501
        "",
        "| strategy | selectivity | visible rows | recall@10 | rows returned | p50 ms | p95 ms |",
        "|---|---|---|---|---|---|---|",
    ]
    lines += [
        f"| {r.strategy} | {r.selectivity:.0%} | {r.visible_rows} | {r.recall_at_10} | {r.rows_returned_mean} | {r.p50_ms} | {r.p95_ms} |"  # noqa: E501
        for r in results
    ]
    ok = [
        r for r in results if r.selectivity == 0.05 and r.recall_at_10 >= 0.95 and r.p95_ms <= 150
    ]
    lines += [
        "",
        "Strategies meeting recall@10 >= 0.95 with p95 <= 150 ms at 5%: "
        + (", ".join(r.strategy for r in ok) or "none"),
    ]
    (out_dir / f"{stamp}-report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report
