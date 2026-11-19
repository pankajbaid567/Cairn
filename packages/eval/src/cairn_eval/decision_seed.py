"""Load the Kestrel Pay gold decisions into the decision tables (plan 2.24).

The scenario already states each decision's timeline, claims, support sets and links. Extraction by
a model (S4) is a separate, user-run measurement; this loader is the deterministic path that makes
the as-of, projection and lineage behaviour testable and demo-able without a model.

Events are replayed in recorded order (ties by list order), exactly the order Cairn would have
learned them, so the bitemporal rectangles come out the same as in production. Evidence that is not
imported in Phase 1 (pull requests, issues, deleted documents) has no item to point at: its support
sets are skipped, so a claim whose only support is such a source is stored *unsupported* and no
principal sees it. That is the intended behaviour for a deleted source (TW-LEDGER-DOC-DELETED).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

import psycopg

from cairn_connectors.decisions import conflicts, store
from cairn_connectors.decisions.conflicts import Side
from cairn_eval.scenario.access import principal_members  # noqa: F401
from cairn_eval.scenario.model import PrincipalRef, Scenario, Source
from cairn_eval.scenario.oracle import is_lifecycle, recorded_time, valid_time

Conn = psycopg.Connection[Any]


@dataclass(frozen=True)
class DecisionSeedStats:
    decisions: int
    events: int
    claims: int
    supported_sets: int
    skipped_sets: int


def _scope_principals(scenario: Scenario, src: Source) -> list[str]:
    """Principal external ids that can read a source that has no imported item (fallback ACL)."""
    loc = src.location
    if loc.meeting is not None:
        out = [f"user:{a}" for a in loc.meeting.attendees]
        out += [_ref(r) for r in loc.meeting.share_with]
        return out
    if loc.channel is not None:
        ch = next(c for c in scenario.channels if c.id == loc.channel)
        return [f"channel:{ch.id}"] if ch.private else ["group:employees"]
    if loc.repo is not None:
        repo = next(r for r in scenario.repos if r.id == loc.repo)
        return ["group:public"] if repo.visibility == "public" else [_ref(g) for g in repo.access]
    return ["group:public"]


def _ref(r: PrincipalRef) -> str:
    return f"team:{r.team}" if r.team is not None else f"user:{r.person}"


def _principal_ids(conn: Conn, external: list[str]) -> list[int]:
    rows = conn.execute(
        "SELECT principal_id FROM cairn.principals WHERE external_id = ANY(%s)", (external,)
    ).fetchall()
    return sorted(int(r[0]) for r in rows)


def seed_decisions(conn: Conn, scenario: Scenario, manifest: dict[str, Any]) -> DecisionSeedStats:
    items: dict[str, tuple[int, list[int]]] = {}
    for sid, info in manifest["sources"].items():
        if info["status"] != "rendered" or not info.get("external_id"):
            continue
        row = conn.execute(
            "SELECT item_id, acl FROM cairn.items WHERE external_id = %s", (info["external_id"],)
        ).fetchone()
        if row:
            items[sid] = (int(row[0]), [int(x) for x in row[1]])

    def acl_of(src_id: str) -> list[int]:
        if src_id in items:
            return items[src_id][1]
        return _principal_ids(
            conn, _scope_principals(scenario, scenario.source(src_id))
        ) or _principal_ids(conn, ["group:public"])

    ids: dict[str, int] = {}
    for d in scenario.decisions:
        ids[d.id] = store.ensure_decision(
            conn,
            d.id,
            d.title,
            question=d.question,
            outcome=d.outcome,
            origin="scenario",
            verification="verified",
        )
    events = claims = good = skipped = 0
    for d in scenario.decisions:
        have = conn.execute(
            "SELECT count(*) FROM cairn.decision_events WHERE decision_id = %s", (ids[d.id],)
        ).fetchone()
        replay = have is None or have[0] == 0  # events are an append-only log: never replay them
        ordered = (
            []
            if not replay
            else sorted(enumerate(d.timeline), key=lambda p: (recorded_time(p[1], scenario), p[0]))
        )
        for _, ev in ordered:
            src = items.get(ev.source)
            store.add_event(
                conn,
                ids[d.id],
                ev.event,
                store.day_start(valid_time(ev)),
                store.day_start(recorded_time(ev, scenario)),
                acl_of(ev.source),
                source_item_id=src[0] if src else None,
                source_ref=ev.source,
                by_key=ev.by,
                note=ev.note,
            )
            events += 1
            if not is_lifecycle(ev):
                continue
        for claim in d.claims:
            sets = [
                [(items[s][0], items[s][1], s) for s in members]
                for members in claim.support_sets
                if all(s in items for s in members)
            ]
            skipped += len(claim.support_sets) - len(sets)
            good += len(sets)
            store.set_claim(conn, ids[d.id], claim.id, claim.text, claim.tier, sets)
            claims += 1
        a = d.anchors
        if a:
            for repo in a.repos:
                store.add_anchor(conn, ids[d.id], "repo", repo)
            for path in a.paths:
                store.add_anchor(conn, ids[d.id], "path", path)
            for pr in a.prs:
                store.add_anchor(conn, ids[d.id], "pr", pr)
        sources = {s for c in d.claims for ss in c.support_sets for s in ss} | {
            e.source for e in d.timeline
        }
        store.record_lineage(
            conn,
            "decision-summary",
            d.id,
            [(items[s][0], "") for s in sorted(sources) if s in items],
        )
    # Gold conflicts open review items; the decision turns disputed. Nothing is resolved.
    for d in scenario.decisions:
        for k in d.conflicts:
            sides: list[Side] = []
            for side in k.sides:
                if side.source not in items:
                    sides = []
                    break
                item_id, item_acl = items[side.source]
                sides.append(Side(side.value, side.tier, side.source, item_id, tuple(item_acl)))
            if len(sides) >= 2:
                conflicts.open_conflict(conn, ids[d.id], k.subject, sides)
    for d in scenario.decisions:
        later = max((recorded_time(e, scenario) for e in d.timeline), default=None)
        stamp = store.day_start(later) if later else datetime.now().astimezone()
        for other in d.supersedes:
            if other in ids:
                store.add_link(conn, ids[d.id], ids[other], "supersedes", stamp)
    conn.commit()
    return DecisionSeedStats(len(ids), events, claims, good, skipped)
