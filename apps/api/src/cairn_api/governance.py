"""Approvals, decisions proposed in Cairn, policies with attestations, and risks (SaaS plan, Phase B)."""

from __future__ import annotations

import datetime as dt
import re
from typing import Any

import psycopg
from fastapi import APIRouter, FastAPI, Request, Response
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field

from cairn_api.apihelp import error, json_response
from cairn_api.people import _db_rule, guard
from cairn_api.tenant import Ctx, record, scope_of

LEADS = ("owner", "admin", "manager")
READERS = ("owner", "admin", "manager", "member", "auditor")


class DecisionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=3, max_length=160)
    question: str = Field("", max_length=500)
    outcome: str = Field(min_length=3, max_length=1000)
    required: int = Field(1, ge=1, le=5)


class NoteBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    note: str = Field("", max_length=300)


class VoteBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    vote: str = Field(pattern="^(approve|reject)$")
    comment: str = Field("", max_length=500)


class RequestBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: str = Field(pattern="^(access|exception)$")
    title: str = Field(min_length=3, max_length=160)
    role: str | None = Field(None, pattern="^(manager|member|auditor)$")
    reason: str = Field("", max_length=1000)
    expires_at: dt.date | None = None
    required: int = Field(1, ge=1, le=3)


class PolicyBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=3, max_length=160)
    body: str = Field(min_length=10, max_length=30000)
    summary: str = Field("", max_length=500)


class NewPolicyBody(PolicyBody):
    slug: str | None = Field(None, max_length=60)
    review_days: int = Field(365, ge=30, le=1095)
    attest_days: int = Field(30, ge=1, le=180)


class RiskBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=3, max_length=160)
    description: str = Field("", max_length=2000)
    category: str = Field(
        "operational", pattern="^(security|operational|compliance|financial|people|vendor)$"
    )
    likelihood: int = Field(ge=1, le=5)
    impact: int = Field(ge=1, le=5)
    mitigation: str = Field("", max_length=2000)
    review_by: dt.date | None = None


class RiskPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    state: str | None = Field(None, pattern="^(open|mitigating|accepted|closed)$")
    likelihood: int | None = Field(None, ge=1, le=5)
    impact: int | None = Field(None, ge=1, le=5)
    mitigation: str | None = Field(None, max_length=2000)
    review_by: dt.date | None = None


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60].strip("-") or "policy"


def approval_row(r: Any, ctx: Ctx) -> dict[str, Any]:
    pending = r[6] == "pending"
    can_vote = (
        pending
        and r[4] != ctx.principal_id
        and not r[11]
        and (ctx.role in ("owner", "admin") or (ctx.role == "manager" and r[1] != "access"))
    )
    return {
        "approval_id": r[0],
        "kind": r[1],
        "title": r[2],
        "detail": r[3],
        "requested_by": r[10],
        "mine": r[4] == ctx.principal_id,
        "required": r[5],
        "state": r[6],
        "approvals": r[7],
        "created_at": r[8].isoformat(),
        "decided_at": r[9].isoformat() if r[9] else None,
        "can_vote": bool(can_vote),
        "voted": bool(r[11]),
    }


APPROVAL_SQL = (
    "SELECT a.approval_id, a.kind, a.title, a.detail, a.requested_by, a.required, a.state, "
    "(SELECT count(*) FROM cairn.approval_votes v WHERE v.approval_id = a.approval_id AND v.vote = 'approve'), "
    "a.created_at, a.decided_at, p.display_name, "
    "EXISTS (SELECT 1 FROM cairn.approval_votes v WHERE v.approval_id = a.approval_id AND v.voter_principal = %s) "
    "FROM cairn.approvals a JOIN cairn.principals p ON p.principal_id = a.requested_by "
)


