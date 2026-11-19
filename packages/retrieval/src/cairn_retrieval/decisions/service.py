"""Read decision memory under the caller's permissions (plan 2.9 to 2.11, 2.13; ADR-0003, ADR-0009).

Everything here goes through registered queries (so the ACL and visibility guards are in every
statement) and is called inside ``principal_transaction``. A decision the caller cannot see answers
exactly like one that does not exist.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from psycopg import AsyncConnection

from cairn_core.db import aset_local
from cairn_retrieval import queries as _queries  # noqa: F401  (registers the queries)
from cairn_retrieval.access import PrincipalSnapshot
from cairn_retrieval.registry import run_registered
from cairn_retrieval.temporal import as_of_instant

MAX_HOPS = 3
LINK_KINDS = ("supersedes", "partially_reverts", "relates", "conflicts")


@dataclass(frozen=True)
class DecisionView:
    key: str
    title: str
    question: str | None
    outcome: str | None
    verification: str
    origin: str


@dataclass(frozen=True)
class StateAsOf:
    status: str  # "absent" when nothing was recorded by then
    valid_from: datetime | None
    valid_to: datetime | None
    approximate: bool = False


@dataclass(frozen=True)
class Neighborhood:
    """Edges ``(from, to, kind)`` reached by graph expansion and the decision keys they touch."""

    edges: tuple[tuple[str, str, str], ...]
    keys: tuple[str, ...]


async def get_decision(
    conn: AsyncConnection[Any], snap: PrincipalSnapshot, key: str
) -> DecisionView | None:
    rows = await run_registered(conn, "decision_get", {"key": key}, snap)
    if not rows:
        return None
    r = rows[0]
    return DecisionView(
        r["key"], r["title"], r["question"], r["outcome"], r["verification"], r["origin"]
    )


STATE_AFTER = {
    "proposed": "proposed",
    "accepted": "accepted",
    "rejected": "rejected",
    "withdrawn": "withdrawn",
    "implemented": "implemented",
    "superseded": "superseded",
    "deprecated": "deprecated",
    "reverted": "reverted",
    "reopened": "proposed",
}
"""Lifecycle events and the state each leads to. Annotation events (discussed, amended,
partially_reverted) are not here: they never change state."""


def lifecycle_state(
    events: Sequence[dict[str, Any]], valid: datetime, recorded: datetime
) -> StateAsOf:
    """The state at ``valid`` as known at ``recorded``, from the events given (the *visible* ones).

    Same rule as the scenario oracle: among lifecycle events recorded by ``recorded`` and valid by
    ``valid``, the one latest by valid time wins; ties go to the later sequence number. The stored
    ``decision_versions`` rectangles encode the same answer for *all* events, but they would show the
    timing of events the caller cannot see, so the read path derives the answer from visible events.
    """  # noqa: E501
    chosen: dict[str, Any] | None = None
    for e in events:
        if e["event"] not in STATE_AFTER or e["recorded_at"] > recorded or e["valid_at"] > valid:
            continue
        if chosen is None or (e["valid_at"], e["seq"]) > (chosen["valid_at"], chosen["seq"]):
            chosen = e
    if chosen is None:
        return StateAsOf("absent", None, None)
    later = [
        e["valid_at"]
        for e in events
        if e["event"] in STATE_AFTER
        and e["recorded_at"] <= recorded
        and e["valid_at"] > chosen["valid_at"]
        and STATE_AFTER[e["event"]] != STATE_AFTER[chosen["event"]]
    ]
    return StateAsOf(
        STATE_AFTER[chosen["event"]], chosen["valid_at"], min(later) if later else None
    )


async def state_as_of(
    conn: AsyncConnection[Any],
    snap: PrincipalSnapshot,
    key: str,
    valid: date | datetime | None = None,
    recorded: date | datetime | None = None,
) -> StateAsOf | None:
    """The lifecycle status at ``valid`` as known at ``recorded`` (both default to now), derived
    from the events the caller may see.

    ``None`` when the decision is not visible. ``"absent"`` when it is visible but no visible event
    puts it in a state by then.
    """
    when_recorded = as_of_instant(recorded)
    events = await timeline(conn, snap, key, when_recorded)
    if events is None:
        return None
    return lifecycle_state(events, as_of_instant(valid), when_recorded)


async def timeline(
    conn: AsyncConnection[Any],
    snap: PrincipalSnapshot,
    key: str,
    recorded: date | datetime | None = None,
) -> list[dict[str, Any]] | None:
    """Events the caller may see that were recorded by ``recorded``, in valid-time order."""
    if await get_decision(conn, snap, key) is None:
        return None
    return await run_registered(
        conn, "decision_timeline", {"key": key, "recorded": as_of_instant(recorded)}, snap
    )


async def visible_claims(
    conn: AsyncConnection[Any], snap: PrincipalSnapshot, key: str
) -> list[dict[str, Any]]:
    return await run_registered(conn, "decision_claims", {"key": key}, snap)


async def claim_citations(
    conn: AsyncConnection[Any], snap: PrincipalSnapshot, key: str
) -> dict[str, list[dict[str, Any]]]:
    """``{claim key: [{external_id, title, support_set}, ...]}`` from fully visible support sets."""
    out: dict[str, list[dict[str, Any]]] = {}
    for r in await run_registered(conn, "decision_claim_citations", {"key": key}, snap):
        out.setdefault(r["claim_key"], []).append(
            {"external_id": r["external_id"], "title": r["title"], "support_set": r["support_set"]}
        )
    return out


async def expand(
    conn: AsyncConnection[Any],
    snap: PrincipalSnapshot,
    key: str,
    hops: int = 1,
    recorded: date | datetime | None = None,
) -> Neighborhood:
    """Graph expansion over supersession and related edges, 1 to 3 hops (D8). Each hop re-checks
    visibility of both ends in the registered query, so a hidden decision is never reached through a
    visible neighbour. Returns ``{decision key: [(from, to, kind), ...]}`` of the edges found."""
    hops = max(1, min(hops, MAX_HOPS))
    seen = {key}
    frontier = [key]
    edges: list[tuple[str, str, str]] = []
    when = as_of_instant(recorded)
    for _ in range(hops):
        if not frontier:
            break
        rows = await run_registered(
            conn, "decision_neighbors", {"keys": frontier, "recorded": when}, snap
        )
        nxt: list[str] = []
        for r in rows:
            e = (r["from_key"], r["to_key"], r["kind"])
            if e not in edges:
                edges.append(e)
            for k in (r["from_key"], r["to_key"]):
                if k not in seen:
                    seen.add(k)
                    nxt.append(k)
        frontier = nxt
    return Neighborhood(edges=tuple(edges), keys=tuple(sorted(seen)))


def principal_set_hash(snap: PrincipalSnapshot) -> str:
    """Identifies who sees the same things: the sorted groups. Used to cache regenerated summaries."""  # noqa: E501
    return hashlib.sha256(snap.setting().encode()).hexdigest()[:32]


def render_summary(view: DecisionView, status: str | None, claims: Sequence[dict[str, Any]]) -> str:
    """A summary built only from what the caller can see. It names no hidden claim and counts none."""  # noqa: E501
    parts = [f"{view.title}."]
    if view.outcome:
        parts.append(view.outcome.rstrip(".") + ".")
    if status and status != "absent":
        parts.append(f"Status: {status}.")
    parts.extend(c["text"].rstrip(".") + "." for c in claims)
    return " ".join(parts)


async def summary(
    conn: AsyncConnection[Any],
    snap: PrincipalSnapshot,
    key: str,
    *,
    valid: date | datetime | None = None,
    recorded: date | datetime | None = None,
) -> dict[str, Any] | None:
    """The summary for this principal set, from cache when its inputs have not changed (plan 2.11).

    The cache is keyed by (principal set, decision) and validated by the hash of the visible inputs,
    so a change of visibility or of a claim rebuilds it. Rows marked stale by lineage rebuild too.
    """
    view = await get_decision(conn, snap, key)
    if view is None:
        return None
    state = await state_as_of(conn, snap, key, valid, recorded)
    claims = await visible_claims(conn, snap, key)
    inputs = json.dumps(
        [
            view.title,
            view.outcome,
            state.status if state else None,
            [(c["key"], c["text"]) for c in claims],
        ],
        sort_keys=True,
    )
    input_hash = hashlib.sha256(inputs.encode()).hexdigest()
    psh = principal_set_hash(snap)
    await aset_local(conn, "cairn.principal_set_hash", psh)
    cached = await (
        await conn.execute(
            "SELECT body, input_hash, stale FROM cairn.derived_artifacts "
            "WHERE artifact_kind = 'decision-summary' AND artifact_id = %s AND principal_set_hash = %s",  # noqa: E501
            (key, psh),
        )
    ).fetchone()
    if cached and cached[1] == input_hash and not cached[2]:
        return {**cached[0], "cached": True}
    body = {
        "text": render_summary(view, state.status if state else None, claims),
        "claims": [c["key"] for c in claims],
    }
    # read_only transactions cannot write the cache; callers that want caching use a read/write one
    try:
        async with (
            conn.transaction()
        ):  # a savepoint: a failed write must not abort the caller's transaction
            await conn.execute(
                "INSERT INTO cairn.derived_artifacts (artifact_kind, artifact_id, principal_set_hash, input_hash, body, stale) "  # noqa: E501
                "VALUES ('decision-summary', %s, %s, %s, %s::jsonb, false) "
                "ON CONFLICT (artifact_kind, artifact_id, principal_set_hash) DO UPDATE SET "
                "input_hash = EXCLUDED.input_hash, body = EXCLUDED.body, stale = false, built_at = now()",  # noqa: E501
                (key, psh, input_hash, json.dumps(body)),
            )
    except Exception:  # noqa: S110
        pass
    return {**body, "cached": False}


def like_pattern(text: str) -> str:
    """``%text%`` with LIKE metacharacters escaped (the query uses ``ESCAPE '\\'``)."""
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


async def search_decisions(
    conn: AsyncConnection[Any],
    snap: PrincipalSnapshot,
    text: str,
    *,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Decisions whose key, title, question or outcome contain ``text``, among the visible ones."""
    return await run_registered(
        conn, "decision_search", {"pattern": like_pattern(text), "k": limit}, snap
    )


async def decisions_for_anchors(
    conn: AsyncConnection[Any],
    snap: PrincipalSnapshot,
    values: Sequence[str],
) -> list[dict[str, Any]]:
    """Visible decisions anchored to any of the repos, paths or pull requests in ``values``."""
    if not values:
        return []
    return await run_registered(conn, "decision_anchors", {"values": list(values)}, snap)
