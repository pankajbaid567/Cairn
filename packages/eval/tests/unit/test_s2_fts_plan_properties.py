"""Property test for S2 plan analysis on full-text search plans (free-tier retarget design
§5, Correctness Property 3; extends Phase 0 Property 17).

**Validates: Requirements 3.4, 3.5**

A random plan tree (random node types and nesting, unknown keys) that by construction holds
no FTS index scan and no ACL evidence gets, at random depths, any combination of a
``Bitmap Index Scan`` on ``chunks_fts_idx``, a ``Bitmap Index Scan`` on the ACL GIN index,
and a ``Filter`` or ``Recheck Cond`` that mentions ``acl``. ``analyze_plan`` must report
``uses_fts_index`` exactly when the FTS scan was inserted and ``acl_filter_location``
``index`` when the ACL index scan was inserted, ``filter`` when only the filter was, and
``none`` otherwise, without raising. The same insertions into the recorded plans in
``data/s2_plans`` (read only) add to what those plans already show. No database is needed.
"""

# Feature: cairn-free-tier-retarget, Property 3: Plan analysis finds the FTS index and classifies the ACL filter anywhere in the tree  # noqa: E501

from __future__ import annotations

import copy
import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from hypothesis import given, settings
from hypothesis import strategies as st

from cairn_eval.spikes.s2_rls_explain import IndexNames, PlanFacts, analyze_plan
from cairn_eval.spikes.s2_rls_explain.analyze import ACL_LOCATIONS, AclFilterLocation

PLANS = Path(__file__).parent / "data" / "s2_plans"
NAMES = IndexNames()
ACL_WORD = re.compile(r"\bacl\b")

# Keys the analyzer reads; unknown keys are drawn from everything else.
READ_KEYS = frozenset(
    {
        "Plan",
        "Plans",
        "Node Type",
        "Custom Plan Provider",
        "Relation Name",
        "Alias",
        "Index Name",
        "Filter",
        "Recheck Cond",
        "Rows Removed by Filter",
        "Actual Rows",
        "Actual Loops",
        "Execution Time",
    }
)

scalars = st.one_of(
    st.none(), st.booleans(), st.integers(), st.floats(allow_nan=True), st.text(max_size=20)
)
unknown_keys = st.one_of(
    st.sampled_from(["Plan Rows", "Parallel Aware", "Parent Relationship", "Index Searches"]),
    st.text(max_size=12),
).filter(lambda k: k not in READ_KEYS)

node_types = st.one_of(
    st.sampled_from(
        [
            "Limit",
            "Sort",
            "Hash Join",
            "Nested Loop",
            "Append",
            "BitmapAnd",
            "BitmapOr",
            "Bitmap Heap Scan",
            "Bitmap Index Scan",
            "Index Scan",
            "Index Only Scan",
            "Seq Scan",
            "CTE Scan",
            "Subquery Scan",
            "Result",
            "Custom Scan",
        ]
    ),
    st.text(max_size=20),
)
# Index names other than the FTS and ACL indexes, including near misses.
other_indexes = st.one_of(
    st.sampled_from([NAMES.hnsw, "chunks_pkey", "chunks_fts_idx_old", "chunks_acl_gin"]),
    st.text(max_size=20),
).filter(lambda n: n not in (NAMES.fts, NAMES.gin))
# Filter text that never mentions the ACL column as a word, including near misses.
no_acl_texts = st.one_of(
    st.sampled_from(["(body_tsv @@ q)", "(id > 5)", "acls", "body_acl", "(acl_count = 1)"]),
    st.text(max_size=30),
).filter(lambda t: ACL_WORD.search(t) is None)
# Filter text that mentions ``acl`` as a word, realistic or embedded in random text.
acl_texts = st.one_of(
    st.sampled_from(
        [
            "(acl && '{1001}'::integer[])",
            "((body_tsv @@ q) AND (acl && '{1005}'::integer[]))",
            "(chunks.acl && current_principals())",
        ]
    ),
    st.builds(
        lambda pre, left, right, post: f"{pre}{left}acl{right}{post}",
        st.text(max_size=10),
        st.sampled_from(["", " ", "(", ".", "&"]),
        st.sampled_from(["", " ", ")", "&", ":"]),
        st.text(max_size=10),
    ).filter(lambda t: ACL_WORD.search(t) is not None),
)


def _with_unknown_keys(node: st.SearchStrategy[dict[str, Any]]) -> st.SearchStrategy[Any]:
    extra = st.dictionaries(unknown_keys, scalars, max_size=3)
    return st.tuples(node, extra).map(lambda pair: {**pair[1], **pair[0]})


def _node(plans: st.SearchStrategy[Any] | None) -> st.SearchStrategy[Any]:
    """A plan node with no FTS scan and no ACL evidence; ``Index Cond`` may name anything."""
    optional: dict[str, st.SearchStrategy[Any]] = {
        "Index Name": other_indexes,
        "Index Cond": st.one_of(no_acl_texts, acl_texts),
        "Filter": no_acl_texts,
        "Recheck Cond": no_acl_texts,
        "Relation Name": st.sampled_from([NAMES.table, "other"]),
        "Actual Rows": st.integers(min_value=0, max_value=10_000),
        "Rows Removed by Filter": st.integers(min_value=0, max_value=10_000),
    }
    if plans is not None:
        optional["Plans"] = plans
    return _with_unknown_keys(st.fixed_dictionaries({"Node Type": node_types}, optional=optional))


