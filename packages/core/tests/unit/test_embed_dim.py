"""``ensure_embedding_dimension`` against a scripted connection (no database)."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any

import pytest
from psycopg import errors

from cairn_core.embed_dim import EmbedDimError, current_dimension, ensure_embedding_dimension


class _Result:
    def __init__(self, row: tuple[Any, ...] | None) -> None:
        self._row = row

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._row


class FakeConn:
    """Records statements; ``typmod`` is the column dimension, ``fail_alter`` simulates rows."""

    def __init__(self, typmod: int | None, *, fail_alter: bool = False) -> None:
        self.typmod = typmod
        self.fail_alter = fail_alter
        self.statements: list[str] = []
        self.rolled_back = False

    def execute(self, sql: str) -> _Result:
        self.statements.append(sql)
        if sql.startswith("SELECT a.atttypmod"):
            return _Result(None if self.typmod is None else (self.typmod,))
        if sql.startswith("ALTER TABLE") and self.fail_alter:
            raise errors.DataException("expected 1024 dimensions, not 384")
        return _Result(None)

    @contextmanager
    def transaction(self) -> Any:
        try:
            yield
        except Exception:
            self.rolled_back = True
            raise


def test_same_dimension_changes_nothing() -> None:
    conn = FakeConn(384)
    assert "already 384" in ensure_embedding_dimension(conn, 384)  # type: ignore[arg-type]
    assert len(conn.statements) == 1


def test_a_new_dimension_drops_alters_and_rebuilds_the_index_in_that_order() -> None:
    conn = FakeConn(384)
    msg = ensure_embedding_dimension(conn, 1024)  # type: ignore[arg-type]
    assert "from 384 to 1024" in msg
    steps = conn.statements[1:]
    assert steps[0].startswith("DROP INDEX")
    assert steps[1] == "ALTER TABLE cairn.embeddings ALTER COLUMN embedding TYPE vector(1024)"
    assert steps[2].startswith("CREATE INDEX embeddings_hnsw_idx")
    assert "hnsw" in steps[2]
    assert not conn.rolled_back


def test_existing_vectors_of_another_dimension_are_refused_and_rolled_back() -> None:
    conn = FakeConn(384, fail_alter=True)
    with pytest.raises(EmbedDimError, match="--reset-embeddings"):
        ensure_embedding_dimension(conn, 1024)  # type: ignore[arg-type]
    assert conn.rolled_back
    assert not any(s.startswith("CREATE INDEX") for s in conn.statements)


def test_reset_truncates_first() -> None:
    conn = FakeConn(384)
    msg = ensure_embedding_dimension(conn, 512, reset=True)  # type: ignore[arg-type]
    assert conn.statements[1] == "TRUNCATE cairn.embeddings"
    assert "deleted" in msg


@pytest.mark.parametrize("dim", [0, 7, 2001, 100000])
def test_dimension_must_be_one_pgvector_can_index(dim: int) -> None:
    conn = FakeConn(384)
    with pytest.raises(EmbedDimError, match="between"):
        ensure_embedding_dimension(conn, dim)  # type: ignore[arg-type]
    assert not conn.statements


def test_a_missing_column_is_reported() -> None:
    with pytest.raises(EmbedDimError, match="migration 0004"):
        current_dimension(FakeConn(None))  # type: ignore[arg-type]
