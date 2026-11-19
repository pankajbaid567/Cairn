"""Who is calling, in which organisation, with which role.

``resolve_ctx`` turns the request's cookie into a ``Ctx``: a signed-in account acting in one
organisation, or a demo persona (the seeded organisation). It also binds the organisation for the
rest of the request (``cairn_core.tenancy``), which every transaction reads. ``scoped`` opens a
transaction with the organisation, account, principal and role written into it; those are the
inputs the database policies and functions read. The role and principal come from the stored
membership, never from the request.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from fastapi import Request
from psycopg import AsyncConnection
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from cairn_api.sessions import COOKIE_NAME, SESSION_COOKIE, verify, verify_session
from cairn_core.db import aset_local
from cairn_core.tenancy import set_request_scope

DEMO_ORG = 1
ROLES = ("owner", "admin", "manager", "member", "auditor", "guest")
RANK = {"owner": 5, "admin": 4, "manager": 3, "member": 2, "auditor": 1, "guest": 0}


@dataclass(frozen=True)
class Ctx:
    account_id: str | None
    org_id: int | None
    principal_id: int | None
    role: str | None
    email: str | None
    display_name: str
    handle: str | None  # the user id without "user:", used as the persona name
    demo_persona: bool = False

    def at_least(self, role: str) -> bool:
        return self.role is not None and RANK[self.role] >= RANK[role]

    def has_role(self, *roles: str) -> bool:
        return self.role in roles


@asynccontextmanager
async def scoped(
    pool: AsyncConnectionPool[AsyncConnection[Any]],
    *,
    org_id: int | None,
    account_id: str | None = None,
    principal_id: int | None = None,
    role: str | None = None,
) -> AsyncIterator[AsyncConnection[Any]]:
    """One transaction with the request settings written. Nothing is visible without an org."""
    async with pool.connection() as conn, conn.transaction():
        for name, value in (
            ("cairn.org_id", org_id),
            ("cairn.account_id", account_id),
            ("cairn.principal_id", principal_id),
            ("cairn.role", role),
        ):
            if value is not None:
                await aset_local(conn, name, str(value))
        yield conn


def scope_of(pool: AsyncConnectionPool[AsyncConnection[Any]], ctx: Ctx) -> Any:
    return scoped(
        pool,
        org_id=ctx.org_id,
        account_id=ctx.account_id,
        principal_id=ctx.principal_id,
        role=ctx.role,
    )


async def record(
    conn: AsyncConnection[Any],
    action: str,
    target_type: str | None = None,
    target_id: str | None = None,
    detail: dict[str, Any] | None = None,
    outcome: str = "ok",
) -> None:
    """Append one event to the organisation's audit chain, in the caller's transaction."""
    await conn.execute(
        "SELECT cairn.record_event(%s, %s, %s, %s, %s)",
        (action, target_type, target_id, Jsonb(detail or {}), outcome),
    )


async def _membership(
    pool: AsyncConnectionPool[AsyncConnection[Any]], account_id: str, org_id: int
) -> tuple[int, str, str] | None:
    async with scoped(pool, org_id=org_id, account_id=account_id) as conn:
        row = await (
            await conn.execute(
                "SELECT m.principal_id, m.role, p.external_id FROM cairn_meta.org_members m "
                "JOIN cairn_meta.orgs o ON o.org_id = m.org_id "
                "JOIN cairn.principals p ON p.principal_id = m.principal_id "
                "WHERE m.org_id = %s AND m.account_id = %s AND m.state = 'active' "
                "AND o.state = 'active'",
                (org_id, account_id),
            )
        ).fetchone()
    return (int(row[0]), str(row[1]), str(row[2])) if row else None


async def _account(
    pool: AsyncConnectionPool[AsyncConnection[Any]], account_id: str, org_id: int | None
) -> tuple[str, str] | None:
    async with scoped(pool, org_id=org_id, account_id=account_id) as conn:
        row = await (
            await conn.execute(
                "SELECT email, display_name FROM cairn_meta.accounts "
                "WHERE account_id = %s AND state = 'active'",
                (account_id,),
            )
        ).fetchone()
    return (str(row[0]), str(row[1])) if row else None


async def resolve_ctx(rt: Any, request: Request) -> Ctx | None:
    """The caller of this request, or ``None``. Binds the organisation as a side effect."""
    cached = getattr(request.state, "ctx", None)
    if cached is not None:
        return cached
    ctx = await _resolve(rt, request)
    request.state.ctx = ctx
    if ctx is not None:
        set_request_scope(ctx.org_id, ctx.account_id)
    return ctx


async def _resolve(rt: Any, request: Request) -> Ctx | None:
    pool = rt.pool
    session = verify_session(request.cookies.get(SESSION_COOKIE), rt.secret)
    if session is not None:
        account_id, org_id = session
        acct = await _account(pool, account_id, org_id)
        if acct is None:
            return None
        if org_id is not None:
            member = await _membership(pool, account_id, org_id)
            if member is not None:
                pid, role, external = member
                return Ctx(
                    account_id, org_id, pid, role, acct[0], acct[1], external.split(":", 1)[1]
                )
        return Ctx(account_id, None, None, None, acct[0], acct[1], None)
    persona = verify(request.cookies.get(COOKIE_NAME), rt.secret)
    if persona is None:
        return None
    async with scoped(pool, org_id=DEMO_ORG) as conn:
        row = await (
            await conn.execute(
                "SELECT p.principal_id, p.display_name, m.account_id::text, m.role, a.email "
                "FROM cairn.principals p "
                "LEFT JOIN cairn_meta.org_members m ON m.principal_id = p.principal_id "
                "AND m.org_id = p.org_id AND m.state = 'active' "
                "LEFT JOIN cairn_meta.accounts a ON a.account_id = m.account_id "
                "WHERE p.external_id = %s AND p.kind = 'user'",
                (f"user:{persona}",),
            )
        ).fetchone()
    if row is None:
        return None
    return Ctx(
        row[2], DEMO_ORG, int(row[0]), row[3] or "member", row[4], str(row[1]), persona, True
    )