def install(app: FastAPI, rt: Any) -> None:
    router = APIRouter()

    # --- approvals -----------------------------------------------------------------------

    @router.get("/governance/approvals")
    async def approvals(request: Request, state: str = "pending") -> Response:
        ctx, bad = await guard(rt, request, "approval.list")
        if bad is not None or ctx is None:
            return bad or error(401, "unauthenticated")
        if state not in ("pending", "all"):
            return error(422, "invalid_request")
        async with scope_of(rt.pool, ctx) as conn:
            rows = await (
                await conn.execute(
                    APPROVAL_SQL
                    + "WHERE (%s = 'all' OR a.state = 'pending') ORDER BY a.created_at DESC LIMIT 100",
                    (ctx.principal_id, state),
                )
            ).fetchall()
        return json_response(200, {"approvals": [approval_row(r, ctx) for r in rows]})

    @router.get("/governance/approvals/{approval_id}")
    async def approval(approval_id: int, request: Request) -> Response:
        ctx, bad = await guard(rt, request, "approval.read")
        if bad is not None or ctx is None:
            return bad or error(401, "unauthenticated")
        async with scope_of(rt.pool, ctx) as conn:
            row = await (
                await conn.execute(
                    APPROVAL_SQL + "WHERE a.approval_id = %s", (ctx.principal_id, approval_id)
                )
            ).fetchone()
            if row is None:
                return error(404, "not_found")
            votes = await (
                await conn.execute(
                    "SELECT p.display_name, v.vote, v.comment, v.voted_at FROM cairn.approval_votes v "
                    "JOIN cairn.principals p ON p.principal_id = v.voter_principal "
                    "WHERE v.approval_id = %s ORDER BY v.vote_id",
                    (approval_id,),
                )
            ).fetchall()
        return json_response(
            200,
            {
                "approval": approval_row(row, ctx),
                "votes": [
                    {"voter": v[0], "vote": v[1], "comment": v[2], "at": v[3].isoformat()}
                    for v in votes
                ],
            },
        )

    @router.post("/governance/approvals/{approval_id}/vote")
    async def vote(approval_id: int, body: VoteBody, request: Request) -> Response:
        ctx, bad = await guard(rt, request, "approval.vote", LEADS)
        if bad is not None or ctx is None:
            return bad or error(401, "unauthenticated")
        try:
            async with scope_of(rt.pool, ctx) as conn:
                row = await (
                    await conn.execute(
                        "SELECT cairn.cast_vote(%s, %s, %s)", (approval_id, body.vote, body.comment)
                    )
                ).fetchone()
                assert row is not None  # noqa: S101
                await record(
                    conn,
                    f"approval.{body.vote}d" if body.vote == "approve" else "approval.rejected",
                    "approval",
                    str(approval_id),
                    {"result": row[0]},
                )
        except psycopg.errors.InsufficientPrivilege as exc:
            async with scope_of(rt.pool, ctx) as conn:
                await record(conn, "approval.vote", "approval", str(approval_id), {}, "denied")
            return json_response(403, {"error": "forbidden", "reason": _db_rule(exc)})
        except (psycopg.errors.RaiseException, psycopg.errors.UniqueViolation) as exc:
            return json_response(409, {"error": "refused", "reason": _db_rule(exc)})
        return json_response(200, {"state": row[0]})

    @router.post("/governance/approvals/{approval_id}/withdraw")
    async def withdraw(approval_id: int, request: Request) -> Response:
        ctx, bad = await guard(rt, request, "approval.withdraw")
        if bad is not None or ctx is None:
            return bad or error(401, "unauthenticated")
        try:
            async with scope_of(rt.pool, ctx) as conn:
                row = await (
                    await conn.execute("SELECT cairn.withdraw_approval(%s)", (approval_id,))
                ).fetchone()
                if not (row and row[0]):
                    return error(409, "not_pending")
                await record(conn, "approval.withdrawn", "approval", str(approval_id))
        except psycopg.errors.InsufficientPrivilege:
            return error(403, "forbidden")
        return json_response(200, {"state": "withdrawn"})

    @router.post("/governance/approvals")
    async def request_approval(body: RequestBody, request: Request) -> Response:
        ctx, bad = await guard(rt, request, "approval.request")
        if bad is not None or ctx is None:
            return bad or error(401, "unauthenticated")
        if body.kind == "access" and body.role is None:
            return error(422, "invalid_request")
        detail: dict[str, Any] = {"reason": body.reason}
        if body.role:
            detail["role"] = body.role
        expires = (
            dt.datetime.combine(body.expires_at, dt.time(23, 59), dt.UTC)
            if body.expires_at
            else None
        )
        try:
            async with scope_of(rt.pool, ctx) as conn:
                row = await (
                    await conn.execute(
                        "SELECT cairn.request_approval(%s, %s, %s, %s, NULL, NULL, %s)",
                        (
                            body.kind,
                            body.title,
                            Jsonb(detail),
                            1 if body.kind == "access" else body.required,
                            expires,
                        ),
                    )
                ).fetchone()
                assert row is not None  # noqa: S101
                await record(
                    conn, "approval.requested", "approval", str(row[0]), {"kind": body.kind}
                )
        except psycopg.errors.InsufficientPrivilege:
            return error(403, "forbidden")
        except psycopg.errors.RaiseException as exc:
            return json_response(409, {"error": "refused", "reason": _db_rule(exc)})
        return json_response(201, {"approval_id": row[0]})

    # --- decisions -----------------------------------------------------------------------

    @router.get("/governance/decisions")
    async def decisions(request: Request) -> Response:
        ctx, bad = await guard(rt, request, "decision.list", READERS)
        if bad is not None or ctx is None:
            return bad or error(401, "unauthenticated")
        async with scope_of(rt.pool, ctx) as conn:
            rows = await (
                await conn.execute("SELECT * FROM cairn.list_manual_decisions()")
            ).fetchall()
        return json_response(
            200,
            {
                "decisions": [
                    {
                        "decision_id": r[0],
                        "key": r[1],
                        "title": r[2],
                        "question": r[3],
                        "outcome": r[4],
                        "status": r[5],
                        "created_at": r[6].isoformat(),
                        "proposed_by": r[7],
                        "approval_id": r[8],
                        "approval_state": r[9],
                        "required": r[10],
                        "approvals": r[11],
                    }
                    for r in rows
                ]
            },
        )

    @router.post("/governance/decisions")
    async def propose(body: DecisionBody, request: Request) -> Response:
        ctx, bad = await guard(
            rt, request, "decision.propose", ("owner", "admin", "manager", "member")
        )
        if bad is not None or ctx is None:
            return bad or error(401, "unauthenticated")
        try:
            async with scope_of(rt.pool, ctx) as conn:
                row = await (
                    await conn.execute(
                        "SELECT * FROM cairn.propose_decision(%s, %s, %s, %s)",
                        (body.title, body.question, body.outcome, body.required),
                    )
                ).fetchone()
                assert row is not None  # noqa: S101
                await record(
                    conn,
                    "decision.proposed",
                    "decision",
                    row[1],
                    {"approval": row[2], "required": body.required},
                )
        except psycopg.errors.RaiseException as exc:
            return json_response(409, {"error": "refused", "reason": _db_rule(exc)})
        return json_response(201, {"decision_id": row[0], "key": row[1], "approval_id": row[2]})

    @router.post("/governance/decisions/{decision_id}/implemented")
    async def implemented(decision_id: int, body: NoteBody, request: Request) -> Response:
        ctx, bad = await guard(rt, request, "decision.implement", LEADS)
        if bad is not None or ctx is None:
            return bad or error(401, "unauthenticated")
        try:
            async with scope_of(rt.pool, ctx) as conn:
                await conn.execute(
                    "SELECT cairn.mark_decision_implemented(%s, %s)", (decision_id, body.note)
                )
                await record(conn, "decision.implemented", "decision", str(decision_id))
        except psycopg.errors.RaiseException as exc:
            return json_response(409, {"error": "refused", "reason": _db_rule(exc)})
        return json_response(200, {"status": "implemented"})

    # --- policies ------------------------------------------------------------------------

    @router.get("/governance/policies")
    async def policies(request: Request) -> Response:
        ctx, bad = await guard(rt, request, "policy.list", READERS)
        if bad is not None or ctx is None:
            return bad or error(401, "unauthenticated")
        async with scope_of(rt.pool, ctx) as conn:
            people = await (
                await conn.execute(
                    "SELECT count(*) FROM cairn_meta.directory() WHERE state = 'active' "
                    "AND role IN ('owner', 'admin', 'manager', 'member')"
                )
            ).fetchone()
            rows = await (
                await conn.execute(
                    "SELECT p.policy_id, p.slug, p.title, p.state, p.current_version, p.review_days, "
                    "p.attest_days, pv.published_at, pv.attest_by, "
                    "(SELECT count(*) FROM cairn.attestations a WHERE a.version_id = pv.version_id), "
                    "EXISTS (SELECT 1 FROM cairn.attestations a WHERE a.version_id = pv.version_id "
                    "AND a.principal_id = %s), "
                    "(SELECT v.state FROM cairn.policy_versions v WHERE v.policy_id = p.policy_id "
                    "ORDER BY v.version DESC LIMIT 1) "
                    "FROM cairn.policies p LEFT JOIN cairn.policy_versions pv "
                    "ON pv.policy_id = p.policy_id AND pv.state = 'published' ORDER BY p.title",
                    (ctx.principal_id,),
                )
            ).fetchall()
        expected = int(people[0]) if people else 0
        now = dt.datetime.now(dt.UTC)
        out = []
        for r in rows:
            published, attest_by = r[7], r[8]
            review_due = (published + dt.timedelta(days=r[5])) if published else None
            out.append(
                {
                    "policy_id": r[0],
                    "slug": r[1],
                    "title": r[2],
                    "state": r[3],
                    "version": r[4],
                    "published_at": published.isoformat() if published else None,
                    "attest_by": attest_by.isoformat() if attest_by else None,
                    "review_due": review_due.date().isoformat() if review_due else None,
                    "review_overdue": bool(review_due and review_due < now),
                    "attested": int(r[9]) if ctx.role in LEADS + ("auditor",) else None,
                    "expected": expected,
                    "mine": bool(r[10]),
                    "needs_my_attestation": bool(
                        r[3] == "active"
                        and not r[10]
                        and ctx.role in ("owner", "admin", "manager", "member")
                    ),
                    "overdue": bool(attest_by and attest_by < now and (int(r[9]) < expected)),
                    "latest_version_state": r[11],
                }
            )
        return json_response(200, {"policies": out})

    @router.get("/governance/policies/{policy_id}")
    async def policy(policy_id: int, request: Request) -> Response:
        ctx, bad = await guard(rt, request, "policy.read", READERS)
        if bad is not None or ctx is None:
            return bad or error(401, "unauthenticated")
        async with scope_of(rt.pool, ctx) as conn:
            head = await (
                await conn.execute(
                    "SELECT policy_id, slug, title, state, current_version, owner_principal FROM cairn.policies WHERE policy_id = %s",
                    (policy_id,),
                )
            ).fetchone()
            if head is None:
                return error(404, "not_found")
            versions = await (
                await conn.execute(
                    "SELECT v.version, v.title, v.body, v.summary, v.state, v.published_at, v.attest_by, "
                    "p.display_name, v.approval_id FROM cairn.policy_versions v "
                    "JOIN cairn.principals p ON p.principal_id = v.created_by "
                    "WHERE v.policy_id = %s ORDER BY v.version DESC",
                    (policy_id,),
                )
            ).fetchall()
            attestations: list[Any] = []
            missing: list[str] = []
            if ctx.role in LEADS + ("auditor",):
                attestations = await (
                    await conn.execute(
                        "SELECT p.display_name, a.attested_at FROM cairn.attestations a "
                        "JOIN cairn.policy_versions v ON v.version_id = a.version_id AND v.state = 'published' "
                        "JOIN cairn.principals p ON p.principal_id = a.principal_id "
                        "WHERE v.policy_id = %s ORDER BY a.attested_at",
                        (policy_id,),
                    )
                ).fetchall()
                rows = await (
                    await conn.execute(
                        "SELECT display_name FROM cairn_meta.directory() d WHERE d.state = 'active' "
                        "AND d.role IN ('owner', 'admin', 'manager', 'member') AND d.principal_id NOT IN ("
                        "SELECT a.principal_id FROM cairn.attestations a JOIN cairn.policy_versions v "
                        "ON v.version_id = a.version_id AND v.state = 'published' WHERE v.policy_id = %s) "
                        "ORDER BY display_name",
                        (policy_id,),
                    )
                ).fetchall()
                missing = [r[0] for r in rows]
        return json_response(
            200,
            {
                "policy": {
                    "policy_id": head[0],
                    "slug": head[1],
                    "title": head[2],
                    "state": head[3],
                    "version": head[4],
                },
                "can_edit": ctx.role in ("owner", "admin"),
                "versions": [
                    {
                        "version": v[0],
                        "title": v[1],
                        "body": v[2],
                        "summary": v[3],
                        "state": v[4],
                        "published_at": v[5].isoformat() if v[5] else None,
                        "attest_by": v[6].isoformat() if v[6] else None,
                        "author": v[7],
                        "approval_id": v[8],
                    }
                    for v in versions
                ],
                "attested": [{"name": a[0], "at": a[1].isoformat()} for a in attestations],
                "missing": missing,
            },
        )

    @router.post("/governance/policies")
    async def create_policy(body: NewPolicyBody, request: Request) -> Response:
        ctx, bad = await guard(rt, request, "policy.create", ("owner", "admin"))
        if bad is not None or ctx is None:
            return bad or error(401, "unauthenticated")
        slug = body.slug or slugify(body.title)
        try:
            async with scope_of(rt.pool, ctx) as conn:
                row = await (
                    await conn.execute(
                        "SELECT cairn.create_policy(%s, %s, %s, %s, %s)",
                        (slug, body.title, body.body, body.review_days, body.attest_days),
                    )
                ).fetchone()
                assert row is not None  # noqa: S101
                await record(conn, "policy.created", "policy", str(row[0]), {"slug": slug})
        except psycopg.errors.UniqueViolation:
            return error(409, "policy_exists")
        except psycopg.errors.CheckViolation:
            return error(422, "invalid_request")
        return json_response(201, {"policy_id": row[0], "slug": slug})

    @router.put("/governance/policies/{policy_id}/draft")
    async def save_draft(policy_id: int, body: PolicyBody, request: Request) -> Response:
        ctx, bad = await guard(rt, request, "policy.edit", ("owner", "admin"))
        if bad is not None or ctx is None:
            return bad or error(401, "unauthenticated")
        try:
            async with scope_of(rt.pool, ctx) as conn:
                row = await (
                    await conn.execute(
                        "SELECT cairn.save_policy_draft(%s, %s, %s, %s)",
                        (policy_id, body.title, body.body, body.summary),
                    )
                ).fetchone()
                assert row is not None  # noqa: S101
                await record(
                    conn, "policy.draft_saved", "policy", str(policy_id), {"version": row[0]}
                )
        except psycopg.errors.RaiseException as exc:
            return json_response(409, {"error": "refused", "reason": _db_rule(exc)})
        return json_response(200, {"version": row[0]})

    @router.post("/governance/policies/{policy_id}/submit")
    async def submit(policy_id: int, request: Request) -> Response:
        ctx, bad = await guard(rt, request, "policy.submit", ("owner", "admin"))
        if bad is not None or ctx is None:
            return bad or error(401, "unauthenticated")
        try:
            async with scope_of(rt.pool, ctx) as conn:
                row = await (
                    await conn.execute("SELECT cairn.submit_policy(%s)", (policy_id,))
                ).fetchone()
                assert row is not None  # noqa: S101
                await record(
                    conn, "policy.submitted", "policy", str(policy_id), {"approval": row[0]}
                )
        except psycopg.errors.RaiseException as exc:
            return json_response(409, {"error": "refused", "reason": _db_rule(exc)})
        return json_response(200, {"approval_id": row[0]})

    @router.post("/governance/policies/{policy_id}/attest")
    async def attest(policy_id: int, request: Request) -> Response:
        ctx, bad = await guard(
            rt, request, "policy.attest", ("owner", "admin", "manager", "member")
        )
        if bad is not None or ctx is None:
            return bad or error(401, "unauthenticated")
        try:
            async with scope_of(rt.pool, ctx) as conn:
                row = await (
                    await conn.execute("SELECT cairn.attest_policy(%s)", (policy_id,))
                ).fetchone()
                if row and row[0]:
                    await record(conn, "policy.attested", "policy", str(policy_id))
        except psycopg.errors.RaiseException as exc:
            return json_response(409, {"error": "refused", "reason": _db_rule(exc)})
        return json_response(200, {"attested": True})

    # --- risks ---------------------------------------------------------------------------

    @router.get("/governance/risks")
    async def risks(request: Request) -> Response:
        ctx, bad = await guard(rt, request, "risk.list", LEADS + ("auditor",))
        if bad is not None or ctx is None:
            return bad or error(401, "unauthenticated")
        async with scope_of(rt.pool, ctx) as conn:
            rows = await (
                await conn.execute(
                    "SELECT r.risk_id, r.title, r.description, r.category, r.likelihood, r.impact, r.score, "
                    "r.state, r.mitigation, r.review_by, p.display_name, r.owner_principal "
                    "FROM cairn.risks r JOIN cairn.principals p ON p.principal_id = r.owner_principal "
                    "ORDER BY (r.state = 'closed'), r.score DESC, r.risk_id"
                )
            ).fetchall()
        today = dt.datetime.now(dt.UTC).date()
        return json_response(
            200,
            {
                "risks": [
                    {
                        "risk_id": r[0],
                        "title": r[1],
                        "description": r[2],
                        "category": r[3],
                        "likelihood": r[4],
                        "impact": r[5],
                        "score": r[6],
                        "state": r[7],
                        "mitigation": r[8],
                        "review_by": r[9].isoformat() if r[9] else None,
                        "owner": r[10],
                        "can_edit": ctx.role in ("owner", "admin") or r[11] == ctx.principal_id,
                        "review_overdue": bool(r[9] and r[9] < today and r[7] != "closed"),
                    }
                    for r in rows
                ]
            },
        )

    @router.post("/governance/risks")
    async def add_risk(body: RiskBody, request: Request) -> Response:
        ctx, bad = await guard(rt, request, "risk.create", LEADS)
        if bad is not None or ctx is None:
            return bad or error(401, "unauthenticated")
        async with scope_of(rt.pool, ctx) as conn:
            row = await (
                await conn.execute(
                    "INSERT INTO cairn.risks (title, description, category, likelihood, impact, owner_principal, "
                    "mitigation, review_by, created_by) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING risk_id",
                    (
                        body.title,
                        body.description,
                        body.category,
                        body.likelihood,
                        body.impact,
                        ctx.principal_id,
                        body.mitigation,
                        body.review_by,
                        ctx.principal_id,
                    ),
                )
            ).fetchone()
            assert row is not None  # noqa: S101
            await record(
                conn, "risk.created", "risk", str(row[0]), {"score": body.likelihood * body.impact}
            )
        return json_response(201, {"risk_id": row[0]})

    @router.patch("/governance/risks/{risk_id}")
    async def patch_risk(risk_id: int, body: RiskPatch, request: Request) -> Response:
        ctx, bad = await guard(rt, request, "risk.update", LEADS)
        if bad is not None or ctx is None:
            return bad or error(401, "unauthenticated")
        changes = body.model_dump(exclude_unset=True)
        if not changes:
            return error(422, "invalid_request")
        sets = ", ".join(f"{k} = %s" for k in changes)  # keys come from the model, not the request
        async with scope_of(rt.pool, ctx) as conn:
            cur = await conn.execute(
                f"UPDATE cairn.risks SET {sets} WHERE risk_id = %s",  # noqa: S608
                (*changes.values(), risk_id),
            )
            if cur.rowcount == 0:
                return error(404, "not_found")
            await record(conn, "risk.updated", "risk", str(risk_id), {"fields": sorted(changes)})
        return json_response(200, {"status": "updated"})

    # --- summary for the dashboard -------------------------------------------------------

    @router.get("/governance/summary")
    async def summary(request: Request) -> Response:
        ctx, bad = await guard(rt, request, "governance.summary")
        if bad is not None or ctx is None:
            return bad or error(401, "unauthenticated")
        async with scope_of(rt.pool, ctx) as conn:
            pending = await (
                await conn.execute(
                    "SELECT count(*) FILTER (WHERE a.requested_by <> %s AND NOT EXISTS (SELECT 1 FROM cairn.approval_votes v "
                    "WHERE v.approval_id = a.approval_id AND v.voter_principal = %s)), count(*) "
                    "FROM cairn.approvals a WHERE a.state = 'pending'",
                    (ctx.principal_id, ctx.principal_id),
                )
            ).fetchone()
            to_attest = await (
                await conn.execute(
                    "SELECT count(*) FROM cairn.policies p JOIN cairn.policy_versions v ON v.policy_id = p.policy_id "
                    "AND v.state = 'published' WHERE NOT EXISTS (SELECT 1 FROM cairn.attestations a "
                    "WHERE a.version_id = v.version_id AND a.principal_id = %s)",
                    (ctx.principal_id,),
                )
            ).fetchone()
            high = None
            if ctx.role in LEADS + ("auditor",):
                high = await (
                    await conn.execute(
                        "SELECT count(*) FILTER (WHERE state <> 'closed' AND score >= 15), "
                        "count(*) FILTER (WHERE state <> 'closed') FROM cairn.risks"
                    )
                ).fetchone()
        can_vote = ctx.role in LEADS
        return json_response(
            200,
            {
                "approvals_waiting_for_me": int(pending[0]) if pending and can_vote else 0,
                "approvals_pending": int(pending[1]) if pending else 0,
                "policies_to_attest": int(to_attest[0])
                if to_attest and ctx.role != "auditor"
                else 0,
                "high_risks": int(high[0]) if high else None,
                "open_risks": int(high[1]) if high else None,
            },
        )

    app.include_router(router)
