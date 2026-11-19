"""The outbox drainer and the embedding workflow against a real database (design §9, PD1-07).

Part 1 uses a plain database and a recording ``enqueue``: claim, dispatch once, roll back on a
failed enqueue, skip locked rows, repair, and the budget-room rule. Part 2 runs the real thing:
DBOS in a throwaway database, a queued ``embed_revision_workflow``, vectors appear, and a second
send with the same workflow id changes nothing (at-least-once delivery, idempotent effects).
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import psycopg
import pytest
from dbos import DBOS

from cairn_connectors.documents import (
    AclRef,
    DirectorySnapshot,
    DirGroup,
    DirMembership,
    DirUser,
    SourceDocument,
)
from cairn_connectors.store import apply_directory, import_document
from cairn_core.bootstrap import bootstrap_database, dbos_spec
from cairn_core.config import IngestSettings, WorkerSettings
from cairn_core.testing import (
    TEST_DB_PREFIX,
    DbTestConfig,
    FreshDb,
    drop_database,
    temporary_database,
)
from cairn_retrieval.models.fake import FAKE_EMBED_KEY
from cairn_worker import app, embedding
from cairn_worker.outbox import OutboxDrainer

pytestmark = pytest.mark.integration

T0 = datetime(2026, 3, 1, tzinfo=UTC)


@pytest.fixture
def db(admin_conn: psycopg.Connection[Any], admin_config: DbTestConfig) -> Iterator[FreshDb]:
    if admin_config.settings.ingest_db_password is None:
        pytest.skip("CAIRN_INGEST_DB_PASSWORD is not set")
    with temporary_database(admin_conn, admin_config) as database:
        assert database.ingest_dsn is not None
        with psycopg.connect(database.ingest_dsn) as conn:
            apply_directory(
                conn,
                DirectorySnapshot(
                    T0.date(),
                    (DirUser("ben", "Ben"),),
                    (DirGroup("team:payments", "Payments"),),
                    (DirMembership("ben", "team:payments", T0.date()),),
                ),
            )
            for i in range(3):
                import_document(
                    conn,
                    SourceDocument(
                        "docs_folder",
                        f"doc:r/{i}.md",
                        "doc",
                        f"Doc {i}",
                        f"# Doc {i}\n\nPostgres ledger text {i}.",
                        T0,
                        frozenset({AclRef("team", "payments")}),
                        {},
                    ),
                )
        yield database


def connect_fn(db: FreshDb):  # type: ignore[no-untyped-def]
    assert db.ingest_dsn is not None
    return lambda: psycopg.connect(db.ingest_dsn, prepare_threshold=None)


def pending(db: FreshDb) -> int:
    with db.connect_owner() as c:
        row = c.execute("SELECT count(*) FROM cairn.outbox WHERE dispatched_at IS NULL").fetchone()
        assert row is not None
        return int(row[0])


def test_drain_dispatches_each_row_once_with_its_event_id(db: FreshDb) -> None:
    sent: list[tuple[int, int]] = []
    drainer = OutboxDrainer(connect_fn(db), lambda e, r: sent.append((e, r)))
    assert pending(db) == 3
    assert drainer.drain_once() == 3
    assert [e for e, _ in sent] == sorted(e for e, _ in sent)
    assert len({r for _, r in sent}) == 3
    assert pending(db) == 0
    assert drainer.drain_once() == 0
    assert len(sent) == 3


def test_a_failed_enqueue_leaves_the_rows_pending(db: FreshDb) -> None:
    def boom(_e: int, _r: int) -> None:
        raise RuntimeError("dbos is away")

    drainer = OutboxDrainer(connect_fn(db), boom)
    with pytest.raises(RuntimeError):
        drainer.drain_once()
    assert pending(db) == 3  # rolled back: sent again later with the same ids


def test_rows_locked_by_another_claimant_are_skipped(db: FreshDb) -> None:
    sent: list[int] = []
    holder = connect_fn(db)()
    holder.execute("SELECT event_id FROM cairn.outbox ORDER BY event_id LIMIT 1 FOR UPDATE")
    try:
        drainer = OutboxDrainer(connect_fn(db), lambda e, _r: sent.append(e))
        assert drainer.drain_once() == 2  # SKIP LOCKED: the held row is left to its claimant
    finally:
        holder.rollback()
        holder.close()
    assert drainer.drain_once() == 1
    assert len(sent) == 3
    assert len(set(sent)) == 3


def test_repair_queues_only_revisions_missing_vectors_and_respects_the_budget(db: FreshDb) -> None:
    sent: list[int] = []
    drainer = OutboxDrainer(connect_fn(db), lambda e, _r: sent.append(e), model_key=FAKE_EMBED_KEY)
    drainer.drain_once()  # the three import rows are dispatched, but no vectors exist yet
    assert drainer.repair_once() == 3
    assert pending(db) == 3
    assert drainer.repair_once() == 0  # pending rows already cover them: no duplicates
    broke = OutboxDrainer(
        connect_fn(db),
        lambda e, _r: None,
        model_key=FAKE_EMBED_KEY,
        has_budget_room=lambda _c: False,
    )
    drainer.drain_once()
    assert broke.repair_once() == 0
    assert pending(db) == 0


# --- the real workflow ---------------------------------------------------------------------


def ingest_settings(db: FreshDb, config: DbTestConfig) -> IngestSettings:
    return IngestSettings.model_validate(
        {
            "CAIRN_DB_HOST": config.target.host,
            "CAIRN_DB_PORT": config.target.port,
            "CAIRN_DB_NAME": db.name,
            "CAIRN_INGEST_DB_PASSWORD": config.settings.ingest_db_password.get_secret_value(),  # type: ignore[union-attr]
            "CAIRN_EMBEDDER": "fake",
            "CAIRN_RERANKER": "fake",
            "CAIRN_MODEL_CACHE": "off",
        }
    )


def vectors(db: FreshDb) -> int:
    with db.connect_owner() as c:
        return int(c.execute("SELECT count(*) FROM cairn.embeddings").fetchone()[0])  # type: ignore[index]


def test_workflow_embeds_through_dbos_and_a_repeated_id_changes_nothing(
    db: FreshDb, admin_conn: psycopg.Connection[Any], admin_config: DbTestConfig
) -> None:
    dbos_name = f"{TEST_DB_PREFIX}dbos_{os.urandom(4).hex()}"
    try:
        bootstrap_database(admin_conn, dbos_name, spec=dbos_spec(dbos_name))
        worker = WorkerSettings.model_validate(
            {
                "CAIRN_DB_HOST": admin_config.target.host,
                "CAIRN_DB_PORT": admin_config.target.port,
                "CAIRN_DBOS_DB_NAME": dbos_name,
                "CAIRN_DBOS_DB_PASSWORD": admin_config.settings.dbos_db_password.get_secret_value(),
                "CAIRN_HEARTBEAT_CRON": "0 0 1 1 *",
                "CAIRN_ENV": "ci",
            }
        )
        embedding.configure(ingest_settings(db, admin_config))
        app.prepare_dbos_logging()
        DBOS(config=app.dbos_config(worker))
        try:
            DBOS.launch()
            drainer = OutboxDrainer(connect_fn(db), embedding.enqueue_embed)
            assert drainer.drain_once() == 3
            deadline = time.monotonic() + 30
            while vectors(db) < 3 and time.monotonic() < deadline:
                time.sleep(0.3)
            assert vectors(db) == 3
            # a second delivery of the same event ids is deduplicated by DBOS, and even if it
            # ran, embed_revision only inserts missing vectors
            with db.connect_owner() as owner:
                owner.execute("UPDATE cairn.outbox SET dispatched_at = NULL")
            assert drainer.drain_once() == 3
            time.sleep(2)
            assert vectors(db) == 3
            ids = {w.workflow_id for w in DBOS.list_workflows(name="embed_revision_workflow")}
            assert len(ids) == 3
            assert all(i.startswith("embed-") for i in ids)
        finally:
            DBOS.destroy()
    finally:
        drop_database(admin_conn, dbos_name)
