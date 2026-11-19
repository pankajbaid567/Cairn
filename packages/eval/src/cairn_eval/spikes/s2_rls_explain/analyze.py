"""Plan analysis for the S2 harness: ``PlanFacts`` and the pure ``analyze_plan``
(Phase 0 design §14; free-tier retarget design §5).

``analyze_plan`` reads one ``EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)`` result and says
which indexes the plan used, whether it scanned the spike table sequentially, and where
the ACL predicate was applied. It never raises: a value of an unexpected type is skipped,
and the raw plan is kept as given.

ACL filter locations (Requirement 3.5), anywhere in the plan tree, in precedence order (a
plan can show both kinds of evidence; both are kept in ``acl_filter_evidence``):

- ``index``: an index scan node on the ACL GIN index (a ``Bitmap Index Scan``, usually
  under a ``BitmapAnd`` with the FTS index), so the index applies the predicate.
- ``filter``: a node's ``Filter`` or ``Recheck Cond`` mentions the ``acl`` column, so the
  predicate runs on heap tuples.
- ``none``: nothing in the plan applies the predicate (the superuser baseline without the
  explicit predicate, or a plan the analyzer cannot read).
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Final, Literal

AclFilterLocation = Literal["index", "filter", "none"]
Role = Literal["cairn_app", "superuser_baseline"]

ACL_LOCATIONS: Final[tuple[AclFilterLocation, ...]] = ("index", "filter", "none")
_EVIDENCE_ORDER: Final[tuple[AclFilterLocation, ...]] = ACL_LOCATIONS[:2]
"""The locations a plan can show evidence for, in precedence order."""

INDEX_SCAN_TYPES: Final = frozenset({"Index Scan", "Index Only Scan", "Bitmap Index Scan"})
"""Node types that scan an index and name it in ``Index Name``."""
_FILTER_KEYS: Final = ("Filter", "Recheck Cond")
_MAX_NODES: Final = 100_000
"""Upper bound on nodes visited, so a hostile or corrupt plan cannot run away."""


@dataclass(frozen=True)
class IndexNames:
    """Names the analyzer looks for."""

    table: str = "chunks"
    fts: str = "chunks_fts_idx"
    hnsw: str = "chunks_embedding_hnsw_idx"
    gin: str = "chunks_acl_gin_idx"
    acl_column: str = "acl"


@dataclass(frozen=True)
class PlanFacts:
    """What one plan shows. The first block of fields is the design's; the rest are
    additions: the evidence behind ``acl_filter_location`` and the raw plan."""

    variant: str
    principal: str
    role: Role
    iterative_scan: str
    node_types: tuple[str, ...]
    uses_fts_index: bool
    uses_hnsw_index: bool
    seq_scan_on_chunks: bool
    acl_filter_location: AclFilterLocation
    rows_returned: int
    rows_removed_by_filter: int
    execution_ms: float
    recall_at_10: float | None = None
    acl_filter_evidence: tuple[AclFilterLocation, ...] = ()
    raw_plan: Any = field(default=None, repr=False, compare=False)


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return int(value)
    return None


def _as_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        try:
            result = float(value)
        except OverflowError:  # an integer beyond the float range
            return None
        return result if math.isfinite(result) else None
    return None


def _text(node: Mapping[str, Any], key: str) -> str:
    value = node.get(key)
    return value if isinstance(value, str) else ""


def _root(plan_json: Any) -> tuple[Mapping[str, Any] | None, Mapping[str, Any]]:
    """``(root plan node, top-level object)`` from any of the shapes EXPLAIN can hand us:
    the one-element list psycopg returns, the object inside it, or a bare node."""
    top = plan_json
    if isinstance(top, list | tuple):
        top = top[0] if top else None
    if not isinstance(top, Mapping):
        return None, {}
    inner = top.get("Plan")
    if isinstance(inner, Mapping):
        return inner, top
    if "Node Type" in top:
        return top, {}
    return None, top


def _nodes(root: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """Every plan node, parents before children, in plan order."""
    found: list[Mapping[str, Any]] = []
    stack: list[Mapping[str, Any]] = [root]
    while stack and len(found) < _MAX_NODES:
        node = stack.pop()
        found.append(node)
        children = node.get("Plans")
        if isinstance(children, list | tuple):
            stack.extend(c for c in reversed(children) if isinstance(c, Mapping))
    return found


def _node_type(node: Mapping[str, Any]) -> str:
    kind = _text(node, "Node Type")
    provider = _text(node, "Custom Plan Provider")
    return f"{kind} ({provider})" if kind and provider else kind


def _scans_index(node: Mapping[str, Any], index: str) -> bool:
    return _text(node, "Node Type") in INDEX_SCAN_TYPES and _text(node, "Index Name") == index


def _acl_evidence(nodes: list[Mapping[str, Any]], names: IndexNames) -> set[AclFilterLocation]:
    mention = re.compile(rf"\b{re.escape(names.acl_column)}\b")
    found: set[AclFilterLocation] = set()
    for node in nodes:
        if _scans_index(node, names.gin):
            found.add("index")
        if any(mention.search(_text(node, key)) for key in _FILTER_KEYS):
            found.add("filter")
    return found


def analyze_plan(
    plan_json: Any,
    indexes: IndexNames | None = None,
    *,
    variant: str = "",
    principal: str = "",
    role: Role = "cairn_app",
    iterative_scan: str = "off",
    recall_at_10: float | None = None,
) -> PlanFacts:
    """Reduce one EXPLAIN JSON result to ``PlanFacts``. Pure and total: never raises.

    The keyword arguments only label the result; ``recall_at_10`` is measured elsewhere.
    ``plan_json`` is what psycopg returns for the statement (a one-element list), the
    object in it, or a bare plan node.
    """
    names = indexes if indexes is not None else IndexNames()
    root, top = _root(plan_json)
    nodes = _nodes(root) if root is not None else []

    seq_scan = any(
        _text(n, "Node Type") in ("Seq Scan", "Sample Scan")
        and names.table in (_text(n, "Relation Name"), _text(n, "Alias"))
        for n in nodes
    )

    removed = 0
    for node in nodes:
        count = _as_int(node.get("Rows Removed by Filter"))
        if count is not None:
            removed += count * max(_as_int(node.get("Actual Loops")) or 1, 1)

    evidence = _acl_evidence(nodes, names)
    location: AclFilterLocation = next((loc for loc in _EVIDENCE_ORDER if loc in evidence), "none")

    returned = _as_int(root.get("Actual Rows")) if root is not None else None
    exec_ms = _as_float(top.get("Execution Time"))
    return PlanFacts(
        variant=variant,
        principal=principal,
        role=role,
        iterative_scan=iterative_scan,
        node_types=tuple(t for t in (_node_type(n) for n in nodes) if t),
        uses_fts_index=any(_scans_index(n, names.fts) for n in nodes),
        uses_hnsw_index=any(_scans_index(n, names.hnsw) for n in nodes),
        seq_scan_on_chunks=seq_scan,
        acl_filter_location=location,
        rows_returned=returned if returned is not None else 0,
        rows_removed_by_filter=removed,
        execution_ms=exec_ms if exec_ms is not None else 0.0,
        recall_at_10=recall_at_10,
        acl_filter_evidence=tuple(loc for loc in _EVIDENCE_ORDER if loc in evidence),
        raw_plan=plan_json,
    )
