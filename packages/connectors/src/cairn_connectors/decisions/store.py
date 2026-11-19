"""Bitemporal writes for decisions (plan 2.6; ADR-0003, ADR-0009).

``decision_events`` is an append-only log. ``decision_versions`` holds the derived rectangles
(valid period x recorded period). Inside the *currently recorded* slice the versions of a decision
partition valid time from its first event onward, so every write only has to find the one row that
contains the new event's valid time:

* close that row's recorded period at the write time (so old recorded times still see it),
* re-insert the part before the event with the old status,
* insert the new status from the event until the next row.

That single move covers supersession, reversion, backdating (a valid time before later events) and
unknown start dates. A correction closes a wrong version's recorded period and re-inserts it with the
right status, leaving valid time unchanged. Nothing rewrites history.

Day mapping (documented in ``docs/temporal-model.md``): an event dated D is stored at D 00:00 UTC; a
query "as of date D" uses the end of D (23:59:59.999999 UTC), so it includes every event of that day,
as the scenario oracle does. Recorded times of one decision are strictly increasing: a write at the
same instant as the previous one is moved forward by one microsecond.
"""  # noqa: E501

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import datetime
from typing import Any, Final

import psycopg

from cairn_retrieval import temporal as _temporal

Conn = psycopg.Connection[Any]

LIFECYCLE: Final = frozenset(
    {
        "proposed",
        "accepted",
        "rejected",
        "withdrawn",
        "implemented",
        "superseded",
        "deprecated",
        "reverted",
        "reopened",
    }
)
ANNOTATIONS: Final = frozenset({"discussed", "amended", "partially_reverted"})
STATE_AFTER: Final = {e: e for e in LIFECYCLE} | {"reopened": "proposed"}
MICRO: Final = _temporal.MICRO


class OutOfOrder(Exception):
    """A write is recorded before the decision's latest recorded version: events must arrive in
    recorded order (replay them sorted)."""


day_start = _temporal.day_start
day_end = _temporal.day_end


def ensure_decision(
    conn: Conn,
    key: str,
    title: str,
    *,
    question: str | None = None,
    outcome: str | None = None,
    origin: str = "manual",
    verification: str = "candidate",
) -> int:
    row = conn.execute(
        "INSERT INTO cairn.decisions (key, title, question, outcome, origin, verification) "
        "VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (org_id, key) DO UPDATE SET title = EXCLUDED.title "  # noqa: E501
        "RETURNING decision_id",
        (key, title, question, outcome, origin, verification),
    ).fetchone()
    assert row is not None  # noqa: S101
    return int(row[0])


def _latest_recorded(conn: Conn, decision_id: int) -> datetime | None:
    row = conn.execute(
        "SELECT max(lower(recorded_period)) FROM cairn.decision_versions WHERE decision_id = %s",
        (decision_id,),
    ).fetchone()
    return row[0] if row else None


def add_event(
    conn: Conn,
    decision_id: int,
    event: str,
    valid_at: datetime,
    recorded_at: datetime,
    acl: Sequence[int],
    *,
    source_item_id: int | None = None,
    source_ref: str | None = None,
    by_key: str | None = None,
    note: str | None = None,
) -> int:
    """Append an event and, for lifecycle events, update the version rectangles. Returns the event id."""  # noqa: E501
    if event not in LIFECYCLE and event not in ANNOTATIONS:
        raise ValueError(f"unknown event {event!r}")
    seq_row = conn.execute(
        "SELECT coalesce(max(seq), -1) + 1 FROM cairn.decision_events WHERE decision_id = %s",
        (decision_id,),
    ).fetchone()
    assert seq_row is not None  # noqa: S101
    row = conn.execute(
        "INSERT INTO cairn.decision_events (decision_id, seq, event, valid_at, recorded_at, source_item_id, "  # noqa: E501
        "source_ref, by_key, note, acl) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::int[]) RETURNING event_id",  # noqa: E501
        (
            decision_id,
            int(seq_row[0]),
            event,
            valid_at,
            recorded_at,
            source_item_id,
            source_ref,
            by_key,
            note,
            list(acl),
        ),
    ).fetchone()
    assert row is not None  # noqa: S101
    event_id = int(row[0])
    if event in LIFECYCLE:
        _apply_version(
            conn, decision_id, STATE_AFTER[event], valid_at, recorded_at, event_id, list(acl)
        )
    return event_id


