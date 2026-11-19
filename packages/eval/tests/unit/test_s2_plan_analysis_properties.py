"""Property test for S2 plan analysis robustness (design §14, Requirement 16.8).

**Validates: Requirements 16.8**

``analyze_plan`` must take any JSON-shaped value, however deeply nested or oddly keyed,
and return ``PlanFacts`` whose fields hold allowed values without raising, and keep the raw
plan. Since the free-tier retarget (task 3.1, Requirement 3.5) the ACL filter location is
``index``, ``filter`` or ``none``: a plan without ACL evidence reports ``none``. No database
is needed. The retarget's Property 3 (FTS index and ACL location anywhere in the tree) is
task 3.3.
"""

# Feature: cairn-phase-0-foundations, Property 17: Plan analysis tolerates unknown plan shapes

from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Any

from hypothesis import example, given, settings
from hypothesis import strategies as st

from cairn_eval.spikes.s2_rls_explain import IndexNames, PlanFacts, analyze_plan
from cairn_eval.spikes.s2_rls_explain.analyze import ACL_LOCATIONS, INDEX_SCAN_TYPES

PLANS = Path(__file__).parent / "data" / "s2_plans"
NAMES = IndexNames()

# Keys the analyzer reads, so random trees hit its code paths and not only the defaults.
KNOWN_KEYS = (
    "Plan",
    "Plans",
    "Node Type",
    "Custom Plan Provider",
    "Relation Name",
    "Alias",
    "Index Name",
    "Index Cond",
    "Filter",
    "Recheck Cond",
    "Rows Removed by Filter",
    "Actual Rows",
    "Actual Loops",
    "Execution Time",
)
KNOWN_VALUES = (
    *sorted(INDEX_SCAN_TYPES),
    "Seq Scan",
    "Bitmap Heap Scan",
    "BitmapAnd",
    "Limit",
    NAMES.table,
    NAMES.fts,
    NAMES.hnsw,
    NAMES.gin,
    NAMES.acl_column,
    "(acl && '{1}'::integer[])",
)

keys = st.one_of(st.sampled_from(KNOWN_KEYS), st.text(max_size=12))
texts = st.one_of(st.sampled_from(KNOWN_VALUES), st.text(max_size=30))
# Integers beyond the float range are valid JSON numbers; floats include nan and infinity.
numbers = st.one_of(
    st.integers(),
    st.integers(min_value=-(10**400), max_value=10**400),
    st.floats(allow_nan=True, allow_infinity=True),
)
scalars = st.one_of(st.none(), st.booleans(), numbers, texts)


def _containers(children: st.SearchStrategy[Any]) -> st.SearchStrategy[Any]:
    """Lists and objects whose values may also be a JSON document as a string."""
    value = st.one_of(children, children.map(lambda c: json.dumps(c, default=str)))
    return st.one_of(
        st.lists(value, max_size=4),
        st.dictionaries(keys, value, max_size=6),
    )


json_trees = st.recursive(scalars, _containers, max_leaves=40)


def _is_allowed(facts: PlanFacts, plan: Any) -> None:
    assert isinstance(facts, PlanFacts)
    assert facts.acl_filter_location in ACL_LOCATIONS
    assert isinstance(facts.node_types, tuple)
    assert all(isinstance(t, str) and t for t in facts.node_types)
    for flag in (facts.uses_fts_index, facts.uses_hnsw_index, facts.seq_scan_on_chunks):
        assert isinstance(flag, bool)
    assert type(facts.rows_returned) is int
    assert type(facts.rows_removed_by_filter) is int
    assert type(facts.execution_ms) is float
    assert math.isfinite(facts.execution_ms)
    # Evidence is a subset of the two evidence locations, without repeats, in order.
    evidence = facts.acl_filter_evidence
    assert len(set(evidence)) == len(evidence)
    assert list(evidence) == [loc for loc in ACL_LOCATIONS[:2] if loc in evidence]
    # A reported location is none exactly when there is no evidence, else the first of it.
    if evidence:
        assert facts.acl_filter_location == evidence[0]
    else:
        assert facts.acl_filter_location == "none"
    # The raw plan is kept as given (Requirement 16.8).
    assert facts.raw_plan is plan


@given(plan=json_trees)
# A number outside the float range where the analyzer reads a float.
@example(plan=[{"Plan": {"Node Type": "Limit"}, "Execution Time": 10**400}])
@example(plan=[{"Plan": {"Node Type": "Limit", "Actual Rows": 10**400}}])
@example(
    plan=[
        {
            "Plan": {
                "Node Type": "Seq Scan",
                "Relation Name": "chunks",
                "Actual Loops": 10**400,
                "Rows Removed by Filter": 3,
            }
        }
    ]
)
@settings(deadline=None)
def test_any_json_tree_yields_allowed_facts(plan: Any) -> None:
    facts = analyze_plan(plan, NAMES, variant="v", principal="p", role="cairn_app")
    _is_allowed(facts, plan)


