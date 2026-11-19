"""``cairn-db set-embed-dim`` logic against a real PostgreSQL with pgvector (needs `make up-db`).

Each test works in its own throwaway database, so the shared test database keeps its 384-dimension
column for every other test.
"""

from __future__ import annotations

from typing import Any

import psycopg
import pytest

from cairn_core.embed_dim import EmbedDimError, current_dimension, ensure_embedding_dimension
from cairn_core.testing import DbTestConfig, temporary_database

pytestmark = pytest.mark.integration

INSERT = (
    "INSERT INTO cairn.embeddings (chunk_id, model_key, embedding, acl) "
    "VALUES (1, 'test-model', array_fill(0.1::real, ARRAY[%s])::vector, '{1}')"
)


def _index_def(conn: psycopg.Connection[Any]) -> str:
    row = conn.execute(
        "SELECT indexdef FROM pg_indexes WHERE indexname = 'embeddings_hnsw_idx'"
    ).fetchone()
    assert row is not None
    return str(row[0])


def test_an_empty_table_is_widened_and_the_hnsw_index_is_rebuilt(
    admin_conn: psycopg.Connection[Any], admin_config: DbTestConfig
) -> None:
    with temporary_database(admin_conn, admin_config) as db, db.connect_owner() as conn:
        assert current_dimension(conn) == 384
        msg = ensure_embedding_dimension(conn, 1024)
        assert "384 to 1024" in msg
        assert current_dimension(conn) == 1024
        definition = _index_def(conn)
        assert "hnsw" in definition
        assert "vector_cosine_ops" in definition
        assert "already 1024" in ensure_embedding_dimension(conn, 1024)  # safe to repeat


def test_vectors_of_another_dimension_block_the_change_until_reset(
    admin_conn: psycopg.Connection[Any], admin_config: DbTestConfig
) -> None:
    with temporary_database(admin_conn, admin_config) as db:
        with db.connect_admin() as admin:  # the superuser bypasses row level security
            admin.execute(INSERT, (384,))
        with db.connect_owner() as conn:
            with pytest.raises(EmbedDimError, match="--reset-embeddings"):
                ensure_embedding_dimension(conn, 1024)
            # nothing changed, and the index is still there
            assert current_dimension(conn) == 384
            assert "hnsw" in _index_def(conn)
            msg = ensure_embedding_dimension(conn, 1024, reset=True)
            assert "deleted" in msg
            assert current_dimension(conn) == 1024
        with db.connect_admin() as admin:
            assert admin.execute("SELECT count(*) FROM cairn.embeddings").fetchone() == (0,)
            admin.execute(INSERT, (1024,))  # the new width takes a 1024-dimension vector
            with pytest.raises(psycopg.errors.DataException):
                admin.execute(INSERT.replace("1, 'test-model'", "2, 'test-model'"), (384,))
