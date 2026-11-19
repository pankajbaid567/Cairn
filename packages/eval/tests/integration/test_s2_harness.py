"""S2 harness against the compose db on 2,000 rows (Requirement 3; needs `make up-db`).

The run builds its own ``cairn_spike_s2_<hex>`` database and drops it at the end; the shared
databases are not touched. The matrix is trimmed (two principals, two iterative-scan
settings) so the test stays quick; ``make s2-smoke`` runs the full matrix.
"""

from __future__ import annotations

import json
import secrets
from pathlib import Path
from typing import Any

import psycopg
import pytest

from cairn_core.testing import DbTestConfig
from cairn_eval.spikes.s2_rls_explain import S2Config, run, write_report
from cairn_eval.spikes.s2_rls_explain.analyze import ACL_LOCATIONS
from cairn_eval.spikes.s2_rls_explain.queries import VARIANTS, is_explicit, shape_of

pytestmark = pytest.mark.integration

PRINCIPALS = ("p01", "p20")
ITERATIVE = ("off", "strict_order")


def test_harness_runs_the_matrix_and_default_deny_holds(
    tmp_path: Path, admin_conn: psycopg.Connection[Any], admin_config: DbTestConfig
) -> None:
    database = "cairn_spike_s2_" + secrets.token_hex(4)
    cfg = S2Config(
        database=database,
        principals=PRINCIPALS,
        iterative_scan=ITERATIVE,
        repeats=1,
        recall_queries=2,
        rows=2000,
        dim=32,
        out_dir=tmp_path / "out",
        drop_after=True,
    )
    report = run(cfg, admin_config.settings, admin_conn=admin_conn)

    exists = admin_conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (database,))
    assert exists.fetchone() is None  # dropped at the end

    deny = report.default_deny
    assert deny.passed
    assert (deny.no_setting_rows, deny.empty_principal_rows, deny.owner_rows) == (0, 0, 0)
    assert deny.row_security_off_error is not None
    assert deny.superuser_rows == 2000

    app = [f for f in report.facts if f.role == "cairn_app"]
    baseline = [f for f in report.facts if f.role == "superuser_baseline"]
    assert len(app) == len(VARIANTS) * len(PRINCIPALS) * len(ITERATIVE)
    # Explicit variants run per principal, the others once per iterative-scan setting.
    assert len(baseline) == (3 * len(PRINCIPALS) + 3) * len(ITERATIVE)
    assert {f.variant for f in report.facts} == set(VARIANTS)
    assert all(f.execution_ms > 0 for f in report.facts)
    assert all(f.raw_plan for f in report.facts)
    assert all(f.acl_filter_location in ACL_LOCATIONS for f in report.facts)
    # Under FORCE RLS the policy applies the ACL predicate in every plan.
    assert all(f.acl_filter_location != "none" for f in app)
    # The superuser has no policy: without the explicit predicate nothing applies it.
    assert all(f.acl_filter_location == "none" for f in baseline if not is_explicit(f.variant))
    # The FTS index exists and the planner can use it: with the explicit predicate and no
    # policy, a selective principal's lexical query reads it (BitmapAnd with the ACL index).
    assert any(
        f.uses_fts_index
        for f in baseline
        if shape_of(f.variant) == "fts" and is_explicit(f.variant)
    )
    hnsw = [f for f in report.facts if f.variant.startswith("hnsw")]
    assert all(f.recall_at_10 is not None and 0.0 <= f.recall_at_10 <= 1.0 for f in hnsw)
    assert all(f.recall_at_10 is None for f in report.facts if not f.variant.startswith("hnsw"))
    assert report.lexical.index_definition == (
        "CREATE INDEX chunks_fts_idx ON s2.chunks USING gin (body_tsv)"
    )
    environment = report.environment
    assert set(environment["extensions"]) == {"vector", "btree_gist"}
    assert environment["image_tag"].startswith("pgvector/pgvector:")
    assert "to_tsvector('simple'::regconfig, body)" in environment["tsv_column"]
    assert "websearch_to_tsquery('simple'" in report.queries["fts"]
    assert report.summary

    markdown, results = write_report(report, cfg.out_dir)
    assert markdown.read_text("utf-8").startswith("# S2 report")
    data = json.loads(results.read_text("utf-8"))
    assert len(data["results"]) == len(report.facts)
    assert data["default_deny"]["passed"] is True
    assert data["schema"] == "cairn-s2-results/2"
