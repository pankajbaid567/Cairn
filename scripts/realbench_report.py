"""Turn var/realbench/results.json into docs/evidence/real-bench/ (tables, CIs, failure list).

    uv run python scripts/realbench_report.py
"""

from __future__ import annotations

import csv
import json
import random
import sys
from collections import Counter
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))
from realbench_run import VARIANTS, boot_ci, paired_diff, rank_metrics  # noqa: E402

OUT = Path("var/realbench")
DOCS = Path("docs/evidence/real-bench")


def main() -> int:
    res = json.loads((OUT / "results.json").read_text())
    qs = {q["id"]: q for q in json.loads((OUT / "questions.json").read_text())}
    DOCS.mkdir(parents=True, exist_ok=True)
    L: list[str] = []

    def table(split: str | None) -> dict[str, dict[str, list[float]]]:
        per: dict[str, dict[str, list[float]]] = {}
        for v in VARIANTS:
            m: dict[str, list[float]] = {k: [] for k in ("hit5", "hit10", "mrr10", "ndcg10")}
            for r in res["rows"][v["name"]]:
                q = qs[r["id"]]
                if q["expected"] != "answer" or (split and q["split"] != split):
                    continue
                for k, x in rank_metrics(r["ranked"], q["gold_peps"]).items():
                    m[k].append(x)
            per[v["name"]] = m
        return per

    L += ["# Real-corpus benchmark: 150 Python Enhancement Proposals", "",
          f"Run on {res['date']}. {res['n_peps']} PEPs ({res['chunks']} chunks), {res['n_questions']} questions, "
          "Titan v2 and Cohere Embed v3 (1,024 dimensions), `gpt-oss-20b` LLM reranker, `gpt-oss-120b` answers, "
          "`qwen3-32b` judge, all on Amazon Bedrock in ap-south-1; local PostgreSQL 18 with pgvector.", "",
          "What is real: the PEP text, titles, authors, dates and statuses. What is synthetic: the three people who "
          "ask and the access rules (about 1 PEP in 11 is steering-only, 1 in 6 core-only, chosen by a hash of the "
          "PEP number). The questions are AI-drafted (content questions have a quote that was checked to appear "
          "verbatim); header and unanswerable questions are made by code. Dev and test are a 50/50 split by question "
          "id; choices were made on dev and the test split is reported separately.", "",
          "## Retrieval ablation (answerable questions, document level)", "",
          "A question counts as a hit when its source PEP is among the top 5 (or 10) documents. 95% intervals are "
          "bootstrap over questions (2,000 resamples).", ""]
    for split in ("dev", "test", None):
        per = table(split)
        n = len(per["hybrid_titan"]["hit5"])
        L += [f"### {'All' if split is None else split.capitalize()} ({n} questions)", "",
              "| Variant | Hit@5 | Hit@10 | MRR@10 | nDCG@10 |", "|---|---|---|---|---|"]
        for v in VARIANTS:
            cells = []
            for k in ("hit5", "hit10", "mrr10", "ndcg10"):
                m, lo, hi = boot_ci(per[v["name"]][k])
                cells.append(f"{m:.3f} ({lo:.3f}–{hi:.3f})")
            L.append(f"| {v['name']} | " + " | ".join(cells) + " |")
        L.append("")
        if split is None:
            L += ["### Paired differences, all answerable questions (positive = first is better)", "",
                  "| Comparison | Δ nDCG@10 | Δ Hit@5 |", "|---|---|---|"]
            for a, b, label in [
                ("hybrid_titan", "dense_titan", "hybrid vs dense (Titan)"),
                ("hybrid_titan", "lexical", "hybrid vs lexical-only"),
                ("hybrid_titan_rerank", "hybrid_titan", "LLM rerank on vs off (Titan)"),
                ("hybrid_cohere_rerank", "hybrid_cohere", "LLM rerank on vs off (Cohere)"),
                ("hybrid_cohere", "hybrid_titan", "Cohere v3 vs Titan v2 (hybrid)"),
                ("hybrid_cohere_rerank", "hybrid_titan_rerank", "Cohere v3 vs Titan v2 (hybrid + rerank)"),
            ]:
                d1 = paired_diff(per[a]["ndcg10"], per[b]["ndcg10"])
                d2 = paired_diff(per[a]["hit5"], per[b]["hit5"])
                L.append(f"| {label} | {d1[0]:+.3f} ({d1[1]:+.3f} to {d1[2]:+.3f}) | {d2[0]:+.3f} ({d2[1]:+.3f} to {d2[2]:+.3f}) |")
            L.append("")
    lat = {v["name"]: sorted(r["ms"] for r in res["rows"][v["name"]]) for v in VARIANTS}
    L += ["### Latency per search (ms, 4 searches in parallel, so these include some queueing)", "",
          "| Variant | p50 | p95 | searches where a stage degraded |", "|---|---|---|---|"]
    for v in VARIANTS:
        x = lat[v["name"]]
        deg = sum(1 for r in res["rows"][v["name"]] if r["degraded"])
        L.append(f"| {v['name']} | {x[len(x) // 2]:.0f} | {x[int(len(x) * 0.95)]:.0f} | {deg} of {len(x)} |")
    leaks = {v["name"]: sum(r["leak"] for r in res["rows"][v["name"]]) for v in VARIANTS}
    L += ["", f"Permission leaks (a restricted PEP in the results of someone who may not read it): {sum(leaks.values())} across all variants "
          f"and {sum(1 for q in qs.values() if q['must_not_cite'])} restricted questions each ({leaks}).", ""]

    # answers
    ans = {a["id"]: a for a in res["answers"]}
    exp_ans = [q for q in qs.values() if q["expected"] == "answer"]
    exp_abs = [q for q in qs.values() if q["expected"] == "abstain"]
    errors = [a for a in ans.values() if "error" in a]
    answered = lambda q: (not ans[q["id"]].get("abstained", True)) and "error" not in ans[q["id"]]  # noqa: E731
    tp = sum(answered(q) for q in exp_ans)
    abs_ok = sum(not answered(q) for q in exp_abs)
    cited_gold = [q for q in exp_ans if answered(q) and set(ans[q["id"]]["cited"]) & set(q["gold_peps"])]
    cited_any = [q for q in exp_ans if answered(q) and ans[q["id"]]["cited"]]
    leak_cite = sum(1 for q in qs.values() if q["must_not_cite"] and answered(q) and set(ans[q["id"]]["cited"]) & set(q["must_not_cite"]))
    judged = [(q, ans[q["id"]]["judge"]) for q in exp_ans if "judge" in ans[q["id"]]]
    yes = sum(j["correct"] == "yes" for _, j in judged)
    part = sum(j["correct"] == "partial" for _, j in judged)
    grounded = sum(j["grounded"] == "yes" for _, j in judged)
    L += [f"## Answers (embedder {res['answer_embedder']}, rerank score floor {res['min_rerank_score']})", "",
          "| Measure | Result |", "|---|---|",
          f"| Questions that should be answered: answered | {tp} of {len(exp_ans)} ({tp / len(exp_ans):.0%}) |",
          f"| Questions that should be refused: refused | {abs_ok} of {len(exp_abs)} ({abs_ok / len(exp_abs):.0%}) |",
          f"| Answered questions that cite the gold PEP | {len(cited_gold)} of {tp} ({len(cited_gold) / max(tp, 1):.0%}) |",
          f"| Answered questions that cite anything | {len(cited_any)} of {tp} |",
          f"| Restricted PEP cited to someone who may not read it | {leak_cite} |",
          f"| AI judge: correct / partial (of answered and judged {len(judged)}) | {yes} ({yes / max(len(judged), 1):.0%}) / {part} |",
          f"| AI judge: every claim supported by the cited quotes | {grounded} of {len(judged)} ({grounded / max(len(judged), 1):.0%}) |",
          f"| Calls that errored | {len(errors)} |", ""]
    by_kind: dict[str, list[int]] = {}
    for q in qs.values():
        ok = answered(q) if q["expected"] == "answer" else not answered(q)
        by_kind.setdefault(f"{q['kind']} ({q['expected']})", []).append(int(ok))
    L += ["| Kind (expected) | Right decision (answer vs refuse) |", "|---|---|"]
    for k, v in sorted(by_kind.items()):
        L.append(f"| {k} | {sum(v)} of {len(v)} |")
    L.append("")
    # failures for analysis
    fails = [q for q in exp_ans if not answered(q)][:12]
    L += ["### Examples of questions wrongly refused", ""]
    for q in fails:
        a = ans[q["id"]]
        L.append(f"- ({q['kind']}) {q['text']} — gold PEP {q['gold_peps']}, retrieved {a.get('retrieved', [])[:5]}, packed {a.get('packed', [])[:5]}")
    wrong = [q for q in exp_abs if answered(q)][:8]
    L += ["", "### Examples of questions that should have been refused but were answered", ""]
    for q in wrong:
        a = ans[q["id"]]
        L.append(f"- ({q['kind']}, asked by {q['asker']}) {q['text']} — cited {a['cited']}")
    L.append("")
    (DOCS / "README.md").write_text("\n".join(L) + "\n")
    # review sheet: 60 judged items, stratified, for a person to confirm
    rng = random.Random(3)
    pick = rng.sample(judged, min(60, len(judged)))
    with (DOCS / "judge_review_sheet.csv").open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["id", "question", "reference_answer", "system_answer", "cited_quote_1", "ai_correct", "ai_grounded", "human_correct", "human_grounded"])
        for q, j in pick:
            a = ans[q["id"]]
            w.writerow([q["id"], q["text"], q["gold_answer"], a["answer"], (a["quotes"] or [""])[0][:500], j["correct"], j["grounded"], "", ""])
    slim = {"date": res["date"], "chunks": res["chunks"], "answer_embedder": res["answer_embedder"], "rows": res["rows"],
            "answers": [{k: v for k, v in a.items() if k not in ("answer", "quotes")} for a in res["answers"]]}
    (DOCS / "results.json").write_text(json.dumps(slim) + "\n")
    shutil_copy = json.loads((OUT / "questions.json").read_text())
    (DOCS / "questions.json").write_text(json.dumps(shutil_copy, indent=1) + "\n")
    print("wrote", DOCS)
    print(Counter(k for k in by_kind))
    return 0


if __name__ == "__main__":
    sys.exit(main())