background_trees = st.recursive(
    _node(None),
    lambda children: _node(st.lists(st.one_of(children, scalars), max_size=3)),
    max_leaves=12,
)

WRAPPERS: dict[str, Callable[[Any], Any]] = {
    "psycopg list": lambda root: [{"Plan": root, "Execution Time": 1.25}],
    "top object": lambda root: {"Plan": root},
    "bare node": lambda root: root,
}


def _nodes(root: dict[str, Any]) -> list[dict[str, Any]]:
    """Every node the analyzer visits: mappings reached through ``Plans`` lists."""
    found: list[dict[str, Any]] = []
    stack = [root]
    while stack:
        node = stack.pop()
        found.append(node)
        children = node.get("Plans")
        if isinstance(children, list):
            stack.extend(c for c in children if isinstance(c, dict))
    return found


def _index_scan(index: str, cond: str) -> dict[str, Any]:
    return {"Node Type": "Bitmap Index Scan", "Index Name": index, "Index Cond": cond}


def _insert_child(data: st.DataObject, root: dict[str, Any], child: dict[str, Any]) -> None:
    """Put ``child`` under a random node, at a random position, behind 0 to 30 pass-through
    nodes so it can sit far deeper than the generated tree."""
    for _ in range(data.draw(st.integers(min_value=0, max_value=30), label="extra depth")):
        child = {"Node Type": "Subquery Scan", "Plans": [child]}
    parent = data.draw(st.sampled_from(_nodes(root)), label="parent")
    plans = parent.get("Plans")
    if not isinstance(plans, list):
        plans = []
        parent["Plans"] = plans
    plans.insert(data.draw(st.integers(min_value=0, max_value=len(plans)), label="position"), child)


def _insert(
    data: st.DataObject, root: dict[str, Any], fts: bool, acl_index: bool, acl_filter: bool
) -> None:
    if fts:
        _insert_child(data, root, _index_scan(NAMES.fts, "(body_tsv @@ q)"))
    if acl_index:
        _insert_child(data, root, _index_scan(NAMES.gin, "(acl && '{1005}'::integer[])"))
    if acl_filter:
        key = data.draw(st.sampled_from(["Filter", "Recheck Cond"]), label="filter key")
        text = data.draw(acl_texts, label="filter text")
        if data.draw(st.booleans(), label="on an existing node"):
            data.draw(st.sampled_from(_nodes(root)), label="target")[key] = text
        else:
            node = data.draw(_node(None), label="filter node")
            node[key] = text
            _insert_child(data, root, node)


def _inserted(acl_index: bool, acl_filter: bool) -> set[AclFilterLocation]:
    found: set[AclFilterLocation] = set()
    if acl_index:
        found.add("index")
    if acl_filter:
        found.add("filter")
    return found


def _expected(evidence: set[AclFilterLocation]) -> tuple[AclFilterLocation, ...]:
    return tuple(loc for loc in ACL_LOCATIONS[:2] if loc in evidence)


def _check(facts: PlanFacts, plan: Any, fts: bool, evidence: tuple[AclFilterLocation, ...]) -> None:
    assert facts.uses_fts_index is fts
    assert facts.acl_filter_evidence == evidence
    assert facts.acl_filter_location == (evidence[0] if evidence else "none")
    assert facts.raw_plan is plan


@given(
    tree=background_trees,
    wrapper=st.sampled_from(sorted(WRAPPERS)),
    fts=st.booleans(),
    acl_index=st.booleans(),
    acl_filter=st.booleans(),
    data=st.data(),
)
@settings(deadline=None)
def test_fts_index_and_acl_location_are_found_anywhere_in_a_random_tree(
    tree: dict[str, Any],
    wrapper: str,
    fts: bool,
    acl_index: bool,
    acl_filter: bool,
    data: st.DataObject,
) -> None:
    root = copy.deepcopy(tree)
    _insert(data, root, fts, acl_index, acl_filter)
    plan = WRAPPERS[wrapper](root)

    facts = analyze_plan(plan, NAMES)

    _check(facts, plan, fts, _expected(_inserted(acl_index, acl_filter)))


@given(
    name=st.sampled_from(sorted(p.stem for p in PLANS.glob("*.json"))),
    fts=st.booleans(),
    acl_index=st.booleans(),
    acl_filter=st.booleans(),
    data=st.data(),
)
@settings(deadline=None)
def test_insertions_into_a_recorded_plan_add_to_its_findings(
    name: str, fts: bool, acl_index: bool, acl_filter: bool, data: st.DataObject
) -> None:
    text = (PLANS / f"{name}.json").read_text("utf-8")
    before = analyze_plan(json.loads(text), NAMES)
    plan = json.loads(text)
    _insert(data, plan[0]["Plan"], fts, acl_index, acl_filter)

    facts = analyze_plan(plan, NAMES)

    evidence = set(before.acl_filter_evidence) | _inserted(acl_index, acl_filter)
    _check(facts, plan, before.uses_fts_index or fts, _expected(evidence))
