"""Load an S1 dataset into Postgres with binary COPY (design §13; Requirement 15.10).

The connection must be to a database built by ``bootstrap_database`` (the ``vector``
extension is there) and its role must be allowed to create the table, which the owner
role is. Row groups are read one at a time, so memory stays near one batch.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Final

import numpy as np
import psycopg
import pyarrow.parquet as pq
from pgvector.psycopg import register_vector
from psycopg import sql

from cairn_eval.spikes.s1_acl_corpus.generate import (
    CHUNKS_FILE,
    MANIFEST_FILE,
    DatasetError,
    read_manifest,
    sha256_file,
)

DEFAULT_TABLE: Final = "s1_chunks"
COLUMNS: Final = ("chunk_id", "cluster", "body", "embedding", "acl")
COPY_TYPES: Final = ("int8", "int2", "text", "vector", "int4[]")
_IDENTIFIER: Final = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


def table_identifier(table: str) -> sql.Identifier:
    """``name`` or ``schema.name`` as a safely quoted identifier."""
    parts = table.split(".")
    if len(parts) > 2 or not all(_IDENTIFIER.match(p) for p in parts):
        raise ValueError(
            f"invalid table name {table!r}: use lowercase letters, digits, _ and one ."
        )
    return sql.Identifier(*parts)


def verify_dataset(dataset: Path) -> None:
    """Fail unless ``chunks.parquet`` is the file the manifest describes (when one exists)."""
    chunks = dataset / CHUNKS_FILE
    if not chunks.is_file():
        raise DatasetError(f"{chunks} does not exist")
    if not (dataset / MANIFEST_FILE).is_file():
        return
    expected = read_manifest(dataset).files[CHUNKS_FILE]["sha256"]
    if sha256_file(chunks) != expected:
        raise DatasetError(f"{chunks} does not match the sha256 in {MANIFEST_FILE}")


def load(
    dataset: Path,
    conn: psycopg.Connection[Any],
    table: str = DEFAULT_TABLE,
    *,
    replace: bool = False,
) -> int:
    """Create ``table`` and copy every row of ``dataset`` into it; return the row count.

    The table is ``(chunk_id bigint primary key, cluster smallint, body text,
    embedding vector(dim), acl int[])``. Creation and copy share one transaction, so a
    failure leaves nothing behind. An existing table is an error unless ``replace``.
    """
    dataset = Path(dataset)
    verify_dataset(dataset)
    name = table_identifier(table)
    parquet = pq.ParquetFile(dataset / CHUNKS_FILE)
    dim = parquet.schema_arrow.field("embedding").type.list_size
    register_vector(conn)
    copied = 0
    with conn.transaction():
        if replace:
            conn.execute(sql.SQL("DROP TABLE IF EXISTS {}").format(name))
        conn.execute(
            sql.SQL(
                "CREATE TABLE {} (chunk_id bigint PRIMARY KEY, cluster smallint NOT NULL, "
                "body text NOT NULL, embedding vector({}) NOT NULL, acl int[] NOT NULL)"
            ).format(name, sql.Literal(dim))
        )
        statement = sql.SQL("COPY {} ({}) FROM STDIN (FORMAT BINARY)").format(
            name, sql.SQL(", ").join(sql.Identifier(c) for c in COLUMNS)
        )
        with conn.cursor() as cur, cur.copy(statement) as copy:
            copy.set_types(list(COPY_TYPES))
            for group in range(parquet.num_row_groups):
                batch = parquet.read_row_group(group)
                ids = batch.column("chunk_id").to_pylist()
                clusters = batch.column("cluster").to_pylist()
                bodies = batch.column("body").to_pylist()
                acls = batch.column("acl").to_pylist()
                embeddings: Any = batch.column("embedding").combine_chunks()
                vectors = (
                    embeddings.flatten()
                    .to_numpy(zero_copy_only=False)
                    .astype(np.float32, copy=False)
                    .reshape(len(ids), dim)
                )
                for row in range(len(ids)):
                    copy.write_row((ids[row], clusters[row], bodies[row], vectors[row], acls[row]))
                copied += len(ids)
    return copied
