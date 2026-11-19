"""Phase 2: the bitemporal store agrees with the scenario oracle, and visibility is enforced (plan 2.24)."""  # noqa: E501

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest

from cairn_connectors.decisions import store
from cairn_core.testing import FreshDb
from cairn_eval.scenario.oracle import is_lifecycle, recorded_time, state_as_of, valid_time
from cairn_eval.seed import SeededCorpus
from cairn_retrieval.temporal import day_end


def _state(conn: Any, key: str, valid: dt.date, recorded: dt.date) -> str:
    row = conn.execute(
        "SELECT v.status FROM cairn.decision_versions v JOIN cairn.decisions d USING (decision_id) "
        "WHERE d.key = %s AND v.valid_period @> %s::timestamptz AND v.recorded_period @> %s::timestamptz",  # noqa: E501
        (key, day_end(valid), day_end(recorded)),
    ).fetchone()
    return str(row[0]) if row else "absent"


def test_store_matches_the_oracle_on_every_date_pair(
    decisions: tuple[FreshDb, SeededCorpus],
) -> None:
    db, corpus = decisions
    sc = corpus.scenario
    mismatches: list[tuple[str, dt.date, dt.date, str, str]] = []
    with db.connect_ingest() as conn:
        for d in sc.decisions:
            life = [e for e in d.timeline if is_lifecycle(e)]
            if not life:
                continue
            days = sorted({valid_time(e) for e in life} | {recorded_time(e, sc) for e in life})
            probes = {x + dt.timedelta(days=k) for x in days for k in (-1, 0, 1)}
            for valid in sorted(probes):
                for rec in sorted(probes):
                    want, got = state_as_of(d, valid, rec, sc), _state(conn, d.id, valid, rec)
                    if want != got:
                        mismatches.append((d.id, valid, rec, want, got))
    assert not mismatches, mismatches[:10]


def test_versions_never_overlap_on_both_axes(decisions: tuple[FreshDb, SeededCorpus]) -> None:
    db, _ = decisions
    with db.connect_ingest() as conn:
        row = conn.execute(
            "SELECT count(*) FROM cairn.decision_versions a JOIN cairn.decision_versions b "
            "ON a.decision_id = b.decision_id AND a.version_id < b.version_id "
            "AND a.valid_period && b.valid_period AND a.recorded_period && b.recorded_period"
        ).fetchone()
    assert row is not None
    assert row[0] == 0


def test_exclusion_constraint_rejects_an_overlap(decisions: tuple[FreshDb, SeededCorpus]) -> None:
    import psycopg

    db, _ = decisions
    with db.connect_ingest() as conn:
        did = store.ensure_decision(conn, "X-OVERLAP", "overlap probe")
        with pytest.raises(psycopg.errors.ExclusionViolation):  # noqa: PT012
            for status in ("proposed", "accepted"):
                conn.execute(
                    "INSERT INTO cairn.decision_versions "
                    "(decision_id, status, valid_period, recorded_period, event_id, acl) "
                    "VALUES (%s, %s, tstzrange('2026-01-01', '2026-02-01'), tstzrange('2026-01-01', NULL), 0, '{1}')",  # noqa: E501
                    (did, status),
                )
        conn.rollback()


def test_backdating_and_correction_keep_history(decisions: tuple[FreshDb, SeededCorpus]) -> None:
    db, _ = decisions
    t = store.day_start
    with db.connect_ingest() as conn:
        did = store.ensure_decision(conn, "X-BACKDATE", "backdate probe")
        store.add_event(
            conn, did, "accepted", t(dt.date(2026, 3, 10)), t(dt.date(2026, 3, 10)), [1]
        )
        store.add_event(
            conn, did, "proposed", t(dt.date(2026, 3, 1)), t(dt.date(2026, 3, 20)), [1]
        )  # learned late
        assert _state(conn, "X-BACKDATE", dt.date(2026, 3, 5), dt.date(2026, 3, 15)) == "absent"
        assert _state(conn, "X-BACKDATE", dt.date(2026, 3, 5), dt.date(2026, 3, 25)) == "proposed"
        vid = conn.execute(
            "SELECT version_id FROM cairn.decision_versions WHERE decision_id = %s AND upper_inf(recorded_period) "  # noqa: E501
            "AND valid_period @> %s::timestamptz",
            (did, t(dt.date(2026, 3, 12))),
        ).fetchone()
        assert vid is not None
        store.correct_version(conn, int(vid[0]), "implemented", t(dt.date(2026, 4, 1)))
        assert (
            _state(conn, "X-BACKDATE", dt.date(2026, 3, 12), dt.date(2026, 3, 30)) == "accepted"
        )  # old belief kept
        assert (
            _state(conn, "X-BACKDATE", dt.date(2026, 3, 12), dt.date(2026, 4, 2)) == "implemented"
        )
        conn.commit()


