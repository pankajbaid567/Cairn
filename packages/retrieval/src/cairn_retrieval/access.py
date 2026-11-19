"""Principal snapshot and the transaction that carries it (design §11; PD1-03, PD1-06).

L1: the snapshot is the sorted set of the caller's own principal id, the ``group:public`` id and
every group with a membership whose ``valid_period`` contains ``now()``. It is taken inside the
same REPEATABLE READ transaction that runs retrieval, and records ``max(acl_versions.version)``,
so the recorded ACL version matches the rows read. An unknown principal yields the empty group
set, and every protected query then returns zero rows. For an agent the effective user is the one
it acts for, and the agent's own id is never in ``groups``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from psycopg import AsyncConnection, IsolationLevel
from psycopg_pool import AsyncConnectionPool

from cairn_core.db import aset_local
from cairn_core.tenancy import current_org

PUBLIC_GROUP = "group:public"


@dataclass(frozen=True)
class PrincipalSnapshot:
    principal_id: int  # the caller: a user, or an agent principal
    acting_for: int | None  # the user an agent acts for
    groups: tuple[int, ...]  # sorted, distinct; empty for an unknown principal
    acl_version: int
    taken_at: datetime

    def setting(self) -> str:
        """Postgres array text, e.g. ``{3,7,12}``."""
        return "{" + ",".join(str(g) for g in self.groups) + "}"

    @property
    def effective_user(self) -> int:
        return self.acting_for if self.acting_for is not None else self.principal_id


async def take_snapshot(
    conn: AsyncConnection[Any], principal_id: int, acting_for: int | None = None
) -> PrincipalSnapshot:
    """Take the snapshot on ``conn``, which must be inside a transaction."""
    effective = acting_for if acting_for is not None else principal_id
    org = current_org()
    if org is not None:  # unset: no tenant row is visible (fail closed)
        await aset_local(conn, "cairn.org_id", str(org))
    await aset_local(conn, "cairn.principal_id", str(effective))
    known = await (
        await conn.execute("SELECT 1 FROM cairn.principals WHERE principal_id = %s", (effective,))
    ).fetchone()
    groups: set[int] = set()
    if known is not None:
        groups.add(effective)
        public = await (
            await conn.execute(
                "SELECT principal_id FROM cairn.principals WHERE external_id = %s", (PUBLIC_GROUP,)
            )
        ).fetchone()
        if public is not None:
            groups.add(int(public[0]))
        rows = await (
            await conn.execute(
                "SELECT group_id FROM cairn.memberships "
                "WHERE member_id = %s AND valid_period @> now()",
                (effective,),
            )
        ).fetchall()
        groups.update(int(r[0]) for r in rows)
    version_row = await (
        await conn.execute("SELECT coalesce(max(version), 0) FROM cairn.acl_versions")
    ).fetchone()
    snapshot = PrincipalSnapshot(
        principal_id=principal_id,
        acting_for=acting_for,
        groups=tuple(sorted(groups)),
        acl_version=int(version_row[0]) if version_row else 0,
        taken_at=datetime.now(UTC),
    )
    await aset_local(conn, "cairn.principal_groups", snapshot.setting())
    return snapshot


@asynccontextmanager
async def principal_transaction(
    pool: AsyncConnectionPool[AsyncConnection[Any]],
    principal_id: int,
    *,
    acting_for: int | None = None,
    read_only: bool = False,
) -> AsyncIterator[tuple[AsyncConnection[Any], PrincipalSnapshot]]:
    """A REPEATABLE READ transaction with the principal settings written (L1 + L3 inputs)."""
    async with pool.connection() as conn:
        await conn.set_isolation_level(IsolationLevel.REPEATABLE_READ)
        if read_only:
            await conn.set_read_only(True)
        try:
            async with conn.transaction():
                snapshot = await take_snapshot(conn, principal_id, acting_for)
                yield conn, snapshot
        finally:
            await conn.set_isolation_level(None)
            if read_only:
                await conn.set_read_only(False)
