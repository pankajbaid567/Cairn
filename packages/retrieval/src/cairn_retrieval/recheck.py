"""L4: the late recheck before an answer leaves the service (plan 3.9).

Retrieval and composition can take seconds (model calls). A revocation applied in between must not
leak, so just before responding the cited items are read again with a *fresh* snapshot of the
principal's groups. If any of them is no longer visible the answer is withheld.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from cairn_retrieval import queries as _queries  # noqa: F401  (registers the queries)
from cairn_retrieval.access import principal_transaction
from cairn_retrieval.registry import run_registered


async def still_visible(
    pool: AsyncConnectionPool[AsyncConnection[Any]],
    principal_id: int,
    item_ids: Sequence[int],
    *,
    acting_for: int | None = None,
) -> bool:
    """True when every item is visible to the principal now (an empty list is trivially visible)."""
    wanted = sorted(set(item_ids))
    if not wanted:
        return True
    async with principal_transaction(pool, principal_id, acting_for=acting_for, read_only=True) as (
        conn,
        snap,
    ):
        rows = await run_registered(conn, "recheck_items", {"ids": wanted}, snap)
    return {int(r["item_id"]) for r in rows} == set(wanted)
