"""Organisation, members, invitations, teams and the audit log (SaaS plan, Phase A)."""

from __future__ import annotations

import csv
import hashlib
import io
import re
import secrets
from typing import Any

import psycopg
from fastapi import APIRouter, FastAPI, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from cairn_api.accounts import _cookie, _ctx_for, memberships
from cairn_api.apihelp import error, json_response
from cairn_api.tenant import Ctx, record, resolve_ctx, scope_of, scoped

ADMIN = ("owner", "admin")
AUDIT_READERS = ("owner", "admin", "auditor")


class RenameBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=2, max_length=80)


class InviteBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: str = Field(min_length=3, max_length=254)
    role: str = Field(pattern="^(admin|manager|member|auditor|guest)$")


class MemberPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: str | None = Field(None, pattern="^(owner|admin|manager|member|auditor|guest)$")
    active: bool | None = None
    title: str | None = Field(None, max_length=80)


class TeamBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=2, max_length=80)


class TeamMemberBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    principal_id: int = Field(ge=1)
    on: bool


class AcceptBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    token: str = Field(min_length=20, max_length=200)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _db_denied(exc: Exception) -> bool:
    return isinstance(exc, psycopg.errors.InsufficientPrivilege)


def _db_rule(exc: Exception) -> str:
    """The database's own message for a rule it enforced (never an internal error)."""
    return str(exc).split("\n")[0][:160]


async def guard(
    rt: Any, request: Request, action: str, roles: tuple[str, ...] | None = None
) -> tuple[Ctx | None, Response | None]:
    """The signed-in caller inside an organisation, or the response to send instead.

    A caller whose role is not allowed gets a 403, and the refusal is recorded in the audit chain.
    """
    ctx = await resolve_ctx(rt, request)
    if ctx is None:
        return None, error(401, "unauthenticated")
    if ctx.org_id is None or ctx.principal_id is None:
        return None, error(409, "no_organisation")
    if roles is not None and ctx.role not in roles:
        async with scope_of(rt.pool, ctx) as conn:
            await record(conn, action, None, None, {"needs": list(roles)}, "denied")
        return None, error(403, "forbidden")
    return ctx, None


