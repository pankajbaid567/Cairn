"""Embed the current chunks of a revision (design §9; PD1-23, PD1-28).

Shared by the embedding workflow in the worker and by ``cairn-import --embed-inline``. Batches
are at most 50 (the Workers AI limit, FV8). ``WorkersAiError("budget_exhausted")`` propagates
unchanged. Only current chunks get embeddings, and an embedding is written only if its chunk is
still current when the insert runs.
"""

from __future__ import annotations

from typing import Any, Final

from psycopg import AsyncConnection

from cairn_retrieval.models.protocols import Embedder
from cairn_retrieval.vector import vector_literal

MAX_BATCH: Final = 50


async def chunks_missing_embedding(
    conn: AsyncConnection[Any], revision_id: int, model_key: str
) -> list[tuple[int, str]]:
    """``(chunk_id, text to embed)`` for current chunks of the revision lacking this model's vector."""  # noqa: E501
    rows = await (
        await conn.execute(
            "SELECT c.chunk_id, c.title, c.text FROM cairn.chunks c "
            "WHERE c.revision_id = %s AND c.is_current AND NOT EXISTS ("
            "SELECT 1 FROM cairn.embeddings e WHERE e.chunk_id = c.chunk_id AND e.model_key = %s) "
            "ORDER BY c.ordinal",
            (revision_id, model_key),
        )
    ).fetchall()
    return [(int(r[0]), embedding_text(str(r[1]), str(r[2]))) for r in rows]


def embedding_text(title: str, text: str) -> str:
    """What is embedded for a chunk: its item title and its text."""
    return f"{title}\n{text}"


async def embed_revision(
    conn: AsyncConnection[Any], embedder: Embedder, revision_id: int, *, batch: int = 32
) -> int:
    """Embed and store the missing vectors of one revision; returns the rows inserted."""
    batch = max(1, min(batch, MAX_BATCH))
    todo = await chunks_missing_embedding(conn, revision_id, embedder.model_key)
    inserted = 0
    for i in range(0, len(todo), batch):
        part = todo[i : i + batch]
        vectors = await embedder.embed_documents([t for _, t in part])
        async with conn.transaction():
            for (chunk_id, _), vector in zip(part, vectors, strict=True):
                cur = await conn.execute(
                    "INSERT INTO cairn.embeddings (chunk_id, model_key, embedding, acl) "
                    "SELECT chunk_id, %s, %s::vector, acl FROM cairn.chunks "
                    "WHERE chunk_id = %s AND is_current "
                    "ON CONFLICT (chunk_id, model_key) DO NOTHING",
                    (embedder.model_key, vector_literal(vector), chunk_id),
                )
                inserted += cur.rowcount
    return inserted


async def missing_embedding_revisions(conn: AsyncConnection[Any], model_key: str) -> list[int]:
    """Revisions that have current chunks without a vector for ``model_key``."""
    rows = await (
        await conn.execute(
            "SELECT DISTINCT c.revision_id FROM cairn.chunks c WHERE c.is_current AND NOT EXISTS ("
            "SELECT 1 FROM cairn.embeddings e WHERE e.chunk_id = c.chunk_id AND e.model_key = %s) "
            "ORDER BY 1",
            (model_key,),
        )
    ).fetchall()
    return [int(r[0]) for r in rows]
