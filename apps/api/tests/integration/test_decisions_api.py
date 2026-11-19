"""Phase 2 HTTP surface: decision page, timeline, as-of parameters (plan 2.9, 2.15, 2.16, 2.18)."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from cairn_core.testing import FreshDb
from cairn_eval.seed import SeededCorpus

from .conftest import login, make_app_client


@pytest.fixture(scope="module")
def dclient(decisions: tuple[FreshDb, SeededCorpus]) -> Iterator[TestClient]:
    yield from make_app_client(decisions[0])


def test_unauthenticated_is_401(dclient: TestClient) -> None:
    dclient.cookies.clear()
    assert dclient.get("/decisions/ADR-007").status_code == 401


def test_unknown_and_malformed_keys_are_404(dclient: TestClient) -> None:
    login(dclient, "asha")
    assert dclient.get("/decisions/ADR-999").status_code == 404
    assert dclient.get("/decisions/..%2Fetc").status_code == 404


def test_bad_dates_are_422(dclient: TestClient) -> None:
    login(dclient, "asha")
    assert dclient.get("/decisions/ADR-007?valid=yesterday").status_code == 422
    assert dclient.get("/decisions/ADR-007?hops=9").status_code == 422


def test_decision_page_has_status_claims_timeline_and_is_audited(dclient: TestClient) -> None:
    login(dclient, "asha")
    r = dclient.get("/decisions/ADR-007")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "implemented"
    assert body["audit_id"]
    assert {c["key"] for c in body["claims"]} >= {"C1", "C2", "C3"}
    assert [e["event"] for e in body["timeline"]][:3] == ["proposed", "discussed", "accepted"]


def test_as_of_parameters_return_the_status_at_that_time(dclient: TestClient) -> None:
    login(dclient, "asha")
    got = {
        d: dclient.get(f"/decisions/ADR-007?valid={d}").json()["status"]
        for d in ("2026-03-01", "2026-03-04", "2026-03-08", "2026-03-12")
    }
    assert got == {
        "2026-03-01": "absent",
        "2026-03-04": "proposed",
        "2026-03-08": "accepted",
        "2026-03-12": "implemented",
    }


def test_flag_vendor_backdating_two_beliefs(dclient: TestClient) -> None:
    """ADR-014: effective 1 Jun, recorded 10 Jun. On 5 Jun, as known today it is accepted; as
    believed on 8 Jun it was still proposed (the ADR-004 supersession was not yet known)."""
    login(dclient, "asha")
    today = dclient.get("/decisions/ADR-014?valid=2026-06-05").json()["status"]
    then = dclient.get("/decisions/ADR-014?valid=2026-06-05&recorded=2026-06-08").json()["status"]
    assert (today, then) == ("accepted", "proposed")


def test_timeline_endpoint_respects_the_recorded_time(dclient: TestClient) -> None:
    login(dclient, "asha")
    full = dclient.get("/decisions/ADR-007/timeline").json()["timeline"]
    early = dclient.get("/decisions/ADR-007/timeline?recorded=2026-03-04").json()["timeline"]
    assert len(early) < len(full)
    assert [e["event"] for e in early] == ["proposed"]


def test_a_non_member_gets_the_same_404_as_a_missing_decision(dclient: TestClient) -> None:
    login(dclient, "carla")
    hidden = [
        k
        for k in ("ADR-007", "ADR-012", "ADR-014", "ADR-018", "ADR-009")
        if dclient.get(f"/decisions/{k}").status_code == 404
    ]
    assert hidden, "expected at least one decision hidden from an HR-only reader"
    missing = dclient.get("/decisions/ADR-999")
    for k in hidden:
        r = dclient.get(f"/decisions/{k}")
        assert r.json() == missing.json()
        assert r.status_code == missing.status_code


def test_links_follow_supersession(dclient: TestClient) -> None:
    login(dclient, "asha")
    body = dclient.get("/decisions/ADR-012?hops=2").json()
    assert any(link["kind"] == "supersedes" for link in body["links"])


def test_as_of_search_never_returns_evidence_recorded_later(
    decisions: tuple[FreshDb, SeededCorpus],
) -> None:
    """Q42 shape: for any cutoff, every returned item's current revision predates the cutoff."""
    import asyncio
    from datetime import UTC, datetime

    from psycopg_pool import AsyncConnectionPool

    from cairn_retrieval.models.fake import FakeEmbedder, FakeReranker
    from cairn_retrieval.pipeline import search as run_search
    from cairn_retrieval.temporal import day_end

    db, _ = decisions
    with db.connect_owner() as c:
        row = c.execute(
            "SELECT principal_id FROM cairn.principals WHERE external_id = 'user:asha'"
        ).fetchone()
        assert row is not None
        pid = int(row[0])
        dated = {
            int(r[0]): r[1]
            for r in c.execute(
                "SELECT DISTINCT ON (item_id) item_id, source_updated_at FROM cairn.revisions "
                "ORDER BY item_id, revision_id DESC"
            ).fetchall()
        }

    async def run(cutoff: datetime | None) -> list[int]:
        pool: AsyncConnectionPool[Any] = AsyncConnectionPool(
            db.app_dsn, min_size=1, max_size=2, kwargs={"prepare_threshold": None}, open=False
        )
        await pool.open()
        try:
            res = await run_search(
                pool,
                FakeEmbedder(),
                FakeReranker(),
                principal_id=pid,
                query="payment-service PostgreSQL migration transactions refunds",
                recorded_before=cutoff,
            )
            return [c.item_id for c in res.ranked]
        finally:
            await pool.close()

    import datetime as dt

    sizes = []
    for day in (
        dt.date(2000, 1, 1),
        dt.date(2026, 3, 5),
        dt.date(2026, 3, 10),
        dt.date(2026, 6, 1),
    ):
        cutoff = day_end(day)
        items = asyncio.run(run(cutoff))
        sizes.append(len(items))
        assert all(dated[i] <= cutoff for i in items), (
            day,
            [i for i in items if dated[i] > cutoff],
        )
    assert sizes[0] == 0, sizes
    assert sizes[-1] > 0, sizes
    assert sizes == sorted(sizes), sizes
    assert asyncio.run(run(None))  # without a cutoff the same query finds evidence
    assert datetime.now(UTC)


