"""The outbox drainer (design §9; PD1-07, PD1-28, ADR-0011).

DBOS lives in its own database, so the import transaction cannot enqueue a workflow. It writes
an ``outbox`` row instead, and this thread turns rows into workflow starts:

- ``drain_once`` claims up to 100 pending rows with ``FOR UPDATE SKIP LOCKED``, enqueues each
  with the workflow id ``embed-<event_id>``, marks them dispatched and commits. A crash between
  enqueue and commit re-enqueues the same ids, which DBOS deduplicates.
- ``repair_once`` runs once at start (the service sleeps, so rows may be waiting and some
  revisions may have lost their vectors): it queues outbox rows for revisions that miss vectors,
  but only when the neuron budget has room for them.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import Any, Final

import psycopg

log = logging.getLogger(__name__)

BATCH: Final = 100
Enqueue = Callable[[int, int], None]
"""``(event_id, revision_id) -> None``: start ``embed_revision_workflow`` with id ``embed-<event_id>``."""  # noqa: E501


class OutboxDrainer(threading.Thread):
    def __init__(
        self,
        connect: Callable[[], psycopg.Connection[Any]],
        enqueue: Enqueue,
        *,
        poll_s: float = 2.0,
        model_key: str | None = None,
        has_budget_room: Callable[[psycopg.Connection[Any]], bool] | None = None,
    ) -> None:
        super().__init__(name="cairn-outbox-drainer", daemon=True)
        self._connect = connect
        self._enqueue = enqueue
        self._poll_s = poll_s
        self._model_key = model_key
        self._room = has_budget_room
        self._stop_event = threading.Event()
        self.dispatched = 0

    def drain_once(self) -> int:
        """Dispatch up to :data:`BATCH` pending rows. Returns how many were dispatched."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT event_id, revision_id FROM cairn.outbox WHERE dispatched_at IS NULL "
                "ORDER BY event_id LIMIT %s FOR UPDATE SKIP LOCKED",
                (BATCH,),
            ).fetchall()
            for event_id, revision_id in rows:
                self._enqueue(int(event_id), int(revision_id))
            if rows:
                conn.execute(
                    "UPDATE cairn.outbox SET dispatched_at = now(), attempts = attempts + 1 "
                    "WHERE event_id = ANY(%s)",
                    ([int(r[0]) for r in rows],),
                )
            conn.commit()
        self.dispatched += len(rows)
        return len(rows)

    def repair_once(self) -> int:
        """Queue outbox rows for current chunks without vectors, if the budget has room."""
        if self._model_key is None:
            return 0
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT DISTINCT c.revision_id FROM cairn.chunks c WHERE c.is_current "
                "AND NOT EXISTS (SELECT 1 FROM cairn.embeddings e WHERE e.chunk_id = c.chunk_id "
                "AND e.model_key = %s) AND NOT EXISTS (SELECT 1 FROM cairn.outbox o "
                "WHERE o.revision_id = c.revision_id AND o.dispatched_at IS NULL)",
                (self._model_key,),
            ).fetchall()
            conn.rollback()
            if not rows:
                return 0
            if self._room is not None and not self._room(conn):
                log.warning(
                    "outbox.repair_skipped",
                    extra={"reason": "no_budget_room", "revisions": len(rows)},
                )
                return 0
            for (rid,) in rows:
                conn.execute(
                    "INSERT INTO cairn.outbox (kind, revision_id) VALUES ('embed_revision', %s)",
                    (rid,),
                )
            conn.commit()
        log.info("outbox.repaired", extra={"revisions": len(rows)})
        return len(rows)

    def run(self) -> None:
        try:
            self.repair_once()
        except Exception as exc:
            log.warning("outbox.repair_failed", extra={"error_class": type(exc).__name__})
        while not self._stop_event.is_set():
            try:
                if self.drain_once() == BATCH:
                    continue  # more waiting: no pause
            except Exception as exc:  # DBOS or the database is away: wait and try again
                log.warning("outbox.drain_failed", extra={"error_class": type(exc).__name__})
            self._stop_event.wait(self._poll_s)

    def stop(self, timeout: float = 5.0) -> None:
        self._stop_event.set()
        if self.is_alive():
            self.join(timeout)
