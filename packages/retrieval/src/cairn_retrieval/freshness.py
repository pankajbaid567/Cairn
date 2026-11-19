"""Freshness badges (plan 3.12): Live, Delayed, Stale, Imported.

* ``Imported``: the source has no live feed (files imported by hand). Honest, not an error.
* ``Live``: a GitHub source that synced within a day and has nothing waiting for more than 5 minutes.
* ``Delayed``: an event for the source has waited more than 5 minutes unapplied.
* ``Stale``: a GitHub source that never synced or last synced more than a day ago.

An answer carries the weakest badge among the sources it cites.
"""  # noqa: E501

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any, Final

from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from cairn_retrieval import queries as _queries  # noqa: F401  (registers the queries)
from cairn_retrieval.access import principal_transaction
from cairn_retrieval.registry import run_registered

DELAYED_AFTER_S: Final = 300.0
STALE_AFTER_S: Final = 86400.0
ORDER: Final = ("Live", "Imported", "Delayed", "Stale")  # best to worst


def badge(source_key: str, last_synced_age_s: float | None, oldest_pending_s: float | None) -> str:
    if not source_key.startswith("github:"):
        return "Imported"
    if last_synced_age_s is None or last_synced_age_s > STALE_AFTER_S:
        return "Stale"
    if oldest_pending_s is not None and oldest_pending_s > DELAYED_AFTER_S:
        return "Delayed"
    return "Live"


def weakest(badges: Iterable[str]) -> str:
    worst = "Live"
    seen = False
    for b in badges:
        seen = True
        if ORDER.index(b) > ORDER.index(worst):
            worst = b
    return worst if seen else "Imported"


async def badges_for(
    pool: AsyncConnectionPool[AsyncConnection[Any]],
    principal_id: int,
    item_ids: Sequence[int],
    *,
    acting_for: int | None = None,
) -> dict[str, str]:
    """``{source_key: badge}`` for the sources of the readable items among ``item_ids``."""
    ids = sorted(set(item_ids))
    if not ids:
        return {}
    out: dict[str, str] = {}
    async with principal_transaction(pool, principal_id, acting_for=acting_for, read_only=True) as (
        conn,
        snap,
    ):
        rows = await run_registered(conn, "item_sources", {"ids": ids}, snap)
        for r in rows:
            key = str(r["source_key"])
            age: float | None = None
            pending: float | None = None
            if key.startswith("github:"):
                got = await (
                    await conn.execute(
                        "SELECT extract(epoch FROM now() - last_synced_at), oldest_pending_s "
                        "FROM cairn.source_freshness(%s)",
                        (key,),
                    )
                ).fetchone()
                if got is not None:
                    age = None if got[0] is None else float(got[0])
                    pending = None if got[1] is None else float(got[1])
            out[key] = badge(key, age, pending)
    return out


async def source_report(
    pool: AsyncConnectionPool[AsyncConnection[Any]],
    principal_id: int,
    *,
    acting_for: int | None = None,
) -> list[dict[str, Any]]:
    """Every source the principal can read an item from, with its badge and timings (plan 3.14)."""
    out: list[dict[str, Any]] = []
    async with principal_transaction(pool, principal_id, acting_for=acting_for, read_only=True) as (
        conn,
        snap,
    ):
        for r in await run_registered(conn, "visible_sources", {}, snap):
            key = str(r["source_key"])
            age: float | None = None
            pending: float | None = None
            synced: Any = None
            if key.startswith("github:"):
                got = await (
                    await conn.execute(
                        "SELECT last_synced_at, extract(epoch FROM now() - last_synced_at), "
                        "oldest_pending_s FROM cairn.source_freshness(%s)",
                        (key,),
                    )
                ).fetchone()
                if got is not None:
                    synced = got[0].isoformat() if got[0] is not None else None
                    age = None if got[1] is None else float(got[1])
                    pending = None if got[2] is None else float(got[2])
            out.append(
                {
                    "source": key,
                    "badge": badge(key, age, pending),
                    "last_synced_at": synced,
                    "oldest_pending_s": None if pending is None else round(pending, 1),
                }
            )
    return out


async def my_audit(
    pool: AsyncConnectionPool[AsyncConnection[Any]],
    principal_id: int,
    limit: int = 50,
    *,
    acting_for: int | None = None,
) -> list[dict[str, Any]]:
    """The caller's own audit entries (ids and counts only)."""
    async with principal_transaction(pool, principal_id, acting_for=acting_for, read_only=True) as (
        conn,
        _,
    ):
        rows = await (
            await conn.execute(
                "SELECT audit_id, at, endpoint, outcome, returned, acl_version, model "
                "FROM cairn.my_audit(%s)",
                (limit,),
            )
        ).fetchall()
    return [
        {
            "audit_id": str(r[0]),
            "at": r[1].isoformat(),
            "endpoint": r[2],
            "outcome": r[3],
            "returned": r[4],
            "acl_version": r[5],
            "model": r[6],
        }
        for r in rows
    ]