# Text that never mentions the ACL column as a word.
no_acl_texts = st.one_of(
    st.sampled_from([v for v in KNOWN_VALUES if "acl" not in v]), st.text(max_size=30)
).filter(lambda t: re.search(r"\bacl\b", t) is None)


def _plan_shaped(children: st.SearchStrategy[Any]) -> st.SearchStrategy[Any]:
    """Plan nodes that are never an index scan or sequential scan and never mention the ACL
    column in a filter, with random extra keys."""
    not_a_scan = st.text(max_size=20).filter(
        lambda t: t not in INDEX_SCAN_TYPES and t not in ("Seq Scan", "Sample Scan")
    )
    node = st.fixed_dictionaries(
        {"Node Type": not_a_scan},
        optional={
            "Plans": st.lists(st.one_of(children, scalars), max_size=3),
            "Filter": no_acl_texts,
            "Recheck Cond": no_acl_texts,
            "Index Name": texts,
            "Index Cond": texts,
            "Actual Rows": numbers,
            "Rows Removed by Filter": numbers,
            "Actual Loops": numbers,
        },
    )
    return node


unrecognized_nodes = st.recursive(
    st.fixed_dictionaries({"Node Type": st.just("Result")}), _plan_shaped, max_leaves=15
)
unrecognized_plans = st.one_of(
    unrecognized_nodes,
    unrecognized_nodes.map(lambda n: [{"Plan": n, "Execution Time": 1.5}]),
    unrecognized_nodes.map(lambda n: {"Plan": n}),
)


@given(plan=unrecognized_plans)
@settings(deadline=None)
def test_plan_without_scans_or_acl_filters_is_none_and_keeps_the_raw_plan(plan: Any) -> None:
    facts = analyze_plan(plan, NAMES)
    _is_allowed(facts, plan)
    assert facts.acl_filter_location == "none"
    assert not facts.uses_fts_index
    assert not facts.uses_hnsw_index
    assert not facts.seq_scan_on_chunks


@given(plan=json_trees)
@settings(deadline=None)
def test_analysis_is_deterministic(plan: Any) -> None:
    assert analyze_plan(plan, NAMES) == analyze_plan(plan, NAMES)


# Junk added to a recorded plan: nodes of an unrecognized type, and children that are not
# nodes at all. None of it names a scan, an index or the ACL column.
junk_children = st.lists(
    st.one_of(
        scalars.filter(lambda v: not isinstance(v, str) or v not in KNOWN_VALUES),
        st.fixed_dictionaries(
            {"Node Type": st.just("Junk")},
            optional={"x-extra": st.lists(scalars, max_size=3), "x-n": st.integers()},
        ),
    ),
    min_size=1,
    max_size=4,
)


@given(
    name=st.sampled_from(sorted(p.stem for p in PLANS.glob("*.json"))),
    junk=junk_children,
)
@settings(deadline=None)
def test_junk_around_a_recorded_plan_leaves_the_findings_unchanged(
    name: str, junk: list[Any]
) -> None:
    original = json.loads((PLANS / f"{name}.json").read_text("utf-8"))
    noisy = json.loads((PLANS / f"{name}.json").read_text("utf-8"))
    root = noisy[0]["Plan"]
    root["Plans"] = [*root.get("Plans", []), *junk]
    noisy[0]["x-unknown-key"] = {"deep": [junk]}

    before = analyze_plan(original, NAMES)
    after = analyze_plan(noisy, NAMES)
    _is_allowed(after, noisy)
    assert after.acl_filter_location == before.acl_filter_location
    assert after.acl_filter_evidence == before.acl_filter_evidence
    assert after.uses_fts_index == before.uses_fts_index
    assert after.uses_hnsw_index == before.uses_hnsw_index
    assert after.seq_scan_on_chunks == before.seq_scan_on_chunks
    assert after.rows_removed_by_filter == before.rows_removed_by_filter
    assert after.rows_returned == before.rows_returned


def test_very_deep_plan_does_not_raise() -> None:
    node: dict[str, Any] = {"Node Type": "Result", "Filter": "(acl && x)"}
    for _ in range(5000):
        node = {"Node Type": "Result", "Plans": [node]}
    deep_plan = [{"Plan": node}]
    facts = analyze_plan(deep_plan, NAMES)
    _is_allowed(facts, deep_plan)
    assert facts.acl_filter_location == "filter"
