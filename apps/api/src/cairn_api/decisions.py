"""Decision memory routes (Phase 2; plan 2.9, 2.10, 2.13).

``GET /decisions/{key}`` returns the decision page: status as of ``valid`` / ``recorded`` (both
default to now), the claims the caller may see, a summary built only from visible inputs, the
timeline and the 1-hop neighbours. A decision the caller cannot see answers exactly like one that
does not exist (404 ``not_found``). Reads are audited with the ``decision`` endpoint.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime
from typing import Any, Final

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from cairn_api.sync import sync_barrier
from cairn_retrieval.access import principal_transaction
from cairn_retrieval.audit import build_audit_record, write_audit
from cairn_retrieval.decisions import service

log = logging.getLogger("cairn.api.decisions")
KEY: Final = re.compile(r"^[A-Za-z][A-Za-z0-9._-]{0,63}$")
Current = Callable[[Any, Request], Awaitable[Any]]  # an Actor or None (see search.Actor)


def parse_when(value: str | None) -> date | datetime | None:
    """``None`` for "now", a date for a day, or a timestamp. Raises ``ValueError`` otherwise."""
    if value is None or value == "":
        return None
    if len(value) == 10:
        return date.fromisoformat(value)
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _iso(v: Any) -> str | None:
    return v.isoformat() if isinstance(v, datetime) else None


class BriefBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    repo: str | None = Field(None, max_length=100, pattern=r"^[A-Za-z0-9._-]+$")
    paths: list[str] = Field(default_factory=list, max_length=20)


def anchor_values(repo: str | None, paths: list[str]) -> list[str]:
    """Anchor values a change can match: the repo, each path and each of its directory prefixes."""
    out: list[str] = [repo] if repo else []
    for path in paths:
        clean = path.strip().lstrip("./")
        if not clean or ".." in clean.split("/") or len(clean) > 200:
            continue
        out.append(clean)
        parts = clean.split("/")
        out.extend("/".join(parts[:i]) + "/" for i in range(1, len(parts)))
    return list(dict.fromkeys(out))


def add_routes(
    router: APIRouter, rt: Any, current: Current, respond: Callable[..., JSONResponse]
) -> None:
    async def _load(
        request: Request, key: str, valid: str | None, recorded: str | None, hops: int
    ) -> JSONResponse:
        who = await current(rt, request)
        if who is None:
            return respond(401, {"error": "unauthenticated"})
        if (blocked := await sync_barrier(rt.pool, rt.search.sync_wait_s)) is not None:
            return blocked
        try:
            v, r = parse_when(valid), parse_when(recorded)
        except ValueError:
            return respond(422, {"error": "invalid_request"})
        if not KEY.match(key):
            return respond(404, {"error": "not_found"})
        async with principal_transaction(rt.pool, who[1], acting_for=who.acting_for) as (
            conn,
            snap,
        ):
            view = await service.get_decision(conn, snap, key)
            if view is None:
                return respond(404, {"error": "not_found"})
            state = await service.state_as_of(conn, snap, key, v, r)
            events = await service.timeline(conn, snap, key, r) or []
            claims = await service.visible_claims(conn, snap, key)
            cites = await service.claim_citations(conn, snap, key)
            summary = await service.summary(conn, snap, key, valid=v, recorded=r)
            graph = await service.expand(conn, snap, key, hops, r)
        body: dict[str, Any] = {
            "decision": {
                "key": view.key,
                "title": view.title,
                "question": view.question,
                "outcome": view.outcome,
                "verification": view.verification,
                "origin": view.origin,
            },
            "as_of": {
                "valid": (v.isoformat() if v else "now"),
                "recorded": (r.isoformat() if r else "now"),
            },
            "status": state.status if state else "absent",
            "claims": [
                {
                    "key": c["key"],
                    "text": c["text"],
                    "tier": c["tier"],
                    "citations": cites.get(c["key"], []),
                }
                for c in claims
            ],
            "summary": (summary or {}).get("text"),
            "timeline": [
                {
                    "event": e["event"],
                    "valid_at": _iso(e["valid_at"]),
                    "recorded_at": _iso(e["recorded_at"]),
                    "by": e["by_key"],
                    "note": e["note"],
                    "source": e["source_ref"],
                }
                for e in events
            ],
            "links": [{"from": a, "to": b, "kind": k} for a, b, k in graph.edges],
        }
        record = build_audit_record(
            snap,
            "decision",
            f"{key} valid={valid or 'now'} recorded={recorded or 'now'}",
            body,
            key=rt.audit_key,
            returned_item_ids=[],
            outcome="results",
        )
        try:
            await write_audit(rt.pool, record)
        except Exception:
            log.error("audit.failed")
            return respond(500, {"error": "internal"})
        return respond(200, {**body, "audit_id": str(record.audit_id)})

    @router.post("/brief")
    async def pre_change_brief(request: Request) -> JSONResponse:
        """Decisions anchored to the repository or paths a change touches (plan 4.8)."""
        who = await current(rt, request)
        if who is None:
            return respond(401, {"error": "unauthenticated"})
        try:
            body = BriefBody.model_validate(await request.json())
        except (ValueError, TypeError):
            return respond(422, {"error": "invalid_request"})
        if (blocked := await sync_barrier(rt.pool, rt.search.sync_wait_s)) is not None:
            return blocked
        values = anchor_values(body.repo, body.paths)
        async with principal_transaction(
            rt.pool, who[1], acting_for=who.acting_for, read_only=True
        ) as (conn, snap):
            found = await service.decisions_for_anchors(conn, snap, values)
            briefs = []
            for key in sorted({r["key"] for r in found}):
                view = await service.get_decision(conn, snap, key)
                state = await service.state_as_of(conn, snap, key)
                events = await service.timeline(conn, snap, key) or []
                if view is None:
                    continue
                briefs.append(
                    {
                        "key": key,
                        "title": view.title,
                        "outcome": view.outcome,
                        "status": state.status if state else "absent",
                        "verification": view.verification,
                        "notes": [
                            {"event": e["event"], "at": _iso(e["valid_at"]), "note": e["note"]}
                            for e in events
                            if e["event"]
                            in ("partially_reverted", "amended", "superseded", "reverted")
                        ],
                    }
                )
        return respond(200, {"decisions": briefs, "anchors": values})

    @router.get("/decisions")
    async def decision_index(
        request: Request,
        q: str | None = Query(None, max_length=100),
        anchor: list[str] | None = Query(None, max_length=10),  # noqa: B008
    ) -> JSONResponse:
        who = await current(rt, request)
        if who is None:
            return respond(401, {"error": "unauthenticated"})
        if (blocked := await sync_barrier(rt.pool, rt.search.sync_wait_s)) is not None:
            return blocked
        if not q and not anchor:
            return respond(422, {"error": "invalid_request"})
        async with principal_transaction(
            rt.pool, who[1], acting_for=who.acting_for, read_only=True
        ) as (conn, snap):
            rows = await service.search_decisions(conn, snap, q) if q else []
            anchored = await service.decisions_for_anchors(conn, snap, anchor or [])
        seen: dict[str, dict[str, Any]] = {r["key"]: r for r in rows}
        for r in anchored:
            seen.setdefault(
                r["key"], {"key": r["key"], "title": None, "outcome": None, "verification": None}
            )
        return respond(
            200,
            {
                "decisions": [
                    {
                        "key": r["key"],
                        "title": r.get("title"),
                        "outcome": r.get("outcome"),
                        "verification": r.get("verification"),
                    }
                    for r in sorted(seen.values(), key=lambda r: r["key"])
                ]
            },
        )

    @router.get("/decisions/{key}")
    async def decision_page(
        key: str,
        request: Request,
        valid: str | None = Query(None),
        recorded: str | None = Query(None),
        hops: int = Query(1, ge=1, le=service.MAX_HOPS),
    ) -> JSONResponse:
        return await _load(request, key, valid, recorded, hops)

    @router.get("/decisions/{key}/timeline")
    async def decision_timeline(
        key: str, request: Request, recorded: str | None = Query(None)
    ) -> JSONResponse:
        page = await _load(request, key, None, recorded, 1)
        if page.status_code != 200:
            return page
        import json

        data = json.loads(bytes(page.body))
        return respond(
            200, {"key": key, "timeline": data["timeline"], "audit_id": data["audit_id"]}
        )
