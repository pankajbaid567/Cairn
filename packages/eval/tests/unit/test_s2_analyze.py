"""S2 unit tests: ``analyze_plan`` on recorded plans, the config, the summary and the report
writer (free-tier retarget Requirement 3; no database needed). The SQL builders are tested in
``test_s2_queries.py``.

The plans in ``data/s2_plans`` were recorded on 2 Nov 2026 (task 3.1) from the S2 harness on
a 2,000-row, 64-dimension S1 dataset, on ``pgvector/pgvector:0.8.6-pg18`` (PostgreSQL 18.6,
pgvector 0.8.6), principal p05 and ``hnsw.iterative_scan = off`` unless the name says
otherwise. ``*_acl_index`` plans ran with the ACL GIN index, ``*_no_acl_index`` plans after it
was dropped. ``*_rls_*`` plans ran as ``cairn_app`` under FORCE RLS, ``*_superuser`` plans as
the superuser baseline. Under RLS the planner never puts ``body_tsv @@ q`` into an index
condition: ``ts_match_vq`` is not leakproof, so it runs as a filter after the policy.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from cairn_eval.spikes.s2_rls_explain import (
    DefaultDenyResults,
    IndexNames,
    LexicalIndexOutcome,
    PlanFacts,
    S2Config,
    S2Report,
    analyze_plan,
    render_markdown,
    summarize,
    write_report,
)

PLANS = Path(__file__).parent / "data" / "s2_plans"
NAMES = IndexNames(
    table="chunks",
    fts="chunks_fts_idx",
    hnsw="chunks_embedding_hnsw_idx",
    gin="chunks_acl_gin_idx",
)


def recorded(name: str) -> Any:
    return json.loads((PLANS / f"{name}.json").read_text("utf-8"))


# --- analyze_plan on recorded plans ----------------------------------------------------


@pytest.mark.parametrize(
    ("name", "removed", "returned"),
    [
        ("fts_rls_acl_index", 111, 2),
        ("fts_explicit_rls_acl_index", 111, 2),
        ("hybrid_rls_acl_index", 111, 10),
        ("hybrid_explicit_rls_acl_index", 111, 10),
    ],
)
def test_lexical_under_rls_with_the_acl_index_filters_after_an_acl_bitmap(
    name: str, removed: int, returned: int
) -> None:
    facts = analyze_plan(recorded(name), NAMES)
    assert not facts.uses_fts_index
    assert not facts.uses_hnsw_index
    assert not facts.seq_scan_on_chunks
    assert "Bitmap Index Scan" in facts.node_types
    assert facts.acl_filter_location == "index"
    assert facts.acl_filter_evidence == ("index", "filter")
    assert facts.rows_removed_by_filter == removed
    assert facts.rows_returned == returned
    assert facts.execution_ms > 0
    assert facts.raw_plan == recorded(name)


@pytest.mark.parametrize(
    ("name", "removed"),
    [
        ("fts_rls_no_acl_index", 1998),
        ("fts_explicit_rls_no_acl_index", 1998),
        ("hybrid_rls_no_acl_index", 3885),
        ("hybrid_explicit_rls_no_acl_index", 3885),
    ],
)
def test_lexical_under_rls_without_the_acl_index_is_a_filtered_sequential_scan(
    name: str, removed: int
) -> None:
    facts = analyze_plan(recorded(name), NAMES)
    assert facts.seq_scan_on_chunks
    assert not facts.uses_fts_index
    assert facts.acl_filter_location == "filter"
    assert facts.acl_filter_evidence == ("filter",)
    assert facts.rows_removed_by_filter == removed


def test_superuser_with_the_explicit_predicate_uses_the_fts_index_in_a_bitmap_and() -> None:
    facts = analyze_plan(recorded("fts_explicit_superuser"), NAMES, role="superuser_baseline")
    assert facts.role == "superuser_baseline"
    assert facts.uses_fts_index
    assert "BitmapAnd" in facts.node_types
    assert facts.acl_filter_location == "index"
    assert facts.rows_returned == 2


def test_superuser_hybrid_with_the_explicit_predicate_uses_both_indexes() -> None:
    facts = analyze_plan(recorded("hybrid_explicit_superuser"), NAMES)
    assert facts.uses_fts_index
    assert facts.uses_hnsw_index
    assert facts.acl_filter_location == "index"
    assert facts.rows_removed_by_filter == 11


def test_superuser_fts_without_a_predicate_has_no_acl_filter() -> None:
    facts = analyze_plan(recorded("fts_superuser"), NAMES)
    assert facts.seq_scan_on_chunks
    assert facts.acl_filter_location == "none"
    assert facts.acl_filter_evidence == ()
    assert facts.rows_removed_by_filter == 1969


def test_hnsw_under_rls_with_a_filter_on_the_index_scan_is_a_filter() -> None:
    facts = analyze_plan(recorded("hnsw_rls_post_filter"), NAMES)
    assert facts.uses_hnsw_index
    assert not facts.uses_fts_index
    assert facts.acl_filter_location == "filter"
    assert facts.rows_removed_by_filter == 24
    assert facts.rows_returned == 10


def test_planner_that_picks_the_acl_bitmap_does_not_use_hnsw() -> None:
    facts = analyze_plan(recorded("hnsw_rls_gin_instead"), NAMES)
    assert not facts.uses_hnsw_index
    assert not facts.seq_scan_on_chunks
    assert facts.acl_filter_location == "index"


def test_superuser_hnsw_baseline_has_no_acl_filter() -> None:
    facts = analyze_plan(recorded("hnsw_superuser"), NAMES, role="superuser_baseline")
    assert facts.uses_hnsw_index
    assert facts.acl_filter_location == "none"
    assert facts.rows_removed_by_filter == 0


def test_labels_and_recall_are_carried_through() -> None:
    facts = analyze_plan(
        recorded("hnsw_superuser"),
        NAMES,
        variant="hnsw",
        principal="p05",
        role="cairn_app",
        iterative_scan="strict_order",
        recall_at_10=0.8,
    )
    assert (facts.variant, facts.principal, facts.iterative_scan) == ("hnsw", "p05", "strict_order")
    assert facts.recall_at_10 == 0.8


def test_accepts_the_bare_object_and_the_bare_node() -> None:
    wrapped = recorded("fts_rls_no_acl_index")
    assert analyze_plan(wrapped[0], NAMES).seq_scan_on_chunks
    assert analyze_plan(wrapped[0]["Plan"], NAMES).seq_scan_on_chunks


# --- hand-made plan shapes -------------------------------------------------------------


@pytest.mark.parametrize(
    "plan",
    [
        None,
        [],
        {},
        "not a plan",
        42,
        [{"Plan": "oops"}],
        [{"Plan": {"Node Type": "Result"}}],
        [{"Plan": {"Node Type": ["x"], "Plans": "no", "Actual Rows": "many"}}],
        [{"Plan": {"Node Type": "Seq Scan", "Relation Name": "chunks", "Filter": "aclx"}}],
    ],
)
def test_plans_without_acl_evidence_report_none_and_keep_the_plan(plan: Any) -> None:
    facts = analyze_plan(plan, NAMES)
    assert facts.acl_filter_location == "none"
    assert not facts.uses_fts_index
    assert not facts.uses_hnsw_index
    assert facts.raw_plan == plan


def test_acl_index_scan_deep_in_the_tree_is_index_and_wins_over_a_filter() -> None:
    gin = {"Node Type": "Bitmap Index Scan", "Index Name": "chunks_acl_gin_idx"}
    fts = {"Node Type": "Bitmap Index Scan", "Index Name": "chunks_fts_idx"}
    heap = {
        "Node Type": "Bitmap Heap Scan",
        "Relation Name": "chunks",
        "Recheck Cond": "(acl && '{1}'::integer[])",
        "Plans": [{"Node Type": "BitmapAnd", "Plans": [fts, gin]}],
    }
    plan = [{"Plan": {"Node Type": "Limit", "Plans": [{"Node Type": "Sort", "Plans": [heap]}]}}]
    facts = analyze_plan(plan, NAMES)
    assert facts.uses_fts_index
    assert facts.acl_filter_location == "index"
    assert facts.acl_filter_evidence == ("index", "filter")


def test_recheck_cond_on_acl_alone_is_a_filter() -> None:
    node = {"Node Type": "Bitmap Heap Scan", "Recheck Cond": "(acl && x)"}
    assert analyze_plan(node, NAMES).acl_filter_location == "filter"


def test_an_index_name_on_a_node_that_is_not_an_index_scan_does_not_count() -> None:
    node = {"Node Type": "Result", "Index Name": "chunks_fts_idx"}
    assert not analyze_plan(node, NAMES).uses_fts_index


# --- config ----------------------------------------------------------------------------


def test_config_defaults_match_the_design() -> None:
    cfg = S2Config()
    assert cfg.database == "cairn_spike_s2"
    assert cfg.principals == ("p01", "p05", "p20", "p50")
    assert cfg.iterative_scan == ("off", "relaxed_order", "strict_order")
    assert (cfg.ef_search, cfg.repeats, cfg.rows) == (40, 3, 20_000)
    assert cfg.out_dir == Path("docs/spikes/S2")


@pytest.mark.parametrize(
    "kwargs",
    [
        {"database": "cairn"},
        {"database": "cairn_test_abcd"},
        {"database": "cairn_spike_x; DROP"},
        {"principals": ()},
        {"principals": ("p01", "p01")},
        {"iterative_scan": ("sometimes",)},
        {"repeats": 0},
        {"ef_search": 0},
        {"maintenance_work_mem": "lots"},
    ],
)
def test_config_rejects_unusable_values(kwargs: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match="invalid S2 configuration"):
        S2Config(**kwargs)


# --- summary and report writer ---------------------------------------------------------


def make_facts(variant: str, **overrides: Any) -> PlanFacts:
    base: dict[str, Any] = {
        "variant": variant,
        "principal": "p01",
        "role": "cairn_app",
        "iterative_scan": "off",
        "node_types": ("Limit",),
        "uses_fts_index": True,
        "uses_hnsw_index": True,
        "seq_scan_on_chunks": False,
        "acl_filter_location": "index",
        "rows_returned": 10,
        "rows_removed_by_filter": 0,
        "execution_ms": 1.5,
        "raw_plan": [{"Plan": {"Node Type": "Limit"}}],
    }
    base.update(overrides)
    return PlanFacts(**base)


def test_summary_says_index_scans_hold_when_every_app_plan_uses_its_index() -> None:
    facts = [make_facts("fts"), make_facts("hnsw"), make_facts("hybrid_rrf")]
    assert summarize(facts).startswith("Index scans used under RLS for every variant.")


def test_summary_names_the_variants_that_fell_back_and_ignores_the_baseline() -> None:
    facts = [
        make_facts("hnsw", uses_hnsw_index=False, seq_scan_on_chunks=True),
        make_facts("fts", uses_fts_index=False, acl_filter_location="filter"),
        make_facts("hnsw_explicit_acl", role="superuser_baseline", uses_hnsw_index=False),
    ]
    text = summarize(facts)
    assert text.startswith("Fell back under RLS: fts (FTS index not used in 1 of 1 plans); hnsw (")
    assert "HNSW index not used in 1 of 1 plans" in text
    assert "sequential scan on the table in 1 of 1 plans" in text
    assert "hnsw_explicit_acl" not in text.split("ACL filter location")[0]
    assert "fts: filter x1" in text


def make_report(facts: list[PlanFacts], **overrides: Any) -> S2Report:
    base: dict[str, Any] = {
        "date": "2026-11-02",
        "config": {"repeats": 1, "recall_queries": 1},
        "environment": {
            "image_tag": "pgvector/pgvector:0.8.6-pg18",
            "image_digest": "sha256:abc",
            "server_version": "18.6",
            "extensions": {"btree_gist": "1.8", "vector": "0.8.6"},
            "settings": {"work_mem": "4096kB"},
            "index_definitions": {},
            "tsv_column": "body_tsv tsvector GENERATED ALWAYS AS (x) STORED",
            "relation_sizes": {},
        },
        "dataset": {"source": "test", "rows": 10, "dim": 2, "principals": {}},
        "lexical": LexicalIndexOutcome("CREATE INDEX chunks_fts_idx ON s2.chunks USING gin"),
        "default_deny": DefaultDenyResults(0, 0, 0, "InsufficientPrivilege (42501): no", 0, 10, 10),
        "facts": tuple(facts),
        "queries": {"fts": "SELECT 1"},
        "summary": summarize(facts),
    }
    base.update(overrides)
    return S2Report(**base)


def test_report_files_hold_the_environment_default_deny_and_raw_plans(tmp_path: Path) -> None:
    report = make_report([make_facts("fts"), make_facts("hnsw")])
    markdown, results = write_report(report, tmp_path / "out")
    assert markdown.name == "2026-11-02-report.md"
    assert results.name == "2026-11-02-results.json"
    text = markdown.read_text("utf-8")
    for expected in (
        "pgvector/pgvector:0.8.6-pg18",
        "sha256:abc",
        "Extension vector",
        "websearch_to_tsquery",
        "| FTS | CREATE INDEX chunks_fts_idx ON s2.chunks USING gin |",
        "Generated column: `body_tsv tsvector GENERATED ALWAYS AS (x) STORED`.",
        "## Default deny",
        "### fts",
        "### hnsw",
        "FTS index",
    ):
        assert expected in text
    data = json.loads(results.read_text("utf-8"))
    assert data["schema"] == "cairn-s2-results/2"
    assert data["default_deny"]["passed"] is True
    assert data["lexical_index"] == {
        "index_definition": "CREATE INDEX chunks_fts_idx ON s2.chunks USING gin"
    }
    assert len(data["results"]) == 2
    assert data["results"][0]["uses_fts_index"] is True
    assert data["results"][0]["raw_plan"] == [{"Plan": {"Node Type": "Limit"}}]
    assert data["environment"]["image_digest"] == "sha256:abc"


def test_default_deny_fails_when_any_check_does_not_hold() -> None:
    good = DefaultDenyResults(0, 0, 0, "error", 0, 10, 10)
    assert good.passed
    for bad in (
        DefaultDenyResults(1, 0, 0, "error", 0, 10, 10),
        DefaultDenyResults(0, 0, 0, None, 0, 10, 10),
        DefaultDenyResults(0, 0, 0, "error", 3, 10, 10),
        DefaultDenyResults(0, 0, 0, "error", 0, 9, 10),
    ):
        assert not bad.passed
        assert (
            "FAILED"
            in render_markdown(make_report([make_facts("fts")], default_deny=bad))
            .split("Default-deny checks: ", 1)[1]
            .split(".")[0]
        )