def _apply_version(
    conn: Conn,
    decision_id: int,
    status: str,
    valid_at: datetime,
    recorded_at: datetime,
    event_id: int,
    acl: list[int],
) -> None:
    last = _latest_recorded(conn, decision_id)
    if last is not None:
        if recorded_at < last:
            raise OutOfOrder(f"recorded {recorded_at.isoformat()} is before {last.isoformat()}")
        if recorded_at <= last:
            recorded_at = last + MICRO
    cur = conn.execute(
        "SELECT version_id, status, lower(valid_period), upper(valid_period), event_id, acl, lower(recorded_period) "  # noqa: E501
        "FROM cairn.decision_versions WHERE decision_id = %s AND upper_inf(recorded_period) "
        "AND valid_period @> %s::timestamptz FOR UPDATE",
        (decision_id, valid_at),
    ).fetchone()
    new_acl = acl
    if cur is not None:
        vid, old_status, a, b, old_event, old_acl, rec_lo = cur
        if old_status == status:
            return  # nothing changes in what held
        conn.execute(
            "UPDATE cairn.decision_versions SET recorded_period = tstzrange(%s, %s, '[)') WHERE version_id = %s",  # noqa: E501
            (rec_lo, recorded_at, vid),
        )
        if a < valid_at:
            _insert(
                conn,
                decision_id,
                old_status,
                a,
                valid_at,
                recorded_at,
                int(old_event),
                list(old_acl),
            )
        _insert(conn, decision_id, status, valid_at, b, recorded_at, event_id, new_acl)
        return
    # Before the earliest open row, or no row yet.
    first = conn.execute(
        "SELECT min(lower(valid_period)) FROM cairn.decision_versions WHERE decision_id = %s AND upper_inf(recorded_period)",  # noqa: E501
        (decision_id,),
    ).fetchone()
    _insert(
        conn,
        decision_id,
        status,
        valid_at,
        first[0] if first and first[0] else None,
        recorded_at,
        event_id,
        new_acl,
    )


def _insert(
    conn: Conn,
    decision_id: int,
    status: str,
    lo: datetime,
    hi: datetime | None,
    recorded_at: datetime,
    event_id: int,
    acl: list[int],
) -> None:
    conn.execute(
        "INSERT INTO cairn.decision_versions (decision_id, status, valid_period, recorded_period, event_id, acl) "  # noqa: E501
        "VALUES (%s, %s, tstzrange(%s, %s, '[)'), tstzrange(%s, NULL, '[)'), %s, %s::int[])",
        (decision_id, status, lo, hi, recorded_at, event_id, acl),
    )


def rebuild_versions(conn: Conn, decision_id: int) -> int:
    """Recompute the version rectangles of one decision from its remaining events.

    Used after a source is deleted (plan 3.11): the events that came from it are removed, so the
    derived rectangles are dropped and replayed in recorded order. Returns the lifecycle events
    replayed. Versions are derived data, never the record of what was said.
    """
    conn.execute("DELETE FROM cairn.decision_versions WHERE decision_id = %s", (decision_id,))
    rows = conn.execute(
        "SELECT event_id, event, valid_at, recorded_at, acl FROM cairn.decision_events "
        "WHERE decision_id = %s ORDER BY recorded_at, seq",
        (decision_id,),
    ).fetchall()
    replayed = 0
    for event_id, event, valid_at, recorded_at, acl in rows:
        if event in LIFECYCLE:
            _apply_version(
                conn,
                decision_id,
                STATE_AFTER[event],
                valid_at,
                recorded_at,
                int(event_id),
                list(acl),
            )
            replayed += 1
    return replayed


def correct_version(conn: Conn, version_id: int, new_status: str, recorded_at: datetime) -> int:
    """Cairn recorded the wrong status: close its recorded period and re-insert it right.

    Valid time is unchanged and the wrong row stays visible to earlier recorded times.
    """
    row = conn.execute(
        "SELECT decision_id, valid_period, lower(recorded_period), event_id, acl FROM cairn.decision_versions "  # noqa: E501
        "WHERE version_id = %s AND upper_inf(recorded_period) FOR UPDATE",
        (version_id,),
    ).fetchone()
    if row is None:
        raise ValueError("version is not currently recorded")
    decision_id, valid, rec_lo, event_id, acl = row
    if recorded_at <= rec_lo:
        recorded_at = rec_lo + MICRO
    conn.execute(
        "UPDATE cairn.decision_versions SET recorded_period = tstzrange(%s, %s, '[)') WHERE version_id = %s",  # noqa: E501
        (rec_lo, recorded_at, version_id),
    )
    _insert(
        conn,
        int(decision_id),
        new_status,
        valid.lower,
        valid.upper,
        recorded_at,
        int(event_id),
        list(acl),
    )
    new = conn.execute(
        "SELECT max(version_id) FROM cairn.decision_versions WHERE decision_id = %s", (decision_id,)
    ).fetchone()
    return int(new[0]) if new else 0


