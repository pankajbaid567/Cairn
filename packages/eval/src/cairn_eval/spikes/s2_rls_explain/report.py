"""The S2 report: result types, the summary line, and the Markdown and JSON writers
(Phase 0 design §14 step 6; free-tier retarget design §5).

``<date>-report.md`` is for reading: environment, default-deny results, one table per
variant, and one raw plan per variant. ``<date>-results.json`` is for tools: the same
facts plus every raw plan. Both are written to ``out_dir`` (``docs/spikes/S2``).
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Final

from cairn_eval.spikes.s2_rls_explain.analyze import PlanFacts
from cairn_eval.spikes.s2_rls_explain.queries import VARIANTS, shape_of

RESULTS_SCHEMA: Final = "cairn-s2-results/2"
"""Version 2 (free-tier retarget): ``lexical_index`` replaces ``bm25_index``,
``uses_fts_index`` replaces ``uses_bm25_scan``, and the ACL locations are ``index``,
``filter`` and ``none``. The 27 Oct 2026 results file is version 1."""
ALL_INDEXES_SUMMARY: Final = "Index scans used under RLS for every variant."


@dataclass(frozen=True)
class LexicalIndexOutcome:
    """The full-text search index as built: its definition from ``pg_indexes``
    (``CREATE INDEX chunks_fts_idx ON s2.chunks USING gin (body_tsv)``)."""

    index_definition: str


@dataclass(frozen=True)
class DefaultDenyResults:
    """Requirement 16.5, observed as ``cairn_app`` unless noted."""

    no_setting_rows: int
    empty_principal_rows: int
    stale_setting_rows: int
    row_security_off_error: str | None
    owner_rows: int
    superuser_rows: int
    expected_rows: int

    @property
    def passed(self) -> bool:
        return (
            self.no_setting_rows == 0
            and self.empty_principal_rows == 0
            and self.stale_setting_rows == 0
            and self.row_security_off_error is not None
            and self.owner_rows == 0
            and self.superuser_rows == self.expected_rows
        )


@dataclass(frozen=True)
class S2Report:
    date: str
    config: Mapping[str, Any]
    environment: Mapping[str, Any]
    dataset: Mapping[str, Any]
    lexical: LexicalIndexOutcome
    default_deny: DefaultDenyResults
    facts: tuple[PlanFacts, ...]
    queries: Mapping[str, str]
    summary: str

    @property
    def passed(self) -> bool:
        """The default-deny checks held. Plan outcomes are findings, never failures."""
        return self.default_deny.passed

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready form with every raw plan."""
        results = []
        for f in self.facts:
            item = asdict(f)
            item["node_types"] = list(f.node_types)
            item["acl_filter_evidence"] = list(f.acl_filter_evidence)
            results.append(item)
        deny = asdict(self.default_deny)
        deny["passed"] = self.default_deny.passed
        return {
            "schema": RESULTS_SCHEMA,
            "date": self.date,
            "summary": self.summary,
            "config": dict(self.config),
            "environment": dict(self.environment),
            "dataset": dict(self.dataset),
            "lexical_index": asdict(self.lexical),
            "default_deny": deny,
            "queries": dict(self.queries),
            "results": results,
        }


def summarize(facts: Iterable[PlanFacts]) -> str:
    """The summary line (design §14 step 6): index scans everywhere under RLS, or which
    variants fell back to sequential scans, and where the ACL filter ran."""
    app = [f for f in facts if f.role == "cairn_app"]
    if not app:
        return "No plans were recorded under RLS."
    runs: Counter[str] = Counter()
    reasons: dict[str, Counter[str]] = {}
    locations: dict[str, Counter[str]] = {}
    for f in app:
        runs[f.variant] += 1
        shape = shape_of(f.variant)
        if shape in ("fts", "hybrid_rrf") and not f.uses_fts_index:
            reasons.setdefault(f.variant, Counter())["FTS index not used"] += 1
        if shape in ("hnsw", "hybrid_rrf") and not f.uses_hnsw_index:
            reasons.setdefault(f.variant, Counter())["HNSW index not used"] += 1
        if f.seq_scan_on_chunks:
            reasons.setdefault(f.variant, Counter())["sequential scan on the table"] += 1
        locations.setdefault(f.variant, Counter())[f.acl_filter_location] += 1
    order = [v for v in VARIANTS if v in runs] + sorted(set(runs) - set(VARIANTS))
    if not reasons:
        head = ALL_INDEXES_SUMMARY
    else:
        parts = []
        for variant in order:
            if variant in reasons:
                detail = ", ".join(
                    f"{why} in {n} of {runs[variant]} plans"
                    for why, n in sorted(reasons[variant].items())
                )
                parts.append(f"{variant} ({detail})")
        head = "Fell back under RLS: " + "; ".join(parts) + "."
    where = []
    for variant in order:
        counts = locations[variant]
        where.append(f"{variant}: " + ", ".join(f"{loc} x{n}" for loc, n in sorted(counts.items())))
    return head + " ACL filter location under RLS: " + "; ".join(where) + "."


