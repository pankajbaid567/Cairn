"""Regenerate every results table from Parquet through DuckDB (plan 5.10, 5.19).

    cairn-eval tables --runs docs/evidence/phase-5/runs --out docs/results.md

The output is a pure function of the files under ``--runs`` (and the S1 study results): the same
inputs give the same bytes. Nothing here imports a provider adapter or opens a network socket, so
regenerating tables can never spend a neuron; a test blocks sockets to check that. Every table that
comes from a run says which model keys produced it and how many neurons were reserved.

Run layout::

    <runs>/<label>/run-info.json              what the label means (mode, date, notes)
    <runs>/<label>/<split>/<variant>/results.parquet   (+ run.json, answers.parquet for b3)
    <runs>/<label>/deterministic.json
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any, Final

import duckdb

from cairn_eval.cost import dollars
from cairn_eval.metrics import percentile
from cairn_eval.results import Row
from cairn_eval.stats import TAXONOMY, bootstrap_ci, label, paired_diff_ci

VARIANTS: Final = ("b0", "b1", "b2", "b3")
VARIANT_NAMES: Final = {
    "b0": "B0 dense only",
    "b1": "B1 hybrid + rerank",
    "b2": "B2 B1 + decision index",
    "b3": "B3 B2 + answer + verifier",
}


def _fmt(mean: float, lo: float, hi: float) -> str:
    return f"{mean:.3f} [{lo:.3f}, {hi:.3f}]"


def _rows(con: duckdb.DuckDBPyConnection, files: list[Path]) -> list[Row]:
    if not files:
        return []
    cols = con.execute(
        "SELECT * FROM read_parquet(?, union_by_name = true) ORDER BY qid",
        [[str(f) for f in files]],
    ).fetchall()
    names = [d[0] for d in con.description or []]
    return [Row(**dict(zip(names, c, strict=True))) for c in cols]


def _runs(root: Path) -> dict[str, dict[str, dict[str, dict[str, Any]]]]:
    """label -> split -> variant -> {rows, manifest, answers}."""
    out: dict[str, dict[str, dict[str, dict[str, Any]]]] = {}
    con = duckdb.connect()
    for run_dir in sorted(p for p in root.glob("*/*/*") if (p / "results.parquet").exists()):
        variant, split, label_ = run_dir.name, run_dir.parent.name, run_dir.parent.parent.name
        entry: dict[str, Any] = {"rows": _rows(con, [run_dir / "results.parquet"])}
        entry["manifest"] = (
            json.loads((run_dir / "run.json").read_text())
            if (run_dir / "run.json").exists()
            else {}
        )
        entry["answers"] = []
        if (run_dir / "answers.parquet").exists():
            from cairn_eval.results import read_answers

            entry["answers"] = read_answers(run_dir / "answers.parquet")
        out.setdefault(label_, {}).setdefault(split, {})[variant] = entry
    con.close()
    return out


def _eligible(rows: list[Row]) -> list[Row]:
    return [r for r in rows if r.eligible and r.recall_at_5 is not None]


def _headline(label_: str, runs: dict[str, dict[str, dict[str, Any]]]) -> list[str]:
    lines = [
        "| split | variant | questions | eligible | Recall@5 (95% CI) | Hit@5 | leaks | citation failures | p50 ms | p95 ms |",  # noqa: E501
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for split in sorted(runs):
        for v in VARIANTS:
            if v not in runs[split]:
                continue
            rows = runs[split][v]["rows"]
            el = _eligible(rows)
            m, lo, hi = bootstrap_ci([float(r.recall_at_5 or 0) for r in el])
            hit = sum(1 for r in el if r.hit_at_5) / len(el) if el else 0.0
            lat = [r.latency_ms for r in rows]
            lines.append(
                f"| {split} | {VARIANT_NAMES[v]} | {len(rows)} | {len(el)} | {_fmt(m, lo, hi)} | {hit:.3f} | "  # noqa: E501
                f"{sum(r.leak for r in rows)} | {sum(not r.citations_resolve for r in rows)} | "
                f"{percentile(lat, 50):.1f} | {percentile(lat, 95):.1f} |"
            )
    return lines


def _paired(runs: dict[str, dict[str, dict[str, Any]]]) -> list[str]:
    lines = [
        "| split | comparison | mean difference in Recall@5 (95% CI) | questions |",
        "|---|---|---|---|",
    ]
    pairs = (("b1", "b0"), ("b2", "b1"), ("b3", "b2"))
    for split in sorted(runs):
        for a, b in pairs:
            if a not in runs[split] or b not in runs[split]:
                continue
            ra = {r.qid: r for r in _eligible(runs[split][a]["rows"])}
            rb = {r.qid: r for r in _eligible(runs[split][b]["rows"])}
            common = sorted(set(ra) & set(rb))
            if not common:
                continue
            m, lo, hi = paired_diff_ci(
                [float(ra[q].recall_at_5 or 0) for q in common],
                [float(rb[q].recall_at_5 or 0) for q in common],
            )
            lines.append(
                f"| {split} | {a.upper()} minus {b.upper()} | {_fmt(m, lo, hi)} | {len(common)} |"
            )
    return lines


def _by_category(runs: dict[str, dict[str, dict[str, Any]]]) -> list[str]:
    cats: defaultdict[tuple[str, str], defaultdict[str, list[float]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for split, by_v in runs.items():
        for v, e in by_v.items():
            for r in _eligible(e["rows"]):
                cats[(split, r.category)][v].append(float(r.recall_at_5 or 0))
    present = [v for v in VARIANTS if any(v in c for c in cats.values())]
    lines = [
        "| split | category | n | " + " | ".join(v.upper() for v in present) + " |",
        "|---|---|---|" + "---|" * len(present),
    ]
    for (split, cat), vals in sorted(cats.items()):
        n = max(len(x) for x in vals.values())
        cells = [f"{sum(vals[v]) / len(vals[v]):.3f}" if vals.get(v) else "-" for v in present]
        lines.append(f"| {split} | {cat} | {n} | " + " | ".join(cells) + " |")
    return lines


def _answers(runs: dict[str, dict[str, dict[str, Any]]]) -> list[str]:
    lines = [
        "| split | answered | abstained | abstention correct | cited-gold recall (95% CI) | leaks | dropped claims | judged correctness |",  # noqa: E501
        "|---|---|---|---|---|---|---|---|",
    ]
    for split in sorted(runs):
        a = runs[split].get("b3", {}).get("answers", [])
        if not a:
            continue
        el = [r for r in a if not r.expect_abstain]
        m, lo, hi = bootstrap_ci([r.gold_cited_recall for r in el])
        judged = [r.judge_score for r in a if r.judge_score is not None]
        lines.append(
            f"| {split} | {sum(not r.abstained for r in a)} | {sum(r.abstained for r in a)} | "
            f"{sum(r.abstention_correct for r in a)}/{len(a)} | {_fmt(m, lo, hi)} | {sum(r.leak for r in a)} | "  # noqa: E501
            f"{sum(r.dropped_claims for r in a)} | {'not run (needs a live, calibrated judge)' if not judged else f'{sum(judged) / len(judged):.3f}'} |"  # noqa: E501
        )
    return lines


def _cost(runs: dict[str, dict[str, dict[str, Any]]]) -> list[str]:
    lines = [
        "| split | variant | estimated neurons / query | estimated USD / 1,000 queries | neurons reserved in the run |",  # noqa: E501
        "|---|---|---|---|---|",
    ]
    for split in sorted(runs):
        for v in VARIANTS:
            if v not in runs[split]:
                continue
            rows = runs[split][v]["rows"]
            per = sum(r.est_neurons for r in rows) / len(rows) if rows else 0.0
            lines.append(
                f"| {split} | {VARIANT_NAMES[v]} | {per:.2f} | {dollars(per * 1000):.4f} | {sum(r.neurons_reserved for r in rows)} |"  # noqa: E501
            )
    return lines


def _taxonomy(runs: dict[str, dict[str, dict[str, Any]]]) -> list[str]:
    lines = [
        "| split | variant | " + " | ".join(TAXONOMY) + " |",
        "|---|---|" + "---|" * len(TAXONOMY),
    ]
    examples: list[str] = []
    for split in sorted(runs):
        for v in VARIANTS:
            if v not in runs[split]:
                continue
            by_label: dict[str, list[str]] = {t: [] for t in TAXONOMY}
            for r in runs[split][v]["rows"]:
                name = label(r)
                if name:
                    by_label[name].append(r.qid)
            lines.append(
                f"| {split} | {v.upper()} | "
                + " | ".join(str(len(by_label[t])) for t in TAXONOMY)
                + " |"
            )
            for t in TAXONOMY:
                if by_label[t]:
                    examples.append(f"- {split} {v.upper()} `{t}`: {', '.join(by_label[t][:5])}")
    return [
        *lines,
        "",
        "Examples (first five question ids per class):",
        "",
        *(examples or ["- none"]),
    ]


def _models(label_: str, runs: dict[str, dict[str, dict[str, Any]]]) -> list[str]:
    seen: dict[tuple[str, ...], set[str]] = defaultdict(set)
    for split, by_v in runs.items():
        for v, e in by_v.items():
            m = e["manifest"]
            seen[(m.get("model_mode", "?"), m.get("embedder", "?"), m.get("reranker", "?"))].add(
                f"{split}/{v}"
            )
    lines = ["| mode | embedder | reranker | runs |", "|---|---|---|---|"]
    for (mode, emb, rer), where in sorted(seen.items()):
        lines.append(f"| {mode} | {emb} | {rer} | {len(where)} |")
    return lines


def _deterministic(path: Path) -> list[str]:
    if not path.exists():
        return ["No deterministic report for this label."]
    d = json.loads(path.read_text())
    a = d["as_of"]
    lines = [
        "| suite | result |",
        "|---|---|",
        f"| as-of status, all questions | {a['all']['correct']}/{a['all']['n']} |",
        f"| as-of status, dev | {a['dev']['correct']}/{a['dev']['n']} |",
        f"| as-of status, test | {a['test']['correct']}/{a['test']['n']} |",
        f"| must-not-cite violations | {len(d['must_not_cite']['violations'])} of {d['must_not_cite']['n']} |",  # noqa: E501
        f"| permission leaks, all variants | {d['permissions']['total_leaks']} |",
    ]
    if a["misses"]:
        lines += [
            "",
            "As-of misses (each is a decision whose status, as the asker can see it, differs from the scenario's expected status):",  # noqa: E501
            "",
            "| question | asker | decision | expected | got | reason |",
            "|---|---|---|---|---|---|",
        ]
        lines += [
            f"| {m['qid']} | {m['asker']} | {m['decision']} | {m['expected']} | {m['got']} | {m.get('reason', '')} |"  # noqa: E501
            for m in a["misses"]
        ]
    return lines


def _judge(path: Path) -> list[str]:
    from cairn_eval.calibration import MIN_LABELS, agreement, read_labels

    if not path.exists():
        return [
            f"Not run: needs a labels file with at least {MIN_LABELS} human labels. Until then no judged metric is reported."  # noqa: E501
        ]
    human, judge = read_labels(path)
    if not human:
        return ["The labels file is empty."]
    a = agreement(human, judge)
    lines = [
        "| labels | raw agreement | Cohen's kappa (95% CI) |",
        "|---|---|---|",
        f"| {a.n} | {a.agree:.3f} | {a.kappa:.3f} [{a.kappa_low:.3f}, {a.kappa_high:.3f}] |",
    ]
    if not a.enough:
        lines += [
            "",
            f"Only {a.n} labels; at least {MIN_LABELS} are needed before the judge counts.",
        ]
    return lines


def _vector_study(path: Path) -> list[str]:
    if not path.exists():
        return ["No vector study results found."]
    d = json.loads(path.read_text())
    rows = d.get("results") or []
    if not rows:
        return ["Vector study file has no rows."]
    keys = [
        k for k in ("strategy", "selectivity", "recall_at_10", "p50_ms", "p95_ms") if k in rows[0]
    ]
    lines = ["| " + " | ".join(keys) + " |", "|" + "---|" * len(keys)]
    lines += ["| " + " | ".join(str(r.get(k, "")) for k in keys) + " |" for r in rows]
    lines += [
        "",
        f"HNSW index size: {d.get('hnsw_index_mb', '?')} MB; {d.get('queries', '?')} queries per cell; {d.get('note', '')}",  # noqa: E501
    ]
    return lines


def build(runs_dir: Path, vector_study: Path | None = None) -> str:
    """The whole results document as text."""
    out = [
        "# Results",
        "",
        "Generated by `make eval-tables` (`cairn-eval tables`) from the Parquet files under "
        f"`{runs_dir.as_posix()}`. Do not edit by hand: rerun the command. Regenerating makes no provider call.",  # noqa: E501
        "",
        "**How to read this.** Intervals are 95% percentile bootstrap over questions (seeded, 2,000 "  # noqa: E501
        "resamples). A run labelled `fake` uses deterministic stand-in models: it checks that the pipeline "  # noqa: E501
        "and the metrics work and gives regression baselines, and says **nothing** about answer quality. "  # noqa: E501
        "Quality numbers come from `live` labels only.",
        "",
    ]
    runs = _runs(runs_dir)
    if not runs:
        return "\n".join([*out, "No runs found."]) + "\n"
    for label_ in sorted(runs):
        info = runs_dir / label_ / "run-info.json"
        meta = json.loads(info.read_text()) if info.exists() else {}
        out += [f"## Run `{label_}`", "", meta.get("note", ""), ""]
        out += ["### Models used", "", *_models(label_, runs[label_]), ""]
        out += ["### Retrieval", "", *_headline(label_, runs[label_]), ""]
        out += ["### Paired differences", "", *_paired(runs[label_]), ""]
        out += ["### By category (Recall@5)", "", *_by_category(runs[label_]), ""]
        out += ["### Answers (B3)", "", *_answers(runs[label_]), ""]
        out += ["### Cost per query", "", *_cost(runs[label_]), ""]
        out += [
            "### Deterministic suites (reported separately from judged metrics)",
            "",
            *_deterministic(runs_dir / label_ / "deterministic.json"),
            "",
        ]
        out += ["### Judge calibration", "", *_judge(runs_dir / label_ / "judge-labels.csv"), ""]
        out += ["### Failures and error classes", "", *_taxonomy(runs[label_]), ""]
    if vector_study is not None:
        out += ["## ACL-filtered vector study", "", *_vector_study(vector_study), ""]
    return "\n".join(out).rstrip() + "\n"
