"""S2: does the app role keep its indexes under FORCE row-level security? (Phase 0 design
§14; free-tier retarget design §5)

``run`` builds a spike database, measures native full-text search, HNSW and hybrid queries as
``cairn_app`` under ``EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)``, and ``write_report`` saves
the Markdown report and JSON results that feed ADR-0006. ``analyze_plan`` is the pure plan
reader. Synthetic data only (D20).
"""

from cairn_eval.spikes.s2_rls_explain.analyze import IndexNames, PlanFacts, analyze_plan
from cairn_eval.spikes.s2_rls_explain.config import S2Config
from cairn_eval.spikes.s2_rls_explain.harness import HarnessError, run
from cairn_eval.spikes.s2_rls_explain.report import (
    DefaultDenyResults,
    LexicalIndexOutcome,
    S2Report,
    render_markdown,
    summarize,
    write_report,
)

__all__ = [
    "DefaultDenyResults",
    "HarnessError",
    "IndexNames",
    "LexicalIndexOutcome",
    "PlanFacts",
    "S2Config",
    "S2Report",
    "analyze_plan",
    "render_markdown",
    "run",
    "summarize",
    "write_report",
]
