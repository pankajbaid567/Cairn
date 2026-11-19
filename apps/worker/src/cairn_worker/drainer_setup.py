"""Build the outbox drainer from settings (design §9; PD1-28).

Shared by the split worker and the combined process. Returns ``None`` when the process has no
``CAIRN_INGEST_DB_PASSWORD`` (a Phase 0 deployment), so those processes behave exactly as before.
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime
from typing import Any

import psycopg

from cairn_core.config import IngestSettings, load_settings
from cairn_core.db import connect
from cairn_retrieval.cloudflare.budget import estimate_neurons
from cairn_retrieval.embed import embedding_text
from cairn_retrieval.models.fake import FAKE_EMBED_KEY
from cairn_worker import embedding
from cairn_worker.outbox import OutboxDrainer

log = logging.getLogger(__name__)


def ingest_configured() -> bool:
    if os.environ.get("CAIRN_INGEST_DB_PASSWORD"):
        return True
    try:
        with open(".env", encoding="utf-8") as fh:
            return any(
                line.startswith("CAIRN_INGEST_DB_PASSWORD=")
                and line.strip() != "CAIRN_INGEST_DB_PASSWORD="
                for line in fh
            )
    except OSError:
        return False


def embedder_model_key(settings: IngestSettings) -> str:
    return FAKE_EMBED_KEY if settings.embedder == "fake" else f"workers-ai:{settings.embed_model}"


def make_budget_room(settings: IngestSettings):  # type: ignore[no-untyped-def]
    """True when today's remaining neurons cover embedding every chunk that lacks a vector."""

    def room(conn: psycopg.Connection[Any]) -> bool:
        if settings.embedder == "fake":
            return True
        rows = conn.execute(
            "SELECT c.title, c.text FROM cairn.chunks c WHERE c.is_current AND NOT EXISTS ("
            "SELECT 1 FROM cairn.embeddings e WHERE e.chunk_id = c.chunk_id AND e.model_key = %s)",
            (embedder_model_key(settings),),
        ).fetchall()
        tokens = sum(max(1, -(-len(embedding_text(str(t), str(x))) // 3)) for t, x in rows)
        need = estimate_neurons(settings.embed_model, tokens)
        used_row = conn.execute(
            "SELECT neurons_reserved FROM cairn_meta.provider_usage WHERE day = %s AND provider = 'workers_ai'",  # noqa: E501
            (datetime.now(UTC).date(),),
        ).fetchone()
        conn.rollback()
        used = int(used_row[0]) if used_row else 0
        return need <= settings.daily_neurons - used

    return room


def make_drainer() -> OutboxDrainer | None:
    if not ingest_configured():
        return None
    settings = load_settings(IngestSettings)
    embedding.configure(settings)

    def open_conn() -> psycopg.Connection[Any]:
        return connect(
            settings,
            settings.ingest_db_user,
            settings.ingest_db_password.get_secret_value(),
            application_name="cairn-outbox",
        )

    return OutboxDrainer(
        open_conn,
        embedding.enqueue_embed,
        poll_s=settings.outbox_poll_s,
        model_key=embedder_model_key(settings),
        has_budget_room=make_budget_room(settings),
    )
