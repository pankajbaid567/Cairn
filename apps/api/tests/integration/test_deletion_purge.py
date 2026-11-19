"""Deleting a source purges everything derived from it (plan 3.11, 3.20, 3.22).

Runs on its own database because it deletes data the shared seeded corpus must keep.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import psycopg
import pytest

from cairn_connectors.github.sync import purge_items
from cairn_core.testing import DbTestConfig, FreshDb, temporary_database
from cairn_eval.decision_seed import seed_decisions
from cairn_eval.seed import SeededCorpus, seed_database
from cairn_retrieval.models.cache import ModelCache

pytestmark = pytest.mark.integration


@pytest.fixture
def world(
    admin_conn: psycopg.Connection[Any], admin_config: DbTestConfig, tmp_path: Path
) -> Iterator[tuple[FreshDb, SeededCorpus]]:
    if admin_config.settings.ingest_db_password is None:
        pytest.skip("CAIRN_INGEST_DB_PASSWORD is not set (make env-update)")
    with temporary_database(admin_conn, admin_config) as db:
        assert db.ingest_dsn is not None
        corpus = seed_database(db.ingest_dsn, tmp_path)
        with db.connect_ingest() as conn:
            seed_decisions(conn, corpus.scenario, corpus.manifest)
        yield db, corpus


def n(conn: psycopg.Connection[Any], sql: str, *args: Any) -> int:
    row = conn.execute(sql, args).fetchone()
    assert row is not None
    return int(row[0])


def test_deleting_the_review_notes_removes_its_spans_vectors_cache_and_sole_support(
    world: tuple[FreshDb, SeededCorpus],
) -> None:
    db, corpus = world
    external = corpus.external_id("mtg-arch-review-2026-03-07")
    assert external is not None
    cache = ModelCache(":memory:")
    with db.connect_ingest() as conn:
        conn.autocommit = False
        item = conn.execute(
            "SELECT item_id FROM cairn.items WHERE external_id = %s", (external,)
        ).fetchone()
        assert item is not None
        item_id = int(item[0])
        texts = [
            str(r[0])
            for r in conn.execute(
                "SELECT text FROM cairn.chunks WHERE item_id = %s", (item_id,)
            ).fetchall()
        ]
        assert texts
        cache.put_vectors("fake-embed-v1", "doc", texts, [[0.1] * 4 for _ in texts])
        cache.put_scores("fake-rerank-v1", "q", texts, [0.5 for _ in texts])
        assert (
            n(
                conn,
                "SELECT count(*) FROM cairn.embeddings e JOIN cairn.chunks c USING (chunk_id) WHERE c.item_id = %s",  # noqa: E501
                item_id,
            )
            > 0
        )
        before_versions = n(
            conn,
            "SELECT count(*) FROM cairn.decision_versions v JOIN cairn.decisions d USING (decision_id) WHERE d.key = 'ADR-012'",  # noqa: E501
        )
        out = purge_items(conn, [item_id], cache=cache)
        conn.commit()
    assert out.items == 1
    assert out.chunks == len(texts)
    assert out.embeddings > 0
    assert out.cache_rows == 2 * len(texts)
    assert out.claim_supports >= 1
    assert cache.get_vectors("fake-embed-v1", "doc", texts) == [None] * len(texts)
    assert cache.get_scores("fake-rerank-v1", "q", texts) == [None] * len(texts)
    with db.connect_owner() as c:
        for table, col in (
            ("items", "item_id"),
            ("chunks", "item_id"),
            ("revisions", "item_id"),
            ("item_acls", "item_id"),
        ):
            assert n(c, f"SELECT count(*) FROM cairn.{table} WHERE {col} = %s", item_id) == 0  # noqa: S608
        assert (
            n(
                c,
                "SELECT count(*) FROM cairn.embeddings e WHERE NOT EXISTS (SELECT 1 FROM cairn.chunks k WHERE k.chunk_id = e.chunk_id)",  # noqa: E501
            )
            == 0
        )
        assert n(c, "SELECT count(*) FROM cairn.lineage WHERE input_item_id = %s", item_id) == 0
        assert (
            n(
                c,
                "SELECT count(*) FROM cairn.outbox o WHERE NOT EXISTS (SELECT 1 FROM cairn.revisions r WHERE r.revision_id = o.revision_id)",  # noqa: E501
            )
            == 0
        )
        claim_support = (
            "SELECT count(*) FROM cairn.claim_support s JOIN cairn.claims k USING (claim_id) "
            "JOIN cairn.decisions d USING (decision_id) WHERE d.key = 'ADR-007' AND k.key = %s"
        )
        # C3 rested on that meeting alone: it now has no support set, so nobody can see it
        assert n(c, claim_support, "C3") == 0
        # C1 has other support sets: still supported
        assert n(c, claim_support, "C1") >= 1
        assert n(c, "SELECT count(*) FROM cairn.acl_versions WHERE reason = 'purge'") == 1
        # the acceptance came from the deleted meeting: the event is gone and the versions were rebuilt  # noqa: E501
        assert (
            n(
                c,
                "SELECT count(*) FROM cairn.decision_events e JOIN cairn.decisions d USING (decision_id) "  # noqa: E501
                "WHERE d.key = 'ADR-007' AND e.source_ref = 'mtg-arch-review-2026-03-07'",
            )
            == 0
        )
        status_on_8_march = c.execute(
            "SELECT v.status FROM cairn.decision_versions v JOIN cairn.decisions d USING (decision_id) "  # noqa: E501
            "WHERE d.key = 'ADR-007' AND v.valid_period @> '2026-03-08T23:59:59Z'::timestamptz "
            "AND v.recorded_period @> now()"
        ).fetchone()
        assert status_on_8_march == ("proposed",)  # accepted only according to the deleted notes
        assert before_versions >= 1


def test_visible_claims_after_a_purge_exclude_the_unsupported_one(
    world: tuple[FreshDb, SeededCorpus],
) -> None:
    import asyncio

    from psycopg_pool import AsyncConnectionPool

    from cairn_retrieval.access import principal_transaction
    from cairn_retrieval.decisions import service

    db, corpus = world
    external = corpus.external_id("mtg-arch-review-2026-03-07")
    with db.connect_ingest() as conn:
        conn.autocommit = False
        item = conn.execute(
            "SELECT item_id FROM cairn.items WHERE external_id = %s", (external,)
        ).fetchone()
        assert item is not None
        purge_items(conn, [int(item[0])])
        conn.commit()
    with db.connect_owner() as c:
        everyone = [
            int(r[0])
            for r in c.execute(
                "SELECT principal_id FROM cairn.principals WHERE kind = 'user'"
            ).fetchall()
        ]

    async def run() -> set[str]:
        pool: AsyncConnectionPool[Any] = AsyncConnectionPool(
            db.app_dsn, min_size=1, max_size=2, kwargs={"prepare_threshold": None}, open=False
        )
        await pool.open()
        keys: set[str] = set()
        try:
            for pid in everyone:
                async with principal_transaction(pool, pid, read_only=True) as (conn, snap):
                    if await service.get_decision(conn, snap, "ADR-007") is not None:
                        keys |= {
                            c["key"] for c in await service.visible_claims(conn, snap, "ADR-007")
                        }
        finally:
            await pool.close()
        return keys

    seen = asyncio.run(run())
    assert "C3" not in seen
    assert "C1" in seen  # no principal sees the claim whose only source is gone
