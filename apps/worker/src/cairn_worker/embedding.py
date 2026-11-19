"""Embedding workflow on DBOS (design §9; PD1-07, PD1-28).

The import transaction writes an ``outbox`` row next to the revision. The drainer
(:mod:`cairn_worker.outbox`) enqueues ``embed_revision_workflow`` with the workflow id
``embed-<event_id>``, so a duplicate enqueue reuses the id: delivery is at least once and the
effects are idempotent (``embed_revision`` only inserts missing vectors).

The workflow is synchronous and its step runs the async embedder with ``asyncio.run``; DBOS runs
queued workflows on threads with no event loop, so that is always legal. A step error is retried
by DBOS (3 attempts, backoff 2), except ``budget_exhausted`` and ``daily_limit``: a retry cannot
succeed on the same UTC day, so the step returns ``-1`` and the workflow ends. The chunks stay
without vectors; full-text search still finds them, and ``cairn-import reembed --missing`` or the
next start sweep repairs the gap.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Final

import psycopg
from dbos import DBOS, Queue, SetWorkflowID

from cairn_core.config import IngestSettings
from cairn_retrieval.bedrock.client import BedrockClient
from cairn_retrieval.cloudflare.budget import InMemoryUsageStore, NeuronBudget, PostgresUsageStore
from cairn_retrieval.cloudflare.client import WorkersAiClient, WorkersAiError
from cairn_retrieval.embed import embed_revision
from cairn_retrieval.models.factory import build_embedder

log = logging.getLogger(__name__)

QUEUE_NAME: Final = "cairn-embed"
STOP_CODES: Final = frozenset({"budget_exhausted", "daily_limit"})


@dataclass
class EmbeddingConfig:
    settings: IngestSettings


_config: EmbeddingConfig | None = None


def configure(settings: IngestSettings) -> None:
    """Called once by the process that runs jobs, before any workflow executes."""
    global _config
    _config = EmbeddingConfig(settings)


async def _embed(settings: IngestSettings, revision_id: int) -> int:
    client: WorkersAiClient | None = None
    bedrock: BedrockClient | None = None
    if settings.embedder in ("workers_ai", "bedrock"):
        postgres = (
            settings.budget_store == "postgres"
            if settings.embedder == "workers_ai"
            else settings.bedrock_budget_store == "postgres"
        )
        store = (
            PostgresUsageStore(
                settings,
                user=settings.ingest_db_user,
                password=settings.ingest_db_password.get_secret_value(),
            )
            if postgres
            else InMemoryUsageStore()
        )
        if settings.embedder == "workers_ai":
            client = WorkersAiClient(settings, NeuronBudget(store, settings.daily_neurons))
        else:
            bedrock = BedrockClient(
                settings,
                NeuronBudget(store, settings.bedrock_daily_micro_usd, provider="bedrock"),
            )
    try:
        embedder = build_embedder(settings, client, bedrock)
        tls: dict[str, Any] = (
            {"sslrootcert": settings.db_sslrootcert} if settings.db_sslrootcert else {}
        )
        async with await psycopg.AsyncConnection.connect(
            host=settings.db_host,
            port=settings.db_port,
            dbname=settings.db_name,
            user=settings.ingest_db_user,
            password=settings.ingest_db_password.get_secret_value(),
            connect_timeout=max(1, int(settings.db_timeout_s)),
            application_name="cairn-embedding",
            prepare_threshold=None,
            autocommit=True,
            sslmode=settings.db_sslmode,
            **tls,
        ) as conn:
            return await embed_revision(conn, embedder, revision_id)
    finally:
        if client is not None:
            await client.aclose()


@DBOS.step(retries_allowed=True, max_attempts=3, backoff_rate=2.0)
def embed_revision_step(revision_id: int) -> int:
    if _config is None:
        raise RuntimeError("embedding worker is not configured")
    try:
        return asyncio.run(_embed(_config.settings, revision_id))
    except WorkersAiError as exc:
        if exc.code in STOP_CODES:
            # Code and revision id only; never text (PD1-22).
            log.warning(
                "embedding.stopped", extra={"error_code": exc.code, "revision_id": revision_id}
            )
            return -1
        raise


@DBOS.workflow()
def embed_revision_workflow(revision_id: int) -> int:
    """Embed the current chunks of one revision. Returns vectors inserted, or ``-1`` if stopped."""
    return embed_revision_step(revision_id)


def embed_queue() -> Queue:
    """The ``cairn-embed`` queue, registered on first use (DBOS 3.3 registers queues after launch).
    One workflow at a time per worker keeps the free Workers AI allocation and 0.1 CPU in check."""
    return DBOS.retrieve_queue(QUEUE_NAME) or DBOS.register_queue(QUEUE_NAME, worker_concurrency=1)


def enqueue_embed(event_id: int, revision_id: int) -> None:
    """Start ``embed_revision_workflow`` with the stable id ``embed-<event_id>``.

    A duplicate call (the drainer re-sends after a crash before commit) reuses the id and DBOS
    runs the workflow once.
    """
    queue = embed_queue()
    with SetWorkflowID(f"embed-{event_id}"):
        queue.enqueue(embed_revision_workflow, revision_id)