def test_a_member_sees_decisions_and_claims_only_through_full_support_sets(
    decisions: tuple[FreshDb, SeededCorpus],
) -> None:
    import asyncio

    from psycopg_pool import AsyncConnectionPool

    from cairn_retrieval.access import principal_transaction
    from cairn_retrieval.decisions import service

    db, corpus = decisions
    with db.connect_owner() as admin:
        pid = {
            r[0]: int(r[1])
            for r in admin.execute("SELECT external_id, principal_id FROM cairn.principals")
        }
    persons = [e for e in pid if e.startswith("user:")]
    assert persons

    async def run() -> dict[str, tuple[Any, list[str]]]:
        pool: AsyncConnectionPool[Any] = AsyncConnectionPool(
            db.app_dsn, min_size=1, max_size=2, kwargs={"prepare_threshold": None}, open=False
        )
        await pool.open()
        out: dict[str, tuple[Any, list[str]]] = {}
        try:
            for ext in persons:
                async with principal_transaction(pool, pid[ext], read_only=True) as (conn, snap):
                    st = await service.state_as_of(
                        conn, snap, "ADR-007", dt.date(2026, 3, 10), None
                    )
                    claims = [c["key"] for c in await service.visible_claims(conn, snap, "ADR-007")]
                    out[ext] = (st, claims)
        finally:
            await pool.close()
        return out

    seen = asyncio.run(run())
    adr7 = next(d for d in corpus.scenario.decisions if d.id == "ADR-007")
    all_keys = {c.id for c in adr7.claims}
    assert any(st is not None for st, _ in seen.values())
    for ext, (st, claims) in seen.items():  # noqa: B007
        assert set(claims) <= all_keys
        if st is None:
            assert claims == []  # a hidden decision exposes nothing


# --- projection, summaries, lineage (plan 2.10, 2.11, 2.12) ----------------------------------


def _as(db: FreshDb, persona: str, fn: Any) -> Any:
    """Run ``fn(conn, snapshot)`` as the persona's principal through the app role."""
    import asyncio

    from psycopg_pool import AsyncConnectionPool

    from cairn_retrieval.access import principal_transaction

    with db.connect_owner() as c:
        row = c.execute(
            "SELECT principal_id FROM cairn.principals WHERE external_id = %s", (f"user:{persona}",)
        ).fetchone()
    assert row is not None

    async def run() -> Any:
        pool: AsyncConnectionPool[Any] = AsyncConnectionPool(
            db.app_dsn, min_size=1, max_size=2, kwargs={"prepare_threshold": None}, open=False
        )
        await pool.open()
        try:
            async with principal_transaction(pool, int(row[0])) as (conn, snap):
                return await fn(conn, snap)
        finally:
            await pool.close()

    return asyncio.run(run())


def test_projection_hides_the_private_incident_claim_from_non_members(
    decisions: tuple[FreshDb, SeededCorpus],
) -> None:
    from cairn_retrieval.decisions import service

    db, _ = decisions

    async def claims(conn: Any, snap: Any) -> list[str] | None:
        if await service.get_decision(conn, snap, "ADR-007") is None:
            return None
        return [c["key"] for c in await service.visible_claims(conn, snap, "ADR-007")]

    asha = _as(db, "asha", claims)  # on the payments team, so a member of the incident channel
    assert asha is not None
    assert "C2" in asha
    carla = _as(db, "carla", claims)  # HR only
    assert carla is None or "C2" not in carla
    # Ben moved to payments on 2026-09-01, so today he is a member: the projection follows membership.  # noqa: E501
    ben = _as(db, "ben", claims)
    assert ben is not None
    assert {"C1", "C3"} <= set(ben)


def test_hidden_claim_text_never_appears_in_a_summary(
    decisions: tuple[FreshDb, SeededCorpus],
) -> None:
    from cairn_retrieval.decisions import service

    db, corpus = decisions
    c2 = next(
        c.text
        for d in corpus.scenario.decisions
        if d.id == "ADR-007"
        for c in d.claims
        if c.id == "C2"
    )

    async def summary(conn: Any, snap: Any) -> dict[str, Any] | None:
        return await service.summary(conn, snap, "ADR-007")

    asha = _as(db, "asha", summary)
    assert asha is not None
    assert c2 in asha["text"]
    assert "Status:" in asha["text"]
    carla = _as(db, "carla", summary)
    assert carla is None or c2 not in carla["text"]
    # A cached summary of one principal set is never served to another.
    assert asha["text"] != (carla or {"text": ""})["text"]


