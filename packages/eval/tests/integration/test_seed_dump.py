"""Seed dump, load and reset (plan 6.4): the frozen corpus travels with its embeddings and
decisions, a load spends no neurons, and it refuses to pass the size limit."""

from __future__ import annotations

import gzip
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import psycopg
import pytest

from cairn_core.testing import DbTestConfig, FreshDb, temporary_database
from cairn_eval import seed_dump
from cairn_eval.decision_seed import seed_decisions
from cairn_eval.seed import SeededCorpus, seed_database

pytestmark = pytest.mark.integration


@pytest.fixture
def source(
    admin_conn: psycopg.Connection[Any], admin_config: DbTestConfig, tmp_path: Path
) -> Iterator[tuple[FreshDb, SeededCorpus, Path, dict[str, int]]]:
    if admin_config.settings.ingest_db_password is None:
        pytest.skip("CAIRN_INGEST_DB_PASSWORD is not set (make env-update)")
    with temporary_database(admin_conn, admin_config) as db:
        assert db.ingest_dsn is not None
        corpus = seed_database(db.ingest_dsn, tmp_path / "corpus")
        with db.connect_ingest() as conn:
            seed_decisions(conn, corpus.scenario, corpus.manifest)
        file = tmp_path / "kestrel.seed.gz"
        with psycopg.connect(db.owner_dsn) as owner:
            counts = seed_dump.dump(owner, file)
        yield db, corpus, file, counts


def count(conn: psycopg.Connection[Any], table: str) -> int:
    row = conn.execute(f"SELECT count(*) FROM cairn.{table}").fetchone()  # noqa: S608
    assert row is not None
    return int(row[0])


def test_the_dump_covers_items_chunks_embeddings_acls_and_decisions(
    source: tuple[FreshDb, SeededCorpus, Path, dict[str, int]],
) -> None:
    _, _, file, counts = source
    for table in (
        "items",
        "revisions",
        "chunks",
        "embeddings",
        "item_acls",
        "principals",
        "decisions",
        "decision_events",
        "claims",
        "claim_support",
    ):
        assert counts[table] > 0, table
    assert counts["embeddings"] == counts["chunks"]  # one stored vector per chunk
    assert file.stat().st_size < 5 * 1024 * 1024  # a few MB at most (Estimate)
    assert "audit_log" not in seed_dump.TABLES
    assert "inbox" not in seed_dump.TABLES


def test_the_dump_is_byte_for_byte_reproducible(
    source: tuple[FreshDb, SeededCorpus, Path, dict[str, int]], tmp_path: Path
) -> None:
    db, _, file, _ = source
    again = tmp_path / "again.seed.gz"
    with psycopg.connect(db.owner_dsn) as owner:
        seed_dump.dump(owner, again)
    assert gzip.decompress(file.read_bytes()) == gzip.decompress(again.read_bytes())


def test_loading_into_an_empty_database_reproduces_everything_with_no_model_call(
    source: tuple[FreshDb, SeededCorpus, Path, dict[str, int]],
    admin_conn: psycopg.Connection[Any],
    admin_config: DbTestConfig,
) -> None:
    _, _, file, counts = source
    with (
        temporary_database(admin_conn, admin_config) as target,
        psycopg.connect(target.owner_dsn) as owner,
    ):
        loaded = seed_dump.load(owner, file)
        assert loaded == counts
        for table, n in counts.items():
            assert count(owner, table) == n, table
        # vectors arrived as stored: no embedding call was needed (none was available)
        v = owner.execute(
            "SELECT count(*) FROM cairn.embeddings WHERE embedding IS NOT NULL AND model_key = 'fake-embed-v1'"  # noqa: E501
        ).fetchone()
        assert v == (counts["embeddings"],)
        # new rows get ids past the loaded ones
        owner.execute(
            "INSERT INTO cairn.decisions (key, title, origin) VALUES ('X-NEW', 't', 'manual')"
        )
        top = owner.execute("SELECT max(decision_id), count(*) FROM cairn.decisions").fetchone()
        assert top is not None
        assert top[0] >= top[1]


def test_a_load_never_merges_into_content_and_reset_replaces_it(
    source: tuple[FreshDb, SeededCorpus, Path, dict[str, int]],
) -> None:
    db, _, file, counts = source
    with psycopg.connect(db.owner_dsn) as owner:
        with pytest.raises(ValueError, match="not empty"):
            seed_dump.load(owner, file)
        owner.rollback()
        owner.execute(
            "UPDATE cairn.items SET title = 'changed' WHERE item_id = (SELECT min(item_id) FROM cairn.items)"  # noqa: E501
        )
        owner.execute(
            "DELETE FROM cairn.chunks WHERE chunk_id = (SELECT min(chunk_id) FROM cairn.chunks)"
        )
        owner.commit()
        assert seed_dump.load(owner, file, reset=True) == counts
        assert count(owner, "chunks") == counts["chunks"]
        assert owner.execute(
            "SELECT count(*) FROM cairn.items WHERE title = 'changed'"
        ).fetchone() == (0,)


def test_it_refuses_before_changing_anything_when_the_limit_would_be_passed(
    source: tuple[FreshDb, SeededCorpus, Path, dict[str, int]],
) -> None:
    db, _, file, counts = source
    with psycopg.connect(db.owner_dsn) as owner:
        before = count(owner, "items")
        with pytest.raises(seed_dump.SeedTooLarge):
            seed_dump.load(owner, file, reset=True, limit_bytes=1024)
        owner.rollback()
        assert count(owner, "items") == before == counts["items"]  # nothing was truncated


def test_a_foreign_or_corrupt_file_is_rejected(tmp_path: Path) -> None:
    bad = tmp_path / "bad.gz"
    bad.write_bytes(gzip.compress(b'{"format": "something-else", "tables": {}}'))
    with pytest.raises(ValueError, match="not a cairn-seed-v1"):
        seed_dump.read(bad)
    worse = tmp_path / "worse.gz"
    worse.write_bytes(gzip.compress(b'{"format": "cairn-seed-v1", "tables": {"audit_log": []}}'))
    with pytest.raises(ValueError, match="unknown tables"):
        seed_dump.read(worse)
