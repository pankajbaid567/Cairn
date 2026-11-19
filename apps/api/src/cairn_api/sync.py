"""Live sync on the API side (plan 3.4, 3.12, 3.14, 3.28).

* ``POST /webhooks/github``: the local receiver. Same contract as the edge Function: verify the
  HMAC in constant time, store a thin row keyed by delivery id, answer 202. It exists so the whole
  loop runs on a laptop and so one set of signed fixtures tests both receivers.
* ``sync_barrier``: drain-before-serve. Every query path calls it first. While an ACL-class event
  received before the request is unapplied it waits (up to ``CAIRN_SYNC_WAIT_S``) and then fails
  closed with 503 ``permissions_syncing``. Content events never block a query.
* ``GET /sync/health``: aggregate counts for the sources page; no row, repo name or text.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any, Final

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import JSONResponse
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from cairn_core.webhook import (
    DELIVERY_HEADER,
    EVENT_HEADER,
    INSERT_SQL,
    MAX_BODY_BYTES,
    SIGNATURE_HEADER,
    insert_params,
    thin_row,
    verify_signature,
)

log = logging.getLogger("cairn.api.sync")
POLL_S: Final = 0.2
RETRY_AFTER_S: Final = 5

InboxWriter = Callable[[tuple[Any, ...]], Awaitable[None]]


def _json(status: int, body: dict[str, Any], headers: dict[str, str] | None = None) -> JSONResponse:
    return JSONResponse(
        body, status_code=status, headers={"Cache-Control": "no-store", **(headers or {})}
    )


async def pending_acl_events(pool: AsyncConnectionPool[AsyncConnection[Any]]) -> tuple[int, Any]:
    """``(count, cutoff)``: unapplied ACL events received before ``cutoff`` (the database clock)."""
    async with pool.connection() as conn:
        cutoff = await (await conn.execute("SELECT clock_timestamp()")).fetchone()
        assert cutoff is not None  # noqa: S101
        row = await (
            await conn.execute("SELECT cairn.pending_acl_events(%s)", (cutoff[0],))
        ).fetchone()
    return (int(row[0]) if row else 0), cutoff[0]


async def count_pending(pool: AsyncConnectionPool[AsyncConnection[Any]], cutoff: Any) -> int:
    async with pool.connection() as conn:
        row = await (
            await conn.execute("SELECT cairn.pending_acl_events(%s)", (cutoff,))
        ).fetchone()
    return int(row[0]) if row else 0


async def sync_barrier(
    pool: AsyncConnectionPool[AsyncConnection[Any]], wait_s: float
) -> JSONResponse | None:
    """``None`` when it is safe to answer; otherwise the 503 to return (fail closed).

    Only ACL-class rows received *before this call* count, so a steady stream of new events cannot
    starve a request, and a request is never answered while a revocation it could be affected by is
    unapplied.
    """
    pending, cutoff = await pending_acl_events(pool)
    if pending == 0:
        return None
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        await asyncio.sleep(POLL_S)
        if await count_pending(pool, cutoff) == 0:
            return None
    log.warning("sync.barrier_closed", extra={"pending_acl": pending})
    return _json(503, {"error": "permissions_syncing"}, {"Retry-After": str(RETRY_AFTER_S)})


async def sync_summary(pool: AsyncConnectionPool[AsyncConnection[Any]]) -> dict[str, Any]:
    async with pool.connection() as conn:
        row = await (await conn.execute("SELECT * FROM cairn.sync_summary()")).fetchone()
    if row is None:
        return {
            "pending_acl": 0,
            "pending_content": 0,
            "oldest_pending_s": None,
            "last_applied_at": None,
        }
    applied = row[3]
    return {
        "pending_acl": row[0],
        "pending_content": row[1],
        "oldest_pending_s": None if row[2] is None else round(float(row[2]), 1),
        "last_applied_at": applied.isoformat() if applied is not None else None,
    }


def install_webhooks(app: FastAPI, secret: bytes | None, write_row: InboxWriter | None) -> None:
    """Add ``POST /webhooks/github`` when a secret and a writer are configured."""
    if not secret or write_row is None:
        return
    router = APIRouter()

    @router.post("/webhooks/github")
    async def receive(request: Request) -> JSONResponse:
        declared = request.headers.get("content-length")
        if declared is not None and declared.isdigit() and int(declared) > MAX_BODY_BYTES:
            return _json(413, {"error": "too_large"})
        body = await request.body()
        if len(body) > MAX_BODY_BYTES:
            return _json(413, {"error": "too_large"})
        if not verify_signature(secret, body, request.headers.get(SIGNATURE_HEADER)):
            return _json(401, {"error": "bad_signature"})
        try:
            payload = json.loads(body)
        except ValueError:
            return _json(400, {"error": "bad_json"})
        headers = {
            EVENT_HEADER: request.headers.get(EVENT_HEADER, ""),
            DELIVERY_HEADER: request.headers.get(DELIVERY_HEADER, ""),
        }
        row = thin_row(headers, payload if isinstance(payload, dict) else {})
        if row is None:
            return _json(202, {"status": "ignored"})
        try:
            await write_row(insert_params(row))
        except Exception:
            log.error("webhook.store_failed")
            return _json(500, {"error": "store_failed"})
        return _json(202, {"status": "accepted"})

    app.include_router(router)


def ingest_inbox_writer(connect: Callable[[], Awaitable[AsyncConnection[Any]]]) -> InboxWriter:
    """A writer that inserts one inbox row on a short-lived connection as ``cairn_ingest``."""

    async def write(params: tuple[Any, ...]) -> None:
        conn = await connect()
        try:
            await conn.execute(INSERT_SQL, params)
            await conn.commit()
        finally:
            await conn.close()

    return write