def test_summary_is_cached_per_principal_set_and_rebuilt_when_marked_stale(
    decisions: tuple[FreshDb, SeededCorpus],
) -> None:
    from cairn_retrieval.decisions import service

    db, _ = decisions

    async def summary(conn: Any, snap: Any) -> dict[str, Any] | None:
        return await service.summary(conn, snap, "ADR-007")

    first = _as(db, "ben", summary)
    second = _as(db, "ben", summary)
    assert first is not None
    assert second is not None
    assert second["cached"] is True
    with db.connect_ingest() as conn:
        item = conn.execute(
            "SELECT input_item_id FROM cairn.lineage WHERE artifact_kind = 'decision-summary' "
            "AND artifact_id = 'ADR-007' LIMIT 1"
        ).fetchone()
        assert item is not None
        assert store.mark_stale(conn, int(item[0])) >= 1
    third = _as(db, "ben", summary)
    assert third is not None
    assert third["cached"] is False
    assert _as(db, "ben", summary)["cached"] is True


def test_acl_copy_refresh_follows_an_item_acl_change(
    decisions: tuple[FreshDb, SeededCorpus],
) -> None:
    db, _ = decisions
    with db.connect_ingest() as conn:
        row = conn.execute(
            "SELECT item_id, item_acl FROM cairn.claim_support WHERE claim_id = "
            "(SELECT claim_id FROM cairn.claims WHERE key = 'C3' LIMIT 1) LIMIT 1"
        ).fetchone()
        assert row is not None
        item_id, original = int(row[0]), list(row[1])
        store.refresh_acl_copies(conn, item_id, [original[0]])
        changed = conn.execute(
            "SELECT item_acl FROM cairn.claim_support WHERE item_id = %s", (item_id,)
        ).fetchall()
        assert all(list(r[0]) == [original[0]] for r in changed)
        store.refresh_acl_copies(conn, item_id, original)


def test_editing_a_cited_document_marks_the_summary_stale_through_the_real_import_path(
    decisions: tuple[FreshDb, SeededCorpus],
) -> None:
    from datetime import UTC, datetime

    from cairn_connectors.documents import AclRef, SourceDocument
    from cairn_connectors.store import import_document
    from cairn_retrieval.decisions import service

    db, _ = decisions

    async def summary(conn: Any, snap: Any) -> dict[str, Any] | None:
        return await service.summary(conn, snap, "ADR-007")

    assert _as(db, "asha", summary) is not None
    assert _as(db, "asha", summary)["cached"] is True
    with db.connect_ingest() as conn:
        conn.autocommit = False
        row = conn.execute(
            "SELECT i.connector, i.external_id, i.source_kind, i.title, i.acl, "
            "(SELECT r.text FROM cairn.revisions r WHERE r.item_id = i.item_id ORDER BY revision_id DESC LIMIT 1) "  # noqa: E501
            "FROM cairn.items i WHERE i.item_id IN (SELECT input_item_id FROM cairn.lineage "
            "WHERE artifact_kind = 'decision-summary' AND artifact_id = 'ADR-007') AND i.source_kind = 'doc' LIMIT 1"  # noqa: E501
        ).fetchone()
        assert row is not None
        connector, external, kind, title, acl_ids, text = row
        names = [
            r[0]
            for r in conn.execute(
                "SELECT external_id FROM cairn.principals WHERE principal_id = ANY(%s)",
                (list(acl_ids),),
            ).fetchall()
        ]
        refs = frozenset(AclRef(n.split(":", 1)[0], n.split(":", 1)[1]) for n in names)
        outcome = import_document(
            conn,
            SourceDocument(
                connector,
                external,
                kind,
                title,
                text + "\n\nEdited after the summary.",
                datetime.now(UTC),
                refs,
                {},
            ),
        )
        conn.commit()
    assert outcome.error is None
    assert outcome.revision is not None
    assert _as(db, "asha", summary)["cached"] is False  # rebuilt on the next read
    assert _as(db, "asha", summary)["cached"] is True