# --- Markdown -------------------------------------------------------------------------


def _cell(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def _table(header: Sequence[str], rows: Iterable[Sequence[Any]]) -> list[str]:
    lines = ["| " + " | ".join(header) + " |", "|" + "|".join("---" for _ in header) + "|"]
    lines += ["| " + " | ".join(_cell(c) for c in row) + " |" for row in rows]
    return lines


def _yes(flag: bool) -> str:
    return "yes" if flag else "no"


def _truncate(text: str, limit: int = 600) -> str:
    return text if len(text) <= limit else text[:limit] + f" ... ({len(text)} characters)"


def render_markdown(report: S2Report) -> str:
    """The human-readable report."""
    env = report.environment
    deny = report.default_deny
    out: list[str] = [f"# S2 report: FORCE RLS and index use ({report.date})", ""]
    out += ["## Summary", "", report.summary, ""]
    out += [
        f"Default-deny checks: {'passed' if deny.passed else 'FAILED'}. "
        "Lexical search is native full-text search: the stored generated column `body_tsv` "
        "(`to_tsvector('simple', body)`) with a GIN index, `websearch_to_tsquery` and "
        "`ts_rank_cd`.",
        "",
        "This is the input to ADR-0006 in task 1.1: RLS-first queries if index scans hold "
        "under the policy, SECURITY DEFINER retrieval functions if they do not.",
        "",
    ]

    out += ["## Environment", ""]
    env_rows = [
        ("Image tag", env.get("image_tag")),
        ("Image digest", env.get("image_digest")),
        ("Image source", env.get("image_source")),
        ("PostgreSQL", env.get("server_version")),
        ("Server version string", env.get("version_string")),
    ]
    env_rows += [(f"Extension {n}", v) for n, v in sorted(env.get("extensions", {}).items())]
    env_rows += [(f"Setting {n}", v) for n, v in sorted(env.get("settings", {}).items())]
    env_rows += [(f"Setting {n}", v) for n, v in sorted(env.get("hnsw_settings", {}).items())]
    out += [*_table(["Item", "Value"], env_rows), ""]

    ds = report.dataset
    out += ["## Dataset", ""]
    out += _table(
        ["Item", "Value"],
        [
            ("Source", ds.get("source")),
            ("Rows", ds.get("rows")),
            ("Dimensions", ds.get("dim")),
            ("ACL mode", ds.get("acl_mode")),
            ("Generator version", ds.get("generator_version")),
            ("content_sha256", ds.get("content_sha256")),
        ],
    )
    out += [""]
    out += _table(
        ["Principal", "Groups", "Visible rows", "Actual selectivity", "Target"],
        [
            (
                name,
                info.get("groups"),
                info.get("visible_rows"),
                f"{info.get('actual_selectivity', 0.0):.4f}",
                info.get("target_selectivity"),
            )
            for name, info in ds.get("principals", {}).items()
        ],
    )
    out += [""]

    out += ["## Index build", ""]
    out += _table(
        ["Index", "Definition"],
        [("FTS", report.lexical.index_definition)]
        + [(n, d) for n, d in sorted(env.get("index_definitions", {}).items())],
    )
    out += [""]
    column = env.get("tsv_column")
    if column:
        out += [f"Generated column: `{column}`.", ""]
    sizes = env.get("relation_sizes", {})
    if sizes:
        out += [*_table(["Relation", "Size (bytes)"], sorted(sizes.items())), ""]

    out += ["## Default deny", ""]
    out += _table(
        ["Check", "Expected", "Observed", "Result"],
        [
            (
                "cairn_app, no principal setting",
                "0 rows",
                f"{deny.no_setting_rows} rows",
                _yes(deny.no_setting_rows == 0),
            ),
            (
                "cairn_app, empty principal `{}`",
                "0 rows",
                f"{deny.empty_principal_rows} rows",
                _yes(deny.empty_principal_rows == 0),
            ),
            (
                "cairn_app, setting left from an earlier transaction",
                "0 rows",
                f"{deny.stale_setting_rows} rows",
                _yes(deny.stale_setting_rows == 0),
            ),
            (
                "cairn_app, `SET row_security = off`",
                "error",
                deny.row_security_off_error or "no error",
                _yes(deny.row_security_off_error is not None),
            ),
            (
                "cairn_owner (FORCE, no owner policy)",
                "0 rows",
                f"{deny.owner_rows} rows",
                _yes(deny.owner_rows == 0),
            ),
            (
                "superuser (RLS bypassed)",
                f"{deny.expected_rows} rows",
                f"{deny.superuser_rows} rows",
                _yes(deny.superuser_rows == deny.expected_rows),
            ),
        ],
    )
    out += [""]

    out += ["## Results", ""]
    out += [
        f"Each row is the median-time run of {report.config.get('repeats')} repeat(s) of "
        "`EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)`. `cairn_app` rows run under FORCE RLS; "
        "`superuser_baseline` rows run as the superuser with no RLS. Recall is HNSW "
        "recall at 10 against exact search with the same visibility, averaged over "
        f"{report.config.get('recall_queries')} query vector(s); it is 1.000 whenever the "
        "planner reads the table instead of the HNSW index, because that path is exact. "
        "`n/a` principal means the "
        "query has no ACL predicate, so the baseline does not depend on the principal.",
        "",
    ]
    for variant in VARIANTS:
        rows = [f for f in report.facts if f.variant == variant]
        if not rows:
            continue
        out += [f"### {variant}", ""]
        if variant in report.queries:
            out += ["```sql", _truncate(report.queries[variant]), "```", ""]
        out += _table(
            [
                "Role",
                "Principal",
                "Iterative scan",
                "FTS index",
                "HNSW index",
                "Seq scan",
                "ACL filter",
                "Removed",
                "Returned",
                "Time (ms)",
                "Recall@10",
            ],
            [
                (
                    f.role,
                    f.principal,
                    f.iterative_scan,
                    _yes(f.uses_fts_index),
                    _yes(f.uses_hnsw_index),
                    _yes(f.seq_scan_on_chunks),
                    f.acl_filter_location,
                    f.rows_removed_by_filter,
                    f.rows_returned,
                    f"{f.execution_ms:.3f}",
                    "" if f.recall_at_10 is None else f"{f.recall_at_10:.3f}",
                )
                for f in rows
            ],
        )
        out += [""]

    out += [
        "## Raw plans",
        "",
        "Every raw plan is in the JSON results file. One per variant "
        "follows: the first principal and first iterative-scan setting under RLS.",
        "",
    ]
    for variant in VARIANTS:
        sample = next(
            (f for f in report.facts if f.variant == variant and f.role == "cairn_app"), None
        )
        if sample is None:
            continue
        out += [
            f"### {variant} ({sample.principal}, iterative_scan {sample.iterative_scan})",
            "",
            "```json",
            json.dumps(sample.raw_plan, separators=(",", ":"), sort_keys=True),
            "```",
            "",
        ]
    return "\n".join(out).rstrip("\n") + "\n"


def write_report(report: S2Report, out_dir: Path) -> tuple[Path, Path]:
    """Write ``<date>-report.md`` and ``<date>-results.json``; returns both paths.

    A second run on the same date replaces the files of the first.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    markdown = out_dir / f"{report.date}-report.md"
    results = out_dir / f"{report.date}-results.json"
    markdown.write_text(render_markdown(report), "utf-8")
    results.write_text(dump_results(report.to_dict()), "utf-8")
    return markdown, results


def dump_results(data: Mapping[str, Any]) -> str:
    """JSON text for the results file: the header indented for reading, one compact line
    per plan result (raw plans make the file large, and indenting them triples its size)."""
    header = {k: v for k, v in data.items() if k != "results"}
    text = json.dumps(header, indent=2, sort_keys=True)
    lines = ",\n".join(
        "    " + json.dumps(r, sort_keys=True, separators=(",", ":")) for r in data["results"]
    )
    return text[: text.rindex("}")].rstrip() + ',\n  "results": [\n' + lines + "\n  ]\n}\n"
