"""Review queue routes (plan 4.5). Lists the review items the caller may see and resolves them.

Visibility and the resolution rules live in SQL functions (migration 0012): a conflict is visible
only to someone who can read every side, resolution needs a reason, and an item the caller cannot
see answers exactly like one that does not exist.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Literal

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from psycopg.errors import InvalidParameterValue
from pydantic import BaseModel, ConfigDict, Field

from cairn_api.sync import sync_barrier
from cairn_retrieval.access import principal_transaction


class ResolveBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    state: Literal["verified", "edited", "disputed", "rejected"]
    reason: str = Field(min_length=5, max_length=500)
    edit: dict[str, str] | None = None


STATES = {"open", "verified", "edited", "disputed", "rejected", "all"}
EDITABLE = {"title", "question", "outcome"}


def add_routes(
    router: APIRouter, rt: Any, current: Any, respond: Callable[..., JSONResponse]
) -> None:
    @router.get("/reviews")
    async def list_reviews(request: Request, state: str = "open") -> JSONResponse:
        who = await current(rt, request)
        if who is None:
            return respond(401, {"error": "unauthenticated"})
        if state not in STATES:
            return respond(422, {"error": "invalid_request"})
        if (blocked := await sync_barrier(rt.pool, rt.search.sync_wait_s)) is not None:
            return blocked
        async with principal_transaction(
            rt.pool, who[1], acting_for=who.acting_for, read_only=True
        ) as (conn, _):
            rows = await (
                await conn.execute("SELECT * FROM cairn.list_reviews(%s)", (state,))
            ).fetchall()
        return respond(
            200,
            {
                "reviews": [
                    {
                        "review_id": r[0],
                        "kind": r[1],
                        "decision": r[2],
                        "state": r[3],
                        "reason": r[4],
                        "created_at": r[5].isoformat(),
                        "resolved_at": r[6].isoformat() if r[6] else None,
                        "detail": r[7],
                        "sides": r[8],
                    }
                    for r in rows
                ]
            },
        )

    @router.post("/reviews/{review_id}/resolve")
    async def resolve(review_id: int, body: ResolveBody, request: Request) -> JSONResponse:
        who = await current(rt, request)
        if who is None:
            return respond(401, {"error": "unauthenticated"})
        if who.is_agent:
            return respond(403, {"error": "forbidden"})  # agents are read-only
        if body.edit is not None and (set(body.edit) - EDITABLE or body.state != "edited"):
            return respond(422, {"error": "invalid_request"})
        if (blocked := await sync_barrier(rt.pool, rt.search.sync_wait_s)) is not None:
            return blocked
        import json

        try:
            async with principal_transaction(rt.pool, who[1]) as (conn, _):
                row = await (
                    await conn.execute(
                        "SELECT cairn.resolve_review(%s, %s, %s, %s::jsonb)",
                        (
                            review_id,
                            body.state,
                            body.reason,
                            json.dumps(body.edit) if body.edit else None,
                        ),
                    )
                ).fetchone()
        except InvalidParameterValue:
            return respond(422, {"error": "invalid_request"})
        if not row or not row[0]:
            return respond(404, {"error": "not_found"})  # not visible, or already resolved
        return respond(200, {"review_id": review_id, "state": body.state})
