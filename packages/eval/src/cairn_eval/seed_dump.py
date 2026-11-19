"""Data-only seed dump, load and reset (plan 6.4).

A demo starts from the frozen corpus: items, revisions, chunks **with their stored embeddings**,
ACLs, principals, memberships and the decision tables. The dump is a gzip of JSON rows, one list per
table, taken with ``to_jsonb`` and loaded with ``jsonb_populate_record`` so every column, array,
range and vector round-trips through its own type. Loading therefore calls no model and spends no
neurons.

Loading runs as ``cairn_owner`` (the laptop-only credential): it owns the tables and its policies
allow writing. It refuses when the dump plus the current database would pass the limit (512 MB, half
of Neon free's 1 GB), and ``reset`` truncates the content tables first so the result is exactly the
dump. Never dumped: the audit log, the sync inbox, caches and cached summaries.
"""

from __future__ import annotations

import gzip
import json
from pathlib import Path
from typing import Any, Final

import psycopg

Conn = psycopg.Connection[Any]
LIMIT_BYTES: Final = 512 * 1024 * 1024
FORMAT: Final = "cairn-seed-v1"

TABLES: Final = (
    "principals",
    "memberships",
    "acl_versions",
    "items",
    "item_acls",
    "revisions",
    "chunks",
    "embeddings",
    "decisions",
    "decision_events",
    "decision_versions",
    "claims",
    "claim_support",
    "decision_links",
    "anchors",
    "entities",
    "aliases",
    "lineage",
    "review_items",
    "conflict_sides",
    "extractions",
)
"""Content and decision tables, in no required order (there are no foreign keys, ADR-0004)."""


class SeedTooLarge(Exception):
    """The dump plus the current database would pass the size limit."""


def database_bytes(conn: Conn) -> int:
    row = conn.execute("SELECT pg_database_size(current_database())").fetchone()
    assert row is not None  # noqa: S101
    return int(row[0])


BASELINE_PRINCIPALS: Final = ("group:public", "group:employees")
"""Seeded by migration 0003 in every database; a database holding only these counts as empty."""


def _columns(conn: Conn, table: str) -> tuple[list[str], list[str]]:
    """``(insertable columns, generated columns)`` of ``cairn.<table>``."""
    rows = conn.execute(
        "SELECT attname, attgenerated <> '' FROM pg_attribute WHERE attrelid = ('cairn.' || %s)::regclass "  # noqa: E501
        "AND attnum > 0 AND NOT attisdropped ORDER BY attnum",
        (table,),
    ).fetchall()
    return [str(n) for n, g in rows if not g], [str(n) for n, g in rows if g]


def dump(conn: Conn, path: Path) -> dict[str, int]:
    """Write every table to ``path`` (gzip). Returns the row count per table."""
    payload: dict[str, Any] = {"format": FORMAT, "tables": {}}
    counts: dict[str, int] = {}
    for table in TABLES:
        _, generated = _columns(conn, table)
        strip = "".join(f" - '{g}'" for g in generated)  # generated columns are recomputed on load
        rows = conn.execute(f"SELECT to_jsonb(t){strip} FROM cairn.{table} t").fetchall()  # noqa: S608
        # a stable order: sort the JSON text, so the same data always gives the same bytes
        payload["tables"][table] = sorted(
            (r[0] for r in rows), key=lambda x: json.dumps(x, sort_keys=True)
        )
        counts[table] = len(rows)
    conn.rollback()
    data = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.GzipFile(path, "wb", mtime=0) as fh:  # mtime 0: identical bytes for identical data
        fh.write(data)
    return counts


def read(path: Path) -> dict[str, list[dict[str, Any]]]:
    with gzip.open(path, "rb") as fh:
        payload = json.loads(fh.read())
    if payload.get("format") != FORMAT:
        raise ValueError(f"not a {FORMAT} seed file")
    tables = payload["tables"]
    unknown = set(tables) - set(TABLES)
    if unknown:
        raise ValueError(f"unknown tables in the seed file: {sorted(unknown)}")
    return tables


def load(
    conn: Conn, path: Path, *, reset: bool = False, limit_bytes: int = LIMIT_BYTES
) -> dict[str, int]:
    """Load the dump in one transaction; with ``reset`` the content tables are truncated first.

    Raises :class:`SeedTooLarge` before changing anything if the result could pass ``limit_bytes``.
    Without ``reset`` the tables must be empty (a seed never merges into existing content).
    """
    tables = read(path)
    uncompressed = len(json.dumps(tables).encode())
    if database_bytes(conn) + uncompressed > limit_bytes:
        raise SeedTooLarge(
            f"{database_bytes(conn)} bytes in use + {uncompressed} bytes of seed would pass {limit_bytes}"  # noqa: E501
        )
    counts: dict[str, int] = {}
    with conn.transaction():
        if reset:
            conn.execute(
                "TRUNCATE " + ", ".join(f"cairn.{t}" for t in TABLES) + " RESTART IDENTITY"
            )
        else:
            for t in TABLES:
                where = " WHERE external_id <> ALL(%s)" if t == "principals" else ""
                args = (list(BASELINE_PRINCIPALS),) if t == "principals" else ()
                row = conn.execute(f"SELECT count(*) FROM cairn.{t}{where}", args).fetchone()  # noqa: S608
                if row is not None and row[0]:
                    raise ValueError(f"cairn.{t} is not empty; use reset to replace the content")
            conn.execute(
                "TRUNCATE cairn.principals"
            )  # only the baseline rows; the dump has them too
        for table in TABLES:
            cols = ", ".join(_columns(conn, table)[0])
            for obj in tables.get(table, []):
                conn.execute(
                    f"INSERT INTO cairn.{table} ({cols}) OVERRIDING SYSTEM VALUE "  # noqa: S608
                    f"SELECT {cols} FROM jsonb_populate_record(NULL::cairn.{table}, %s::jsonb)",
                    (json.dumps(obj),),
                )
            counts[table] = len(tables.get(table, []))
            _reset_sequence(conn, table)
    return counts


def _reset_sequence(conn: Conn, table: str) -> None:
    """After loading explicit ids, move the table's identity sequence past the largest one."""
    row = conn.execute(
        "SELECT a.attname, pg_get_serial_sequence('cairn.' || %s, a.attname) FROM pg_attribute a "
        "WHERE a.attrelid = ('cairn.' || %s)::regclass AND a.attidentity <> '' LIMIT 1",
        (table, table),
    ).fetchone()
    if row is None or row[1] is None:
        return
    conn.execute(
        f"SELECT setval(%s, greatest(coalesce((SELECT max({row[0]}) FROM cairn.{table}), 0), 1), "  # noqa: S608
        f"(SELECT count(*) > 0 FROM cairn.{table}))",
        (row[1],),
    )
