"""S1 loader against the compose db (Requirement 15.10; needs `make up-db`).

The test builds its own throwaway ``cairn_test_<hex>`` database through
``bootstrap_database`` and drops it at the end; the shared databases are not touched.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import psycopg
import pytest

from cairn_core.testing import DbTestConfig, temporary_database
from cairn_eval.spikes.s1_acl_corpus import S1Config, generate, load

pytestmark = pytest.mark.integration

ROWS = 500
DIM = 32


def test_500_rows_are_copied_and_counted(
    tmp_path: Path, admin_conn: psycopg.Connection[Any], admin_config: DbTestConfig
) -> None:
    cfg = S1Config(rows=ROWS, dim=DIM, clusters=8, noise_groups=10, queries=5, batch_rows=200)
    manifest = generate(cfg, tmp_path)

    with (
        temporary_database(admin_conn, admin_config, apply_migrations=False) as db,
        db.connect_owner() as conn,
    ):
        assert load(tmp_path, conn, "s1_chunks") == ROWS

        assert conn.execute("SELECT count(*) FROM s1_chunks").fetchone() == (ROWS,)
        dims = conn.execute("SELECT DISTINCT vector_dims(embedding) FROM s1_chunks").fetchall()
        assert dims == [(DIM,)]
        visible = conn.execute("SELECT count(*) FROM s1_chunks WHERE acl && %s::int[]", ([1000],))
        assert visible.fetchone() == (manifest.principals["p01"]["visible_rows"],)
        first = conn.execute(
            "SELECT acl, vector_norm(embedding) FROM s1_chunks WHERE chunk_id = 0"
        ).fetchone()
        assert first is not None
        assert first[0] == sorted(first[0])
        assert first[1] == pytest.approx(1.0, abs=1e-5)

        with pytest.raises(psycopg.errors.DuplicateTable):
            load(tmp_path, conn, "s1_chunks")
        assert load(tmp_path, conn, "s1_chunks", replace=True) == ROWS