def install(app: FastAPI, rt: Any) -> None:
    router = APIRouter()

    @router.get("/org")
    async def get_org(request: Request) -> Response:
        ctx, bad = await guard(rt, request, "org.read")
        if bad is not None or ctx is None:
            return bad or error(401, "unauthenticated")
        async with scope_of(rt.pool, ctx) as conn:
            org = await (
                await conn.execute(
                    "SELECT org_id, slug, name, plan, is_demo, created_at FROM cairn_meta.orgs "
                    "WHERE org_id = %s",
                    (ctx.org_id,),
                )
            ).fetchone()
            counts = await (
                await conn.execute(
                    "SELECT count(*) FILTER (WHERE state = 'active'), "
                    "count(*) FILTER (WHERE state = 'deactivated') FROM cairn_meta.org_members "
                    "WHERE org_id = %s",
                    (ctx.org_id,),
                )
            ).fetchone()
        if org is None or counts is None:
            return error(404, "not_found")
        return json_response(
            200,
            {
                "org_id": org[0],
                "slug": org[1],
                "name": org[2],
                "plan": org[3],
                "is_demo": org[4],
                "created_at": org[5].isoformat(),
                "active_members": counts[0],
                "deactivated_members": counts[1],
                "role": ctx.role,
            },
        )

    @router.patch("/org")
    async def rename(body: RenameBody, request: Request) -> Response:
        ctx, bad = await guard(rt, request, "org.rename", ADMIN)
        if bad is not None or ctx is None:
            return bad or error(401, "unauthenticated")
        async with scope_of(rt.pool, ctx) as conn:
            await conn.execute("SELECT cairn_meta.rename_org(%s)", (body.name.strip(),))
            await record(conn, "org.renamed", "org", str(ctx.org_id))
        return json_response(200, {"name": body.name.strip()})

    @router.get("/org/members")
    async def members(request: Request) -> Response:
        ctx, bad = await guard(rt, request, "member.list")
        if bad is not None or ctx is None:
            return bad or error(401, "unauthenticated")
        async with scope_of(rt.pool, ctx) as conn:
            rows = await (
                await conn.execute(
                    "SELECT account_id::text, principal_id, email, display_name, role, state, "
                    "title, joined_at, team_ids FROM cairn_meta.directory()"
                )
            ).fetchall()
        return json_response(
            200,
            {
                "members": [
                    {
                        "account_id": r[0],
                        "principal_id": r[1],
                        "email": r[2],
                        "name": r[3],
                        "role": r[4],
                        "state": r[5],
                        "title": r[6],
                        "joined_at": r[7].isoformat(),
                        "team_ids": list(r[8]),
                        "you": r[0] == ctx.account_id,
                    }
                    for r in rows
                ]
            },
        )

    @router.post("/org/invites")
    async def invite(body: InviteBody, request: Request) -> Response:
        ctx, bad = await guard(rt, request, "member.invite", ADMIN)
        if bad is not None or ctx is None:
            return bad or error(401, "unauthenticated")
        email = body.email.strip().lower()
        if not re.fullmatch(r"[^@\s]{1,64}@[^@\s]{1,190}\.[^@\s]{2,}", email):
            return error(422, "invalid_request")
        token = secrets.token_urlsafe(32)
        try:
            async with scope_of(rt.pool, ctx) as conn:
                row = await (
                    await conn.execute(
                        "SELECT cairn_meta.create_invite(%s, %s, %s, 168)",
                        (email, body.role, token_hash(token)),
                    )
                ).fetchone()
                assert row is not None  # noqa: S101
                await record(
                    conn,
                    "member.invited",
                    "invite",
                    str(row[0]),
                    {
                        "role": body.role,
                        "email_hash": hashlib.sha256(email.encode()).hexdigest()[:16],
                    },
                )
        except psycopg.errors.InsufficientPrivilege:
            return error(403, "forbidden")
        except psycopg.errors.RaiseException as exc:
            return json_response(409, {"error": "invite_refused", "reason": _db_rule(exc)})
        return json_response(
            201,
            {
                "invite_id": str(row[0]),
                "email": email,
                "role": body.role,
                "token": token,
                "path": f"/#/invite/{token}",
                "expires_in_hours": 168,
            },
        )

    @router.get("/org/invites")
    async def invites(request: Request) -> Response:
        ctx, bad = await guard(rt, request, "invite.list", ADMIN)
        if bad is not None or ctx is None:
            return bad or error(401, "unauthenticated")
        async with scope_of(rt.pool, ctx) as conn:
            rows = await (
                await conn.execute(
                    "SELECT invite_id::text, email, role, state, created_at, expires_at "
                    "FROM cairn_meta.invites WHERE org_id = %s AND state = 'pending' "
                    "ORDER BY created_at DESC",
                    (ctx.org_id,),
                )
            ).fetchall()
        return json_response(
            200,
            {
                "invites": [
                    {
                        "invite_id": r[0],
                        "email": r[1],
                        "role": r[2],
                        "state": r[3],
                        "created_at": r[4].isoformat(),
                        "expires_at": r[5].isoformat(),
                    }
                    for r in rows
                ]
            },
        )

    @router.delete("/org/invites/{invite_id}")
    async def revoke(invite_id: str, request: Request) -> Response:
        ctx, bad = await guard(rt, request, "invite.revoke", ADMIN)
        if bad is not None or ctx is None:
            return bad or error(401, "unauthenticated")
        try:
            async with scope_of(rt.pool, ctx) as conn:
                row = await (
                    await conn.execute("SELECT cairn_meta.revoke_invite(%s::uuid)", (invite_id,))
                ).fetchone()
                if not (row and row[0]):
                    return error(404, "not_found")
                await record(conn, "invite.revoked", "invite", invite_id)
        except psycopg.errors.DataError:
            return error(422, "invalid_request")
        return json_response(200, {"status": "revoked"})

    @router.get("/invites/preview")
    async def preview(token: str) -> Response:
        if not 20 <= len(token) <= 200:
            return error(404, "not_found")
        async with scoped(rt.pool, org_id=None) as conn:
            row = await (
                await conn.execute(
                    "SELECT org_name, role, email FROM cairn_meta.invite_preview(%s)",
                    (token_hash(token),),
                )
            ).fetchone()
        if row is None:
            return error(404, "not_found")
        return json_response(200, {"org": row[0], "role": row[1], "email": row[2]})

    @router.post("/invites/accept")
    async def accept(body: AcceptBody, request: Request) -> Response:
        ctx = await resolve_ctx(rt, request)
        if ctx is None or ctx.account_id is None or ctx.demo_persona:
            return error(401, "unauthenticated")
        try:
            async with scoped(rt.pool, org_id=None, account_id=ctx.account_id) as conn:
                row = await (
                    await conn.execute(
                        "SELECT cairn_meta.accept_invite(%s, %s::uuid)",
                        (token_hash(body.token), ctx.account_id),
                    )
                ).fetchone()
        except psycopg.errors.RaiseException:
            return error(400, "invalid_invite")
        assert row is not None  # noqa: S101
        org_id = int(row[0])
        new_ctx = await _ctx_for(rt, ctx.account_id, org_id)
        if new_ctx is None:
            return error(400, "invalid_invite")
        async with scope_of(rt.pool, new_ctx) as conn:
            await record(conn, "invite.accepted", "org", str(org_id))
        response = json_response(
            200, {"org_id": org_id, "orgs": await memberships(rt, ctx.account_id)}
        )
        _cookie(rt, response, ctx.account_id, org_id)
        return response

    @router.patch("/org/members/{account_id}")
    async def patch_member(account_id: str, body: MemberPatch, request: Request) -> Response:
        ctx, bad = await guard(rt, request, "member.update", ADMIN)
        if bad is not None or ctx is None:
            return bad or error(401, "unauthenticated")
        try:
            async with scope_of(rt.pool, ctx) as conn:
                if body.role is not None:
                    await conn.execute(
                        "SELECT cairn_meta.set_member_role(%s::uuid, %s)", (account_id, body.role)
                    )
                    await record(
                        conn, "member.role_changed", "account", account_id, {"to": body.role}
                    )
                if body.active is not None:
                    await conn.execute(
                        "SELECT cairn_meta.set_member_state(%s::uuid, %s)",
                        (account_id, body.active),
                    )
                    await record(
                        conn,
                        "member.reactivated" if body.active else "member.deactivated",
                        "account",
                        account_id,
                    )
                if body.title is not None:
                    await conn.execute(
                        "SELECT cairn_meta.set_member_title(%s::uuid, %s)", (account_id, body.title)
                    )
                    await record(conn, "member.title_changed", "account", account_id)
        except psycopg.errors.InsufficientPrivilege as exc:
            async with scope_of(rt.pool, ctx) as conn:
                await record(conn, "member.update", "account", account_id, {}, "denied")
            return json_response(403, {"error": "forbidden", "reason": _db_rule(exc)})
        except psycopg.errors.RaiseException as exc:
            return json_response(409, {"error": "refused", "reason": _db_rule(exc)})
        except psycopg.errors.DataError:
            return error(422, "invalid_request")
        return json_response(200, {"status": "updated"})

    @router.get("/org/teams")
    async def teams(request: Request) -> Response:
        ctx, bad = await guard(rt, request, "team.list")
        if bad is not None or ctx is None:
            return bad or error(401, "unauthenticated")
        async with scope_of(rt.pool, ctx) as conn:
            rows = await (
                await conn.execute(
                    "SELECT principal_id, slug, name, member_count FROM cairn_meta.teams()"
                )
            ).fetchall()
        return json_response(
            200,
            {
                "teams": [
                    {"principal_id": r[0], "slug": r[1], "name": r[2], "member_count": r[3]}
                    for r in rows
                ]
            },
        )

    @router.post("/org/teams")
    async def create_team(body: TeamBody, request: Request) -> Response:
        ctx, bad = await guard(rt, request, "team.create", ADMIN)
        if bad is not None or ctx is None:
            return bad or error(401, "unauthenticated")
        slug = re.sub(r"[^a-z0-9]+", "-", body.name.lower()).strip("-")[:30].strip("-") or "team"
        try:
            async with scope_of(rt.pool, ctx) as conn:
                row = await (
                    await conn.execute(
                        "SELECT cairn_meta.create_team(%s, %s)", (slug, body.name.strip())
                    )
                ).fetchone()
                assert row is not None  # noqa: S101
                await record(conn, "team.created", "team", str(row[0]), {"slug": slug})
        except psycopg.errors.UniqueViolation:
            return error(409, "team_exists")
        except psycopg.errors.RaiseException as exc:
            return json_response(422, {"error": "invalid_request", "reason": _db_rule(exc)})
        return json_response(201, {"principal_id": row[0], "slug": slug, "name": body.name.strip()})

    @router.post("/org/teams/{team_id}/members")
    async def team_member(team_id: int, body: TeamMemberBody, request: Request) -> Response:
        ctx, bad = await guard(rt, request, "team.member", ADMIN)
        if bad is not None or ctx is None:
            return bad or error(401, "unauthenticated")
        try:
            async with scope_of(rt.pool, ctx) as conn:
                await conn.execute(
                    "SELECT cairn_meta.team_set_member(%s, %s, %s)",
                    (team_id, body.principal_id, body.on),
                )
                await record(
                    conn,
                    "team.member_added" if body.on else "team.member_removed",
                    "team",
                    str(team_id),
                    {"member": body.principal_id},
                )
        except psycopg.errors.RaiseException as exc:
            return json_response(409, {"error": "refused", "reason": _db_rule(exc)})
        return json_response(200, {"status": "updated"})

    # --- audit log -----------------------------------------------------------------------

    @router.get("/org/audit")
    async def audit(
        request: Request, limit: int = 50, before_seq: int | None = None, action: str | None = None
    ) -> Response:
        ctx, bad = await guard(rt, request, "audit.read")
        if bad is not None or ctx is None:
            return bad or error(401, "unauthenticated")
        limit = max(1, min(limit, 200))
        async with scope_of(rt.pool, ctx) as conn:
            rows = await (
                await conn.execute(
                    "SELECT e.org_seq, e.at, e.action, e.target_type, e.target_id, e.outcome, "
                    "e.actor_role, p.display_name, e.detail FROM cairn.audit_events e "
                    "LEFT JOIN cairn.principals p ON p.principal_id = e.actor_principal "
                    "WHERE e.org_id = %s AND (%s::bigint IS NULL OR e.org_seq < %s) "
                    "AND (%s::text IS NULL OR e.action = %s) ORDER BY e.org_seq DESC LIMIT %s",
                    (ctx.org_id, before_seq, before_seq, action, action, limit),
                )
            ).fetchall()
        return json_response(
            200,
            {
                "all": ctx.role in AUDIT_READERS,
                "events": [
                    {
                        "seq": r[0],
                        "at": r[1].isoformat(),
                        "action": r[2],
                        "target_type": r[3],
                        "target_id": r[4],
                        "outcome": r[5],
                        "actor_role": r[6],
                        "actor": r[7],
                        "detail": r[8],
                    }
                    for r in rows
                ],
            },
        )

    @router.get("/org/audit/verify")
    async def verify_chain(request: Request) -> Response:
        ctx, bad = await guard(rt, request, "audit.verify", AUDIT_READERS)
        if bad is not None or ctx is None:
            return bad or error(401, "unauthenticated")
        async with scope_of(rt.pool, ctx) as conn:
            row = await (await conn.execute("SELECT * FROM cairn.verify_chain()")).fetchone()
            assert row is not None  # noqa: S101
            await record(conn, "audit.verified", "audit", None, {"ok": bool(row[0])})
        return json_response(
            200,
            {
                "ok": row[0],
                "checked": row[1],
                "first_bad_seq": row[2],
                "head_seq": row[3],
                "head_hash": row[4],
            },
        )

    @router.get("/org/audit/export")
    async def export(request: Request) -> Response:
        ctx, bad = await guard(rt, request, "audit.export", AUDIT_READERS)
        if bad is not None or ctx is None:
            return bad or error(401, "unauthenticated")
        async with scope_of(rt.pool, ctx) as conn:
            rows = await (
                await conn.execute(
                    "SELECT e.org_seq, e.at, e.action, e.target_type, e.target_id, e.outcome, "
                    "e.actor_role, p.display_name, e.detail::text, e.prev_hash, e.row_hash "
                    "FROM cairn.audit_events e "
                    "LEFT JOIN cairn.principals p ON p.principal_id = e.actor_principal "
                    "WHERE e.org_id = %s ORDER BY e.org_seq",
                    (ctx.org_id,),
                )
            ).fetchall()
            await record(conn, "audit.exported", "audit", None, {"rows": len(rows)})
        out = io.StringIO()
        writer = csv.writer(out)
        writer.writerow(
            [
                "seq",
                "at",
                "action",
                "target_type",
                "target_id",
                "outcome",
                "actor_role",
                "actor",
                "detail",
                "prev_hash",
                "row_hash",
            ]
        )
        for r in rows:
            writer.writerow([r[0], r[1].isoformat(), *r[2:]])
        return Response(
            out.getvalue(),
            media_type="text/csv",
            headers={
                "Content-Disposition": 'attachment; filename="cairn-audit.csv"',
                "Cache-Control": "no-store",
            },
        )

    app.include_router(router)
