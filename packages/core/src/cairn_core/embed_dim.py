"""Set the embedding dimension of ``cairn.embeddings`` for a deployment.

Migration 0004 creates ``embedding vector(384)`` (the dimension of the Workers AI default). A
deployment that embeds with another model, such as Amazon Titan Text Embeddings V2 (256, 512 or
1024), changes the column once, right after the migrations and before any vector is stored:

- an empty table is altered and its HNSW index rebuilt, in one transaction;
- a table that already holds vectors of another dimension is refused (nothing changes), because a
  vector of one dimension cannot be cast to another. ``reset=True`` first truncates the table; the
  vectors are derived data and the outbox re-embeds every revision that lacks one for the
  configured model.

The vector search and the stored ``model_key`` never mix dimensions: a query is embedded by the
same model that embedded the chunks.
"""

from __future__ import annotations

from typing import Any, Final

import psycopg
from psycopg import errors

__all__ = ["MAX_HNSW_DIM", "EmbedDimError", "current_dimension", "ensure_embedding_dimension"]

MAX_HNSW_DIM: Final = 2000
"""pgvector builds an HNSW index on a ``vector`` column of at most 2,000 dimensions."""
MIN_DIM: Final = 8

_CURRENT_SQL: Final = (
    "SELECT a.atttypmod FROM pg_attribute a "
    "WHERE a.attrelid = 'cairn.embeddings'::regclass AND a.attname = 'embedding' "
    "AND NOT a.attisdropped"
)
_DROP_INDEX: Final = "DROP INDEX IF EXISTS cairn.embeddings_hnsw_idx"
_CREATE_INDEX: Final = (
    "CREATE INDEX embeddings_hnsw_idx ON cairn.embeddings "
    "USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64)"
)


class EmbedDimError(Exception):
    """The dimension cannot be changed (invalid, or vectors of another dimension exist)."""


def current_dimension(conn: psycopg.Connection[Any]) -> int:
    row = conn.execute(_CURRENT_SQL).fetchone()
    if row is None or row[0] is None or int(row[0]) <= 0:
        raise EmbedDimError(
            "cairn.embeddings.embedding has no dimension (is migration 0004 applied?)"
        )
    return int(row[0])


def ensure_embedding_dimension(
    conn: psycopg.Connection[Any], dim: int, *, reset: bool = False
) -> str:
    """Make ``cairn.embeddings.embedding`` a ``vector(dim)``. Returns what happened (one line)."""
    if not MIN_DIM <= dim <= MAX_HNSW_DIM:
        raise EmbedDimError(f"dimension must be between {MIN_DIM} and {MAX_HNSW_DIM}")
    have = current_dimension(conn)
    if have == dim:
        return f"embedding dimension is already {dim}; nothing changed"
    try:
        with conn.transaction():
            if reset:
                conn.execute("TRUNCATE cairn.embeddings")
            conn.execute(_DROP_INDEX)
            conn.execute(f"ALTER TABLE cairn.embeddings ALTER COLUMN embedding TYPE vector({dim})")
            conn.execute(_CREATE_INDEX)
    except errors.DataException as exc:
        # the transaction block rolled everything back, including the index drop
        raise EmbedDimError(
            f"cairn.embeddings holds {have}-dimension vectors; they cannot become {dim}. "
            "Re-run with --reset-embeddings to delete them (they are re-created by the embedder)."
        ) from exc
    return f"embedding dimension changed from {have} to {dim}" + (
        " (existing embeddings deleted)" if reset else ""
    )