def supersede(
    conn: Conn,
    old_id: int,
    new_id: int,
    effective: datetime,
    recorded_at: datetime,
    acl: Sequence[int],
    *,
    by_key: str,
    source_item_id: int | None = None,
    source_ref: str | None = None,
) -> None:
    """The old decision is superseded from ``effective``; a ``supersedes`` link is recorded."""
    add_event(
        conn,
        old_id,
        "superseded",
        effective,
        recorded_at,
        acl,
        source_item_id=source_item_id,
        source_ref=source_ref,
        by_key=by_key,
    )
    conn.execute(
        "INSERT INTO cairn.decision_links (from_decision, to_decision, kind, recorded_at) "
        "VALUES (%s, %s, 'supersedes', %s) ON CONFLICT DO NOTHING",
        (new_id, old_id, recorded_at),
    )


def add_link(conn: Conn, from_id: int, to_id: int, kind: str, recorded_at: datetime) -> None:
    conn.execute(
        "INSERT INTO cairn.decision_links (from_decision, to_decision, kind, recorded_at) VALUES (%s, %s, %s, %s) "  # noqa: E501
        "ON CONFLICT DO NOTHING",
        (from_id, to_id, kind, recorded_at),
    )


def add_anchor(conn: Conn, decision_id: int, kind: str, value: str) -> None:
    conn.execute(
        "INSERT INTO cairn.anchors (decision_id, kind, value) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",  # noqa: E501
        (decision_id, kind, value),
    )


def set_claim(
    conn: Conn,
    decision_id: int,
    key: str,
    text: str,
    tier: str,
    support_sets: Sequence[Sequence[tuple[int, Sequence[int], str | None]]],
) -> int:
    """Create or replace a claim and its support sets: ``[[(item_id, item_acl, source_ref), ...], ...]``."""  # noqa: E501
    row = conn.execute(
        "INSERT INTO cairn.claims (decision_id, key, text, tier) VALUES (%s, %s, %s, %s) "
        "ON CONFLICT (decision_id, key) DO UPDATE SET text = EXCLUDED.text, tier = EXCLUDED.tier RETURNING claim_id",  # noqa: E501
        (decision_id, key, text, tier),
    ).fetchone()
    assert row is not None  # noqa: S101
    claim_id = int(row[0])
    conn.execute("DELETE FROM cairn.claim_support WHERE claim_id = %s", (claim_id,))
    for n, members in enumerate(support_sets):
        for item_id, item_acl, ref in members:
            conn.execute(
                "INSERT INTO cairn.claim_support (claim_id, support_set, item_id, item_acl, source_ref) "  # noqa: E501
                "VALUES (%s, %s, %s, %s::int[], %s) ON CONFLICT DO NOTHING",
                (claim_id, n, item_id, list(item_acl), ref),
            )
    return claim_id


# --- lineage and invalidation (plan 2.12) -----------------------------------------------------


def record_lineage(
    conn: Conn, kind: str, artifact_id: str, inputs: Iterable[tuple[int, str]]
) -> None:
    for item_id, h in inputs:
        conn.execute(
            "INSERT INTO cairn.lineage (artifact_kind, artifact_id, input_item_id, input_hash) VALUES (%s, %s, %s, %s) "  # noqa: E501
            "ON CONFLICT (artifact_kind, artifact_id, input_item_id) DO UPDATE SET input_hash = EXCLUDED.input_hash",  # noqa: E501
            (kind, artifact_id, item_id, h),
        )


def mark_stale(conn: Conn, item_id: int) -> int:
    """An input changed, was deleted or changed ACL: every artifact derived from it is stale and is
    rebuilt on the next read. Returns how many cached artifacts were marked."""
    cur = conn.execute(
        "UPDATE cairn.derived_artifacts d SET stale = true WHERE EXISTS (SELECT 1 FROM cairn.lineage l "  # noqa: E501
        "WHERE l.input_item_id = %s AND l.artifact_kind = d.artifact_kind AND l.artifact_id = d.artifact_id)",  # noqa: E501
        (item_id,),
    )
    return cur.rowcount


def refresh_acl_copies(conn: Conn, item_id: int, acl: Sequence[int]) -> None:
    """Keep the ACL copies of decision tables equal to the item's ACL (like chunks, Property 9)."""
    a = list(acl)
    conn.execute(
        "UPDATE cairn.claim_support SET item_acl = %s::int[] WHERE item_id = %s", (a, item_id)
    )
    conn.execute(
        "UPDATE cairn.decision_events SET acl = %s::int[] WHERE source_item_id = %s", (a, item_id)
    )
    conn.execute(
        "UPDATE cairn.decision_versions v SET acl = %s::int[] FROM cairn.decision_events e "
        "WHERE v.event_id = e.event_id AND e.source_item_id = %s",
        (a, item_id),
    )