def test_state_is_derived_from_visible_events_only_for_every_persona(
    decisions: tuple[FreshDb, SeededCorpus],
) -> None:
    """The read path equals the oracle run on the events the persona may see; hidden events change
    neither the status nor its boundaries."""
    from cairn_retrieval.decisions import service

    db, corpus = decisions
    sc = corpus.scenario
    with db.connect_owner() as c:
        groups_of = {}
        for ext in ("asha", "ben", "carla", "priya"):
            rows = c.execute(
                "SELECT p.principal_id FROM cairn.principals p WHERE p.external_id IN ('group:public', %s) "  # noqa: E501
                "UNION SELECT group_id FROM cairn.memberships m JOIN cairn.principals u ON u.principal_id = m.member_id "  # noqa: E501
                "WHERE u.external_id = %s AND m.valid_period @> now()",
                (f"user:{ext}", f"user:{ext}"),
            ).fetchall()
            groups_of[ext] = {int(r[0]) for r in rows}
        stored = {
            (r[0], r[1], r[2]): set(r[3])
            for r in c.execute(
                "SELECT d.key, e.event, e.source_ref, e.acl FROM cairn.decision_events e "
                "JOIN cairn.decisions d USING (decision_id)"
            ).fetchall()
        }

    checked = 0
    for persona, groups in groups_of.items():
        for d in sc.decisions:
            visible = [
                e
                for e in d.timeline
                if stored.get((d.id, e.event, e.source))
                and stored[(d.id, e.event, e.source)] & groups
            ]
            life = [e for e in visible if is_lifecycle(e)]
            days = sorted({valid_time(e) for e in life} | {recorded_time(e, sc) for e in life}) or [
                dt.date(2026, 6, 1)
            ]
            probes = sorted({x + dt.timedelta(days=k) for x in days for k in (-1, 0, 1)})
            want_dec = d.model_copy(update={"timeline": visible})

            async def got_for(
                conn: Any, snap: Any, d_id: str = d.id, ps: list[dt.date] = probes
            ) -> Any:
                if await service.get_decision(conn, snap, d_id) is None:
                    return None
                return {
                    (v, r): (await service.state_as_of(conn, snap, d_id, v, r)).status  # type: ignore[union-attr]
                    for v in ps
                    for r in ps
                }

            got = _as(db, persona, got_for)
            if not visible:
                assert got is None, (
                    persona,
                    d.id,
                )  # no visible event: the decision does not exist for them
                continue
            assert got is not None, (persona, d.id)
            for (v, r), status in got.items():
                assert status == state_as_of(want_dec, v, r, sc), (persona, d.id, v, r)
                checked += 1
    assert checked > 500


def test_adjacent_ranges_are_accepted_and_overlaps_are_not(
    decisions: tuple[FreshDb, SeededCorpus],
) -> None:
    import psycopg

    db, _ = decisions
    sql = (
        "INSERT INTO cairn.decision_versions "
        "(decision_id, status, valid_period, recorded_period, event_id, acl) "
        "VALUES (%s, %s, tstzrange(%s, %s, '[)'), tstzrange('2026-01-01', NULL, '[)'), 0, '{1}')"
    )
    with db.connect_ingest() as conn:
        did = store.ensure_decision(conn, "X-ADJACENT", "adjacent probe")
        conn.execute(sql, (did, "proposed", "2026-01-01", "2026-02-01"))
        conn.execute(
            sql, (did, "accepted", "2026-02-01", "2026-03-01")
        )  # touches, does not overlap
        conn.execute(sql, (did, "implemented", "2026-03-01", None))
        with pytest.raises(psycopg.errors.ExclusionViolation):
            conn.execute(sql, (did, "reverted", "2026-02-15", "2026-02-20"))


def test_supersede_records_the_status_and_the_link(decisions: tuple[FreshDb, SeededCorpus]) -> None:
    db, _ = decisions
    t = store.day_start
    with db.connect_ingest() as conn:
        old = store.ensure_decision(conn, "X-OLD", "old")
        new = store.ensure_decision(conn, "X-NEW", "new")
        store.add_event(conn, old, "accepted", t(dt.date(2026, 5, 1)), t(dt.date(2026, 5, 1)), [1])
        store.supersede(
            conn,
            old,
            new,
            t(dt.date(2026, 6, 1)),
            t(dt.date(2026, 6, 10)),
            [1],
            by_key="X-NEW",
            source_ref="s",
        )
        assert _state(conn, "X-OLD", dt.date(2026, 5, 15), dt.date(2026, 7, 1)) == "accepted"
        assert (
            _state(conn, "X-OLD", dt.date(2026, 6, 5), dt.date(2026, 6, 5)) == "accepted"
        )  # not yet known
        assert _state(conn, "X-OLD", dt.date(2026, 6, 5), dt.date(2026, 6, 11)) == "superseded"
        link = conn.execute(
            "SELECT kind FROM cairn.decision_links WHERE from_decision = %s AND to_decision = %s",
            (new, old),
        ).fetchone()
        assert link == ("supersedes",)
