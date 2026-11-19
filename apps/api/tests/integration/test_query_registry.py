"""Query registry tests 1.20 and 1.21 (design §12; PD1-01, PD1-02).

1. Static: no Python module outside ``queries.py`` names a protected relation in a string.
2. Template: every alias bound to a protected relation in FROM or JOIN has an ``{acl:alias}``.
3. Plan: as the RLS-bypassing oracle, EXPLAIN shows an ``acl`` condition on every scan of a
   protected relation, so that condition can only come from the explicit L2 predicate.
4. Backstop: with L2 stripped (test build only) the app role still sees only allowed rows, and with
   no principal, an empty one or a stale '' setting it sees none.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path
from typing import Any

import psycopg
import pytest

from cairn_core.testing import FreshDb
from cairn_eval.seed import SeededCorpus
from cairn_retrieval import queries  # noqa: F401  (registers the queries)
from cairn_retrieval.registry import (
    CLAIM_RELATIONS,
    DECISION_RELATIONS,
    PROTECTED_RELATIONS,
    registered,
    render,
)
from cairn_retrieval.vector import vector_literal

ROOT = Path(__file__).resolve().parents[4]
SCAN_DIRS = ("packages/retrieval/src", "apps/api/src", "apps/combined/src")
SCAN_FILES = ("packages/eval/src/cairn_eval/runners.py",)
# queries.py owns the retrieval SQL. embed.py writes embeddings as cairn_ingest and never serves a
# principal, so it is not a retrieval path.
EXEMPT = {"queries.py", "embed.py", "registry.py"}
PROTECTED_RE = re.compile(
    r"\bcairn\.(items|revisions|chunks|embeddings|decision_events|decision_versions|decisions|claims|anchors)\b"
)


def test_static_no_module_names_a_protected_relation_outside_queries() -> None:
    offenders = []
    files = [p for d in SCAN_DIRS for p in (ROOT / d).rglob("*.py")]
    files += [ROOT / f for f in SCAN_FILES if (ROOT / f).exists()]
    for path in files:
        if path.name in EXEMPT:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and PROTECTED_RE.search(node.value)
            ):
                if ast.get_docstring(tree) == node.value:
                    continue
                offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}")
    assert offenders == [], offenders


_BOUND = re.compile(r"\b(?:FROM|JOIN)\s+(cairn\.\w+)\s+(?:AS\s+)?([a-z_]\w*)", re.IGNORECASE)


@pytest.mark.parametrize("name", sorted(registered()))
def test_template_every_protected_alias_has_a_placeholder(name: str) -> None:
    q = registered()[name]
    pairs = _BOUND.findall(q.template)
    bound = {alias for rel, alias in pairs if rel in PROTECTED_RELATIONS}
    assert bound == set(q.protected_aliases)
    assert {alias for rel, alias in pairs if rel in DECISION_RELATIONS} == set(q.decision_aliases)
    assert {alias for rel, alias in pairs if rel in CLAIM_RELATIONS} == set(q.claim_aliases)
    for alias in bound:
        assert "{acl:%s}" % alias in q.template  # noqa: UP031
    for alias in q.decision_aliases:
        assert "{dvis:%s}" % alias in q.template  # noqa: UP031
    for alias in q.claim_aliases:
        assert "{cvis:%s}" % alias in q.template  # noqa: UP031
    rendered = render(q)
    assert "{acl:" not in rendered
    assert "{dvis:" not in rendered
    assert "{cvis:" not in rendered
    assert rendered.count("%(acl_groups)s::int[]") == len(q.protected_aliases)


def _params(seeded: tuple[FreshDb, SeededCorpus]) -> dict[str, Any]:
    return {
        "q": "postgres or ledger",
        "k": 10,
        "model": "fake-embed-v1",
        "qv": vector_literal([0.1] * 384),
        "ids": [1, 2, 3],
        "acl_groups": [1, 2, 3],
        "start": 0,
        "len": 40,
        "external_id": "doc:eng-docs/adr/ADR-007-postgresql-for-payment-service.md",
        "hash8": "00000000",
        "key": "ADR-007",
        "keys": ["ADR-007"],
        "values": ["payment-service"],
        "pattern": "%adr%",
        "valid": "2026-03-10T23:59:59Z",
        "recorded": "2026-03-10T23:59:59Z",
        "recorded_before": "2026-12-31T00:00:00Z",
    }


def _scans(plan: Any, out: list[dict[str, Any]]) -> None:
    if isinstance(plan, dict):
        if "Relation Name" in plan:
            out.append(plan)
        for v in plan.values():
            _scans(v, out)
    elif isinstance(plan, list):
        for v in plan:
            _scans(v, out)


@pytest.mark.matrix
@pytest.mark.parametrize("name", sorted(registered()))
def test_plan_acl_condition_on_every_protected_scan(
    name: str, seeded: tuple[FreshDb, SeededCorpus]
) -> None:
    q = registered()[name]
    with seeded[0].connect_owner() as oracle:  # bypasses RLS, so an acl condition can only be L2's
        oracle.execute("SET enable_seqscan = off") if False else None
        row = oracle.execute("EXPLAIN (FORMAT JSON) " + render(q), _params(seeded)).fetchone()
    assert row is not None
    scans: list[dict[str, Any]] = []
    _scans(row[0], scans)
    protected = [s for s in scans if f"cairn.{s['Relation Name']}" in PROTECTED_RELATIONS]
    if not protected:
        # decision and claim queries are guarded by the visibility functions, which must be in the plan  # noqa: E501
        assert q.decision_aliases or q.claim_aliases, f"{name}: no protected scan found"
        text = json.dumps(row[0])
        assert "decision_visible" in text or "claim_visible" in text, (
            f"{name}: no visibility guard in the plan"
        )
        return
    for scan in protected:
        text = json.dumps({k: scan.get(k) for k in ("Filter", "Index Cond", "Recheck Cond")})
        assert "acl" in text, (
            f"{name}: scan of {scan['Relation Name']} has no acl condition: {text}"
        )


# --- 1.21 the RLS backstop -----------------------------------------------------------------


@pytest.fixture
def stripped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CAIRN_TEST_STRIP_ACL", "1")
    monkeypatch.setenv("CAIRN_ENV", "local")


def test_strip_is_refused_without_the_test_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CAIRN_TEST_STRIP_ACL", raising=False)
    with pytest.raises(RuntimeError):
        render(registered()["fts_candidates"], strip_acl=True)
    monkeypatch.setenv("CAIRN_TEST_STRIP_ACL", "1")
    monkeypatch.setenv("CAIRN_ENV", "demo")
    with pytest.raises(RuntimeError):
        render(registered()["fts_candidates"], strip_acl=True)


def test_services_refuse_to_load_with_the_strip_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    from pydantic import ValidationError

    from cairn_core.config import ApiSettings, WorkerSettings

    monkeypatch.setenv("CAIRN_TEST_STRIP_ACL", "1")
    with pytest.raises(ValidationError):
        ApiSettings.model_validate({"CAIRN_APP_DB_PASSWORD": "x"})
    with pytest.raises(ValidationError):
        WorkerSettings.model_validate({"CAIRN_DBOS_DB_PASSWORD": "x"})


def _groups_of(db: FreshDb, persona: str) -> list[int]:
    with db.connect_owner() as conn:
        rows = conn.execute(
            "SELECT p.principal_id FROM cairn.principals p WHERE p.external_id IN ('group:public', %s) "  # noqa: E501
            "UNION SELECT group_id FROM cairn.memberships m JOIN cairn.principals u ON u.principal_id = m.member_id "  # noqa: E501
            "WHERE u.external_id = %s AND m.valid_period @> now()",
            (f"user:{persona}", f"user:{persona}"),
        ).fetchall()
    return sorted(int(r[0]) for r in rows)


@pytest.mark.matrix
@pytest.mark.parametrize("persona", ["asha", "ben", "carla", "priya"])
def test_rls_alone_returns_exactly_what_the_explicit_predicate_returns(
    persona: str, seeded: tuple[FreshDb, SeededCorpus], stripped: None
) -> None:
    db = seeded[0]
    groups = _groups_of(db, persona)
    setting = "{" + ",".join(map(str, groups)) + "}"
    params = _params(seeded)
    for name in ("fts_candidates", "dense_candidates", "dense_exact_candidates", "chunk_details"):
        q = registered()[name]
        p = {**params, "acl_groups": groups, "ids": list(range(1, 60))}
        with db.connect_owner() as oracle:
            with_l2 = oracle.execute(render(q), p).fetchall()  # oracle bypasses RLS: L2 only
        with db.connect_app() as app:
            app.execute("SELECT set_config('cairn.principal_groups', %s, false)", (setting,))
            rls_only = app.execute(render(q, strip_acl=True), p).fetchall()  # RLS only
        assert sorted(map(str, rls_only)) == sorted(map(str, with_l2)), (
            f"{name} differs for {persona}"
        )
        assert with_l2, f"{name}: no rows for {persona}, the check would prove nothing"


@pytest.mark.matrix
@pytest.mark.parametrize("setting", [None, "{}", ""])
def test_no_empty_or_stale_principal_setting_sees_nothing(
    setting: str | None, seeded: tuple[FreshDb, SeededCorpus], stripped: None
) -> None:
    db = seeded[0]
    params = {**_params(seeded), "ids": list(range(1, 60))}
    with db.connect_app() as app:
        if setting is not None:
            app.execute("SELECT set_config('cairn.principal_groups', %s, false)", (setting,))
        for name in (
            "fts_candidates",
            "dense_candidates",
            "dense_exact_candidates",
            "chunk_details",
        ):
            rows = app.execute(render(registered()[name], strip_acl=True), params).fetchall()
            assert rows == [], f"{name} returned rows with setting {setting!r}"
        for table in ("items", "revisions", "chunks", "embeddings"):
            assert app.execute(f"SELECT count(*) FROM cairn.{table}").fetchone() == (0,)  # noqa: S608


def test_unused_import_guard() -> None:
    assert psycopg is not None