def test_as_of_values_are_validated(dclient: TestClient) -> None:
    login(dclient, "asha")
    assert dclient.post("/search", json={"query": "x", "recorded": "last week"}).status_code == 422
    assert dclient.post("/answer", json={"question": "x", "recorded": "soon"}).status_code == 422


def _all_events(db: FreshDb, key: str) -> list[dict[str, object]]:
    with db.connect_owner() as c:
        cur = c.execute(
            "SELECT e.event, e.valid_at, e.recorded_at, e.seq FROM cairn.decision_events e "
            "JOIN cairn.decisions d USING (decision_id) WHERE d.key = %s",
            (key,),
        )
        cols = [x.name for x in cur.description or []]
        return [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]


def test_adr012_superseded_by_adr018_and_adr009_partial_revert_at_four_dates(
    decisions: tuple[FreshDb, SeededCorpus],
) -> None:
    import datetime as dt

    from cairn_retrieval.decisions.service import lifecycle_state
    from cairn_retrieval.temporal import day_end

    db, _ = decisions
    cases = {
        "ADR-012": [
            ("2026-01-15", "proposed"),
            ("2026-02-01", "accepted"),
            ("2026-03-10", "implemented"),
            ("2026-07-20", "superseded"),
        ],
        "ADR-018": [
            ("2026-06-01", "absent"),
            ("2026-07-01", "proposed"),
            ("2026-07-16", "accepted"),
            ("2026-08-21", "implemented"),
        ],
        # partially_reverted is an annotation: the status stays implemented after 20 May
        "ADR-009": [
            ("2026-03-30", "absent"),
            ("2026-04-05", "accepted"),
            ("2026-05-01", "implemented"),
            ("2026-05-25", "implemented"),
        ],
    }
    now = day_end(dt.date(2027, 1, 1))
    for key, rows in cases.items():
        events = _all_events(db, key)
        for day, want in rows:
            got = lifecycle_state(events, day_end(dt.date.fromisoformat(day)), now)
            assert got.status == want, (key, day, got)


def test_partial_revert_is_on_the_timeline_but_not_a_state_change(
    decisions: tuple[FreshDb, SeededCorpus],
) -> None:
    import datetime as dt

    from cairn_retrieval.decisions.service import lifecycle_state
    from cairn_retrieval.temporal import day_end

    db, _ = decisions
    events = _all_events(db, "ADR-009")
    assert "partially_reverted" in {e["event"] for e in events}
    assert (
        lifecycle_state(events, day_end(dt.date(2026, 6, 1)), day_end(dt.date(2027, 1, 1))).status
        == "implemented"
    )


