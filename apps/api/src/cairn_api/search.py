"""Phase 1 routes: persona login, ``/me``, ``/search`` and ``/answer`` (design §17).

Every request body forbids extra fields; every 422 body is exactly ``{"error": "invalid_request"}``
so no input is echoed. Responses carry ``Cache-Control: no-store``. Model calls are awaited with
no database connection held. A search or answer is audited in a second short transaction before
the response is sent; if the audit insert fails the request returns 500 ``internal``: no answer
without an audit row.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final, NamedTuple

from fastapi import APIRouter, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from opentelemetry import trace
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel, ConfigDict, Field

from cairn_api.agent_tokens import verify_token
from cairn_api.answer_cache import AnswerCache, CachedAnswer
from cairn_api.ratelimit import FixedWindowLimiter
from cairn_api.sessions import COOKIE_NAME, sign, verify
from cairn_api.sync import (
    InboxWriter,
    ingest_inbox_writer,
    install_webhooks,
    sync_barrier,
    sync_summary,
)
from cairn_core.config import ApiSettings, IngestSettings, SearchSettings, load_settings
from cairn_core.db import RUNTIME_CONNECT_KWARGS, conninfo, make_async_pool
from cairn_retrieval.access import principal_transaction
from cairn_retrieval.answer import AnswerBody, citation_id, location_of, not_found
from cairn_retrieval.audit import AuditRecord, build_audit_record, write_audit
from cairn_retrieval.citations import CitationError, parse_citation
from cairn_retrieval.cloudflare.client import WorkersAiError
from cairn_retrieval.freshness import badges_for, my_audit, source_report, weakest
from cairn_retrieval.pipeline import search as run_search
from cairn_retrieval.recheck import still_visible
from cairn_retrieval.registry import AccessMode, run_registered
from cairn_retrieval.runtime import Providers
from cairn_retrieval.types import RetrievalConfig
from cairn_retrieval.workflow import WorkflowError, answer_workflow

log = logging.getLogger("cairn.api.search")

PERSONAS: Final = (("asha", "Asha"), ("ben", "Ben"), ("carla", "Carla"), ("priya", "Priya"))
CONNECTORS: Final = ("chat_export", "meeting_notes", "docs_folder")
SNIPPET_CHARS: Final = 300
NO_STORE: Final = {"Cache-Control": "no-store"}


@dataclass
class SearchRuntime:
    api: ApiSettings
    search: SearchSettings
    pool: AsyncConnectionPool[AsyncConnection[Any]]
    providers: Providers
    limiter: FixedWindowLimiter | None
    agent_limiter: FixedWindowLimiter | None = None
    inbox_writer: InboxWriter | None = None  # the local webhook receiver's store, when configured
    answer_cache: AnswerCache | None = None

    @property
    def secret(self) -> bytes:
        return self.search.session_secret.get_secret_value().encode()

    @property
    def audit_key(self) -> bytes:
        return self.search.audit_hash_key.get_secret_value().encode()

    @property
    def ttl_s(self) -> int:
        return 2 * 3600 if self.search.env == "demo" else 8 * 3600

    @property
    def retrieval(self) -> RetrievalConfig:
        s = self.search
        return RetrievalConfig(
            vector_strategy=s.vector_strategy,
            min_rerank_score=s.rerank_min_score,
        )


def build_runtime(
    api: ApiSettings,
    search: SearchSettings,
    providers: Providers,
    *,
    pool: AsyncConnectionPool[AsyncConnection[Any]] | None = None,
) -> SearchRuntime:
    the_pool = pool or make_async_pool(
        api,
        api.app_db_user,
        api.app_db_password.get_secret_value(),
        application_name="cairn-api",
        max_size=search.pool_max,
    )
    limiter = None
    if search.env == "demo" and search.dev_login_enabled:
        limiter = FixedWindowLimiter(search.audit_hash_key.get_secret_value().encode())
    return SearchRuntime(
        api,
        search,
        the_pool,
        providers,
        limiter,
        agent_limiter=FixedWindowLimiter(search.audit_hash_key.get_secret_value().encode()),
        inbox_writer=_local_inbox_writer(search),
        answer_cache=AnswerCache(search.answer_cache_size, search.answer_cache_ttl_s)
        if search.answer_cache_size > 0
        else None,
    )


def _local_inbox_writer(search: SearchSettings) -> InboxWriter | None:
    """The local receiver stores through a short-lived ``cairn_ingest`` connection, or is off."""
    if search.github_webhook_secret is None:
        return None
    try:
        ingest = load_settings(IngestSettings)
    except Exception:
        log.warning("webhook.local_receiver_off", extra={"reason": "no_ingest_login"})
        return None

    async def connect() -> AsyncConnection[Any]:
        return await AsyncConnection.connect(
            conninfo(
                ingest,
                ingest.ingest_db_user,
                ingest.ingest_db_password.get_secret_value(),
                application_name="cairn-webhook",
                dbname=None,
            ),
            **RUNTIME_CONNECT_KWARGS,
        )

    return ingest_inbox_writer(connect)


@asynccontextmanager
async def runtime_lifespan(runtime: SearchRuntime) -> AsyncIterator[None]:
    await runtime.pool.open()
    if runtime.search.github_org and runtime.inbox_writer is not None:
        # Drain-before-serve: queue a permission poll before the first request can arrive, so the
        # barrier is closed until the jobs runner has re-read GitHub's access state (plan 3.28).
        try:
            await runtime.inbox_writer(
                (
                    f"poll-boot-{uuid.uuid4().hex[:12]}",
                    "poll",
                    "boot",
                    "acl",
                    0,
                    None,
                    None,
                    None,
                    None,
                    None,
                )
            )
        except Exception:
            log.error("sync.boot_poll_not_queued")
    try:
        yield
    finally:
        await runtime.pool.close()
        await runtime.providers.aclose()


# --- request and response models ---------------------------------------------------------


class LoginBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    persona: str = Field(min_length=1, max_length=16)


class SearchBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=1, max_length=300)
    limit: int = Field(10, ge=1, le=20)
    recorded: str | None = Field(None, max_length=40)  # "as Cairn knew it on": a date or timestamp


class AnswerBodyIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question: str = Field(min_length=1, max_length=500)
    recorded: str | None = Field(None, max_length=40)


def _recorded_before(value: str | None) -> tuple[datetime | None, bool]:
    """``(instant, ok)``: ``None`` means now (the normal path); not ok means the value is malformed."""  # noqa: E501
    from cairn_api.decisions import parse_when
    from cairn_retrieval.temporal import as_of_instant

    try:
        when = parse_when(value)
    except ValueError:
        return None, False
    return (None if when is None else as_of_instant(when)), True


_tracer = trace.get_tracer("cairn.api.workflow")


def _step_span(name: str) -> Any:
    """One trace span per workflow step (plan 4.1)."""
    return _tracer.start_as_current_span(f"workflow.{name}")


def _json(status: int, body: dict[str, Any], headers: dict[str, str] | None = None) -> JSONResponse:
    return JSONResponse(body, status_code=status, headers={**NO_STORE, **(headers or {})})


def _seconds_until_midnight_utc() -> int:
    now = datetime.now(UTC)
    nxt = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return max(1, int((nxt - now).total_seconds()))


def _limited(
    rt: SearchRuntime, request: Request, bucket: str, limit: int, window_s: int
) -> JSONResponse | None:
    if rt.limiter is None:
        return None
    client = rt.limiter.client_key(request.headers.get("x-forwarded-for"))
    d = rt.limiter.check(client, bucket, limit, window_s)
    if d.allowed:
        return None
    return _json(429, {"error": "rate_limited"}, {"Retry-After": str(d.retry_after_s)})


async def _principal_for(rt: SearchRuntime, persona: str) -> tuple[int, str] | None:
    async with rt.pool.connection() as conn:
        row = await (
            await conn.execute(
                "SELECT principal_id, display_name FROM cairn.principals WHERE external_id = %s",
                (f"user:{persona}",),
            )
        ).fetchone()
        await conn.rollback()
    return (int(row[0]), str(row[1])) if row else None


class Actor(NamedTuple):
    """Who is asking. ``principal_id`` is the caller (an agent for MCP); ``acting_for`` is the user
    whose access applies, set only for agents. Indexing keeps the old ``(persona, id, name)`` shape."""  # noqa: E501

    persona: str
    principal_id: int
    display_name: str
    acting_for: int | None = None

    @property
    def is_agent(self) -> bool:
        return self.acting_for is not None


async def _agent_actor(rt: SearchRuntime, request: Request) -> Actor | None:
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        return None
    name = verify_token(header[7:].strip(), rt.secret)
    if name is None or not name.replace("-", "").replace("_", "").isalnum():
        return None
    async with rt.pool.connection() as conn:
        row = await (
            await conn.execute(
                "SELECT principal_id, display_name, acting_for FROM cairn.principals "
                "WHERE external_id = %s AND kind = 'agent'",
                (f"agent:{name}",),
            )
        ).fetchone()
    if row is None or row[2] is None:
        return None
    if (
        rt.agent_limiter is not None
        and not rt.agent_limiter.check_daily(f"agent:{name}", rt.search.agent_calls_per_day).allowed
    ):
        return None  # a per-agent daily cap, so one token cannot drain the shared neuron budget
    return Actor(f"agent:{name}", int(row[0]), str(row[1]), int(row[2]))


async def _current(rt: SearchRuntime, request: Request) -> Actor | None:
    if request.headers.get("authorization"):
        # A bearer token is the only credential considered when one is sent: never fall back to a
        # cookie, so an agent's access cannot silently become a user's.
        return await _agent_actor(rt, request)
    persona = verify(request.cookies.get(COOKIE_NAME), rt.secret)
    if persona is None or persona not in {p for p, _ in PERSONAS}:
        return None
    found = await _principal_for(rt, persona)
    return Actor(persona, found[0], found[1]) if found else None


async def _untrusted_ids(rt: SearchRuntime, actor: Actor, ids: list[int]) -> set[int]:
    """Items from public GitHub issues and pull requests: text anyone on the internet can write."""
    if not ids:
        return set()
    async with principal_transaction(
        rt.pool, actor.principal_id, acting_for=actor.acting_for, read_only=True
    ) as (conn, snap):
        rows = await run_registered(conn, "untrusted_items", {"ids": sorted(set(ids))}, snap)
    return {int(r["item_id"]) for r in rows}


def _result_row(rank: int, c: Any) -> dict[str, Any]:
    return {
        "citation": citation_id(c),
        "title": c.title,
        "source_kind": c.source_kind,
        "location": location_of(c.external_id),
        "snippet": c.text[:SNIPPET_CHARS],
        "rank": rank,
    }


def install(app: FastAPI, rt: SearchRuntime) -> None:
    """Add the Phase 1 routes and the 422 handler to ``app``."""
    router = APIRouter()

    @app.exception_handler(RequestValidationError)
    async def _invalid(_: Request, __: RequestValidationError) -> JSONResponse:
        return _json(422, {"error": "invalid_request"})

    @router.get("/auth/personas")
    async def personas() -> JSONResponse:
        if not rt.search.dev_login_enabled:
            return _json(404, {"error": "not_found"})
        return _json(
            200,
            {
                "demo": rt.search.env == "demo",
                "personas": [{"persona": p, "display_name": n} for p, n in PERSONAS],
            },
        )

    @router.post("/auth/dev-login")
    async def dev_login(body: LoginBody, request: Request) -> Response:
        if not rt.search.dev_login_enabled:
            return _json(404, {"error": "not_found"})
        if (limited := _limited(rt, request, "login", rt.search.login_per_min, 60)) is not None:
            return limited
        if (
            body.persona not in {p for p, _ in PERSONAS}
            or await _principal_for(rt, body.persona) is None
        ):
            return _json(422, {"error": "invalid_request"})
        response = Response(status_code=204, headers=NO_STORE)
        response.set_cookie(
            COOKIE_NAME,
            sign(body.persona, rt.secret, rt.ttl_s),
            max_age=rt.ttl_s,
            httponly=True,
            samesite="strict",
            secure=rt.search.env == "demo",
            path="/",
        )
        return response

    @router.post("/auth/logout")
    async def logout() -> Response:
        response = Response(status_code=204, headers=NO_STORE)
        response.delete_cookie(COOKIE_NAME, path="/")
        return response

    @router.get("/me")
    async def me(request: Request) -> JSONResponse:
        current = await _current(rt, request)
        if current is None:
            return _json(401, {"error": "unauthenticated"})
        return _json(200, {"persona": current[0], "display_name": current[2]})

    @router.post("/search")
    async def search_route(body: SearchBody, request: Request) -> JSONResponse:
        current = await _current(rt, request)
        if current is None:
            return _json(401, {"error": "unauthenticated"})
        if (blocked := await sync_barrier(rt.pool, rt.search.sync_wait_s)) is not None:
            return blocked
        if (
            limited := _limited(rt, request, "search", rt.search.search_per_10min, 600)
        ) is not None:
            return limited
        cfg = RetrievalConfig(**{**rt.retrieval.__dict__, "limit": body.limit})
        before, ok = _recorded_before(body.recorded)
        if not ok:
            return _json(422, {"error": "invalid_request"})
        try:
            result = await run_search(
                rt.pool,
                rt.providers.embedder,
                rt.providers.reranker,
                principal_id=current[1],
                acting_for=current.acting_for,
                query=body.query,
                cfg=cfg,
                mode=AccessMode(rt.search.access_mode),
                recorded_before=before,
            )
        except WorkersAiError as exc:
            log.warning("search.models_unavailable", extra={"error_code": exc.code})
            return _json(503, {"error": "models_unavailable"})
        untrusted = await _untrusted_ids(rt, current, [c.item_id for c in result.ranked])
        results = [
            {**_result_row(i, c), "untrusted": c.item_id in untrusted}
            for i, c in enumerate(result.ranked, start=1)
        ]
        payload = {
            "results": results,
            "searched": list(CONNECTORS),
            "degraded": list(result.degraded),
        }
        record = build_audit_record(
            result.snapshot,
            "search",
            body.query,
            payload,
            key=rt.audit_key,
            returned_item_ids=[c.item_id for c in result.ranked],
            outcome="results" if results else "no_results",
        )
        try:
            await write_audit(rt.pool, record)
        except Exception:
            log.error("audit.failed")
            return _json(500, {"error": "internal"})
        return _json(200, {**payload, "audit_id": str(record.audit_id)})

    @router.post("/answer")
    async def answer_route(body: AnswerBodyIn, request: Request) -> JSONResponse:
        current = await _current(rt, request)
        if current is None:
            return _json(401, {"error": "unauthenticated"})
        if (blocked := await sync_barrier(rt.pool, rt.search.sync_wait_s)) is not None:
            return blocked
        if (
            limited := _limited(rt, request, "answer", rt.search.answer_per_10min, 600)
        ) is not None:
            return limited
        if rt.limiter is not None:
            day = rt.limiter.check_daily("answers", rt.search.answers_per_day)
            if not day.allowed:
                return _json(
                    429, {"error": "rate_limited"}, {"Retry-After": str(day.retry_after_s)}
                )
        before, ok = _recorded_before(body.recorded)
        if not ok:
            return _json(422, {"error": "invalid_request"})
        cache_key: str | None = None
        if rt.answer_cache is not None and before is None:
            # L7: the key holds the principal set and the ACL version, so an answer cached for one
            # principal set is never served to another and any sync or purge invalidates it.
            async with principal_transaction(
                rt.pool, current[1], acting_for=current.acting_for, read_only=True
            ) as (_, snap):
                cache_key = AnswerCache.key(body.question, snap.groups, snap.acl_version)
            hit = rt.answer_cache.get(cache_key)
            if hit is not None:
                hit_record = build_audit_record(
                    snap,
                    "answer",
                    body.question,
                    hit.body,
                    key=rt.audit_key,
                    returned_item_ids=list(hit.returned_item_ids),
                    outcome="answered",
                    model=hit.model,
                )
                try:
                    await write_audit(rt.pool, hit_record)
                except Exception:
                    log.error("audit.failed")
                    return _json(500, {"error": "internal"})
                return _json(
                    200, {**hit.body, "audit_id": str(hit_record.audit_id), "cached": True}
                )
        try:
            flow = await answer_workflow(
                rt.pool,
                rt.providers.embedder,
                rt.providers.reranker,
                rt.providers.answer,
                principal_id=current[1],
                acting_for=current.acting_for,
                question=body.question,
                cfg=rt.retrieval,
                mode=AccessMode(rt.search.access_mode),
                searched=CONNECTORS,
                max_tokens=rt.search.max_output_tokens,
                recorded_before=before,
                observer=_step_span,
            )
        except WorkflowError as exc:
            if exc.step == "retrieve":
                log.warning("answer.models_unavailable", extra={"error_code": exc.cause.code})
                return _json(503, {"error": "models_unavailable"})
            if exc.cause.code in ("budget_exhausted", "daily_limit"):
                return _json(
                    503,
                    {"error": "model_budget_exhausted"},
                    {"Retry-After": str(_seconds_until_midnight_utc())},
                )
            log.warning("answer.llm_unavailable", extra={"error_code": exc.cause.code})
            return _json(502, {"error": "llm_unavailable"})
        result, packed, answer = flow.search, flow.packed, flow.answer
        if not answer.abstained and not await still_visible(
            rt.pool, current[1], [c.item_id for c in packed], acting_for=current.acting_for
        ):
            # L4: access changed while the answer was being composed. Withhold it.
            log.warning("answer.withheld_after_recheck")
            answer = not_found(CONNECTORS, result.degraded)
        body_json = answer.to_json()
        if not answer.abstained:
            by_source = await badges_for(
                rt.pool, current[1], [c.item_id for c in packed], acting_for=current.acting_for
            )
            body_json["freshness"] = {
                "weakest_badge": weakest(by_source.values()),
                "sources": dict(sorted(by_source.items())),
            }
        record: AuditRecord = build_audit_record(
            result.snapshot,
            "answer",
            body.question,
            body_json,
            key=rt.audit_key,
            returned_item_ids=[c.item_id for c in packed] if not answer.abstained else [],
            outcome="not_found" if answer.abstained else "answered",
            model=f"{rt.providers.answer.provider}:{rt.providers.answer.model}",
        )
        try:
            await write_audit(rt.pool, record)
        except Exception:
            log.error("audit.failed")
            return _json(500, {"error": "internal"})
        if cache_key is not None and rt.answer_cache is not None and not answer.abstained:
            rt.answer_cache.put(
                cache_key,
                CachedAnswer(
                    body_json,
                    tuple(c.item_id for c in packed),
                    f"{rt.providers.answer.provider}:{rt.providers.answer.model}",
                ),
            )
        return _json(200, {**body_json, "audit_id": str(record.audit_id)})

    @router.get("/evidence")
    async def evidence(request: Request, citation: str = "") -> JSONResponse:
        """The text a citation points at, if the caller can read it (plan 4.8)."""
        current = await _current(rt, request)
        if current is None:
            return _json(401, {"error": "unauthenticated"})
        try:
            parsed = parse_citation(citation)
        except CitationError:
            return _json(422, {"error": "invalid_request"})
        if (blocked := await sync_barrier(rt.pool, rt.search.sync_wait_s)) is not None:
            return blocked
        async with principal_transaction(
            rt.pool, current[1], acting_for=current.acting_for, read_only=True
        ) as (conn, snap):
            rows = await run_registered(
                conn,
                "resolve_citation",
                {
                    "external_id": parsed.external_id,
                    "hash8": parsed.hash8,
                    "start": parsed.start,
                    "len": parsed.end - parsed.start,
                },
                snap,
            )
        if not rows:
            return _json(404, {"error": "not_found"})  # missing and unreadable look the same
        r = rows[0]
        item_ids = [r["item_id"]] if "item_id" in r else []
        untrusted = await _untrusted_ids(rt, current, item_ids) if item_ids else set()
        return _json(
            200,
            {
                "citation": citation,
                "title": r["title"],
                "source_kind": r["source_kind"],
                "location": location_of(r["external_id"]),
                "quote": r["quote"][:2000],
                "untrusted": bool(untrusted),
            },
        )

    @router.get("/sync/sources")
    async def sync_sources(request: Request) -> JSONResponse:
        current = await _current(rt, request)
        if current is None:
            return _json(401, {"error": "unauthenticated"})
        return _json(
            200,
            {
                "sources": await source_report(rt.pool, current[1], acting_for=current.acting_for),
                **await sync_summary(rt.pool),
            },
        )

    @router.get("/audit/mine")
    async def audit_mine(request: Request) -> JSONResponse:
        current = await _current(rt, request)
        if current is None:
            return _json(401, {"error": "unauthenticated"})
        return _json(
            200, {"entries": await my_audit(rt.pool, current[1], acting_for=current.acting_for)}
        )

    @router.get("/sync/health")
    async def sync_health(request: Request) -> JSONResponse:
        if await _current(rt, request) is None:
            return _json(401, {"error": "unauthenticated"})
        return _json(200, await sync_summary(rt.pool))

    install_webhooks(
        app,
        rt.search.github_webhook_secret.get_secret_value().encode()
        if rt.search.github_webhook_secret
        else None,
        rt.inbox_writer,
    )

    from cairn_api.decisions import add_routes as add_decision_routes

    add_decision_routes(router, rt, _current, _json)
    from cairn_api.reviews import add_routes as add_review_routes

    add_review_routes(router, rt, _current, _json)
    app.include_router(router)


__all__ = [
    "AnswerBody",
    "SearchRuntime",
    "build_runtime",
    "install",
    "not_found",
    "runtime_lifespan",
]