def test_canary_matrix_covers_decision_page_timeline_and_as_of_paths(
    dclient: TestClient, decisions: tuple[FreshDb, SeededCorpus]
) -> None:
    """Plan 2.20: no canary a persona may not read appears on any decision path, and no hidden
    source id leaks through a timeline; positive controls prove the paths return data."""
    from .test_canary_matrix_v1 import PERSONAS, _canary_cases

    corpus = decisions[1]
    cases = _canary_cases(corpus)
    keys = [d.id for d in corpus.scenario.decisions]
    variants = [
        "",
        "?valid=2026-03-04",
        "?valid=2026-06-05&recorded=2026-06-08",
        "?valid=2026-08-01&hops=3",
    ]
    cells = answered = 0
    for persona in PERSONAS:
        login(dclient, persona)
        for key in keys:
            for suffix in variants:
                r = dclient.get(f"/decisions/{key}{suffix}")
                cells += 1
                assert r.status_code in (200, 404), (persona, key, suffix, r.status_code)
                answered += r.status_code == 200
                for cid, needle, _family, who, _ext in cases:
                    if persona not in who:
                        assert needle not in r.text, f"LEAK {cid} to {persona} via {key}{suffix}"
            t = dclient.get(f"/decisions/{key}/timeline")
            for cid, needle, _family, who, _ext in cases:
                if persona not in who:
                    assert needle not in t.text, f"LEAK {cid} to {persona} via {key} timeline"
    assert cells == len(PERSONAS) * len(keys) * len(variants)
    assert answered > cells // 4, "decision paths returned too little data to be a meaningful check"


def test_decision_index_searches_visible_decisions_and_anchors(dclient: TestClient) -> None:
    login(dclient, "asha")
    r = dclient.get("/decisions", params={"q": "postgresql"})
    assert r.status_code == 200
    assert "ADR-007" in {d["key"] for d in r.json()["decisions"]}
    by_anchor = dclient.get("/decisions", params={"anchor": "payment-service"})
    assert "ADR-007" in {d["key"] for d in by_anchor.json()["decisions"]}
    assert dclient.get("/decisions").status_code == 422
    assert dclient.get("/decisions", params={"q": "100%_"}).json() == {
        "decisions": []
    }  # metacharacters are literal
    login(dclient, "carla")
    hidden = dclient.get("/decisions", params={"q": "ADR-007"}).json()["decisions"]
    visible_to_carla = dclient.get("/decisions/ADR-007").status_code == 200
    assert bool(hidden) == visible_to_carla  # the index never shows what the page would hide


def test_claim_citations_come_only_from_fully_visible_support_sets(
    dclient: TestClient, decisions: tuple[FreshDb, SeededCorpus]
) -> None:
    login(dclient, "asha")
    body = dclient.get("/decisions/ADR-007").json()
    by_key = {c["key"]: c for c in body["claims"]}
    assert by_key["C3"]["citations"], "a visible claim carries its sources"
    assert all(x["external_id"] for x in by_key["C3"]["citations"])
    # A claim with two sets, one of them hidden from the persona, cites only the readable one.
    login(dclient, "carla")
    for key in ("ADR-007", "ADR-014", "ADR-012", "ADR-018", "ADR-009"):
        r = dclient.get(f"/decisions/{key}")
        if r.status_code != 200:
            continue
        for claim in r.json()["claims"]:
            if claim["citations"]:
                with decisions[0].connect_owner() as c:
                    for cite in claim["citations"]:
                        row = c.execute(
                            "SELECT acl FROM cairn.items WHERE external_id = %s",
                            (cite["external_id"],),
                        ).fetchone()
                        assert row is not None
                        groups = {
                            int(g[0])
                            for g in c.execute(
                                "SELECT p.principal_id FROM cairn.principals p WHERE p.external_id IN ('group:public','user:carla') "  # noqa: E501
                                "UNION SELECT group_id FROM cairn.memberships m JOIN cairn.principals u ON u.principal_id = m.member_id "  # noqa: E501
                                "WHERE u.external_id = 'user:carla' AND m.valid_period @> now()"
                            ).fetchall()
                        }
                        assert set(row[0]) & groups, (key, claim["key"], cite)
