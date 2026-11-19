"""Conflicts and the review queue (plan 4.4, 4.5, 4.14). Nothing is ever auto-resolved."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from cairn_connectors.decisions import conflicts
from cairn_connectors.decisions.conflicts import Side, Statement, detect_date_conflicts
from cairn_core.testing import FreshDb
from cairn_eval.seed import SeededCorpus

from .conftest import login, make_app_client

pytestmark = pytest.mark.integration
PERSONAS = ("asha", "ben", "carla", "priya")


@pytest.fixture(scope="module")
def rclient(decisions: tuple[FreshDb, SeededCorpus]) -> Iterator[TestClient]:
    yield from make_app_client(decisions[0])


def conflict_for(client: TestClient, persona: str) -> dict[str, Any] | None:
    login(client, persona)
    rows = client.get("/reviews?state=all").json()["reviews"]
    return next((r for r in rows if r["kind"] == "conflict" and r["decision"] == "ADR-015"), None)


def test_adr015_conflict_shows_both_dates_with_tiers_and_sources_to_a_reader_of_both(
    rclient: TestClient,
) -> None:
    seen = {p: conflict_for(rclient, p) for p in PERSONAS}
    readers = [p for p, c in seen.items() if c is not None]
    assert readers, "at least one persona can read both sides of the API v1 deprecation conflict"
    c = seen[readers[0]]
    assert c is not None
    sides = {s["value"]: s for s in c["sides"]}
    assert set(sides) == {"2026-09-30", "2026-10-31"}
    assert (sides["2026-09-30"]["tier"], sides["2026-10-31"]["tier"]) == ("T3", "T5")
    assert sides["2026-09-30"]["source"] == "doc-api-v1-deprecation"
    assert sides["2026-10-31"]["source"] == "thread-mobile-eng-2026-07-14"
    assert c["state"] == "open"
    assert c["detail"]["subject"] == "API v1 deprecation date"
    page = rclient.get("/decisions/ADR-015").json()
    assert page["decision"]["verification"] == "disputed"


def test_a_persona_who_cannot_read_every_side_does_not_see_the_conflict_at_all(
    rclient: TestClient,
) -> None:
    seen = {p: conflict_for(rclient, p) for p in PERSONAS}
    if all(c is not None for c in seen.values()):
        pytest.skip("every demo persona can read both sides in this scenario")
    hidden = [p for p, c in seen.items() if c is None]
    for p in hidden:
        login(rclient, p)
        text = rclient.get("/reviews?state=all").text
        assert "2026-10-31" not in text
        assert "thread-mobile-eng-2026-07-14" not in text


def test_resolution_needs_a_reason_and_records_it_and_hidden_items_look_missing(
    rclient: TestClient, decisions: tuple[FreshDb, SeededCorpus]
) -> None:
    holder = next((p for p in PERSONAS if conflict_for(rclient, p) is not None), None)
    assert holder is not None
    review = conflict_for(rclient, holder)
    assert review is not None
    rid = review["review_id"]
    assert (
        rclient.post(
            f"/reviews/{rid}/resolve", json={"state": "verified", "reason": "no"}
        ).status_code
        == 422
    )
    assert (
        rclient.post(
            f"/reviews/{rid}/resolve", json={"state": "bogus", "reason": "long enough reason"}
        ).status_code
        == 422
    )
    outsider = next((p for p in PERSONAS if conflict_for(rclient, p) is None), None)
    if outsider is not None:
        login(rclient, outsider)
        r = rclient.post(
            f"/reviews/{rid}/resolve", json={"state": "verified", "reason": "I want to see"}
        )
        assert r.status_code == 404  # same as a review item that does not exist
        assert (
            rclient.post(
                "/reviews/999999/resolve", json={"state": "verified", "reason": "I want to see"}
            ).status_code
            == 404
        )
    login(rclient, holder)
    ok = rclient.post(
        f"/reviews/{rid}/resolve", json={"state": "disputed", "reason": "Both dates are in use"}
    )
    assert ok.status_code == 200
    again = rclient.post(
        f"/reviews/{rid}/resolve", json={"state": "verified", "reason": "second attempt here"}
    )
    assert again.status_code == 404  # already resolved
    done = conflict_for(rclient, holder)
    assert done is not None
    assert done["state"] == "disputed"
    assert done["reason"] == "Both dates are in use"
    with decisions[0].connect_owner() as c:
        who = c.execute(
            "SELECT resolved_by FROM cairn.review_items WHERE review_id = %s", (rid,)
        ).fetchone()
        mine = c.execute(
            "SELECT principal_id FROM cairn.principals WHERE external_id = %s", (f"user:{holder}",)
        ).fetchone()
    assert who == mine


def test_editing_updates_the_decision_text(
    rclient: TestClient, decisions: tuple[FreshDb, SeededCorpus]
) -> None:
    db, _ = decisions
    with db.connect_ingest() as c:
        did = c.execute("SELECT decision_id FROM cairn.decisions WHERE key = 'ADR-007'").fetchone()
        assert did is not None
        rid = c.execute(
            "INSERT INTO cairn.review_items (kind, decision_id, detail) VALUES ('candidate', %s, '{}') RETURNING review_id",  # noqa: E501
            (did[0],),
        ).fetchone()
    assert rid is not None
    login(rclient, "asha")
    r = rclient.post(
        f"/reviews/{rid[0]}/resolve",
        json={
            "state": "edited",
            "reason": "Fix the outcome wording",
            "edit": {"outcome": "Move payment-service to PostgreSQL (edited)."},
        },
    )
    assert r.status_code == 200
    assert rclient.get("/decisions/ADR-007").json()["decision"]["outcome"].endswith("(edited).")
    bad = rclient.post(
        f"/reviews/{rid[0]}/resolve",
        json={"state": "edited", "reason": "wrong field edit", "edit": {"acl": "x"}},
    )
    assert bad.status_code == 422


def test_unauthenticated_gets_401_and_a_bad_state_filter_422(rclient: TestClient) -> None:
    rclient.cookies.clear()
    assert rclient.get("/reviews").status_code == 401
    login(rclient, "asha")
    assert rclient.get("/reviews?state=nonsense").status_code == 422


def test_the_deterministic_rule_finds_conflicting_dates_and_ignores_the_rest() -> None:
    a = Statement("API v1 will be deprecated on 2026-09-30", "T3", "doc", 1, (1,))
    b = Statement("API v1 will be deprecated on 2026-10-31", "T5", "chat", 2, (1,))
    same_source = Statement("API v1 will be deprecated on 2026-11-30", "T3", "doc", 3, (1,))
    unrelated = Statement("The offsite is on 2026-08-01 in Pune", "T5", "chat2", 4, (1,))
    agree = Statement("API v1 will be deprecated on 2026-09-30", "T5", "chat3", 5, (1,))
    pairs = detect_date_conflicts([a, b, same_source, unrelated, agree])
    assert {frozenset({x.value, y.value}) for x, y in pairs} == {
        frozenset({"2026-09-30", "2026-10-31"}),
        frozenset({"2026-10-31", "2026-11-30"}),
        frozenset({"2026-09-30", "2026-11-30"}),  # the agreeing chat versus the doc's second date
    }
    assert all(x.source_ref != y.source_ref for x, y in pairs)
    assert not detect_date_conflicts([a, agree])  # same date: no conflict
    assert not detect_date_conflicts([a, unrelated])


@pytest.mark.asyncio
async def test_the_judge_only_adds_conflicts_on_a_clear_yes() -> None:
    from cairn_retrieval.llm.types import LlmRequest, LlmResponse

    class Say:
        provider, model = "fake", "judge"

        def __init__(self, text: str) -> None:
            self.text = text

        async def complete(self, req: LlmRequest) -> LlmResponse:
            return LlmResponse(
                text=self.text,
                tokens_in=1,
                tokens_out=1,
                latency_ms=0.0,
                provider="fake",
                model="judge",
            )

    assert await conflicts.judge_conflict(Say('{"conflict": true}'), "s", "a", "b") is True
    for text in ('{"conflict": false}', "maybe", '{"conflict": "true"}', "", "{broken"):
        assert await conflicts.judge_conflict(Say(text), "s", "a", "b") is False


def test_open_conflict_is_idempotent_and_needs_two_sides(
    decisions: tuple[FreshDb, SeededCorpus],
) -> None:
    db, _ = decisions
    with db.connect_ingest() as conn:
        conn.autocommit = False
        did = conn.execute(
            "SELECT decision_id FROM cairn.decisions WHERE key = 'ADR-007'"
        ).fetchone()
        assert did is not None
        sides = [Side("a", "T3", "s1", None, (1,)), Side("b", "T5", "s2", None, (1,))]
        first = conflicts.open_conflict(conn, int(did[0]), "unit subject", sides)
        assert first is not None
        assert conflicts.open_conflict(conn, int(did[0]), "unit subject", sides) is None
        with pytest.raises(ValueError):  # noqa: PT011
            conflicts.open_conflict(conn, int(did[0]), "x", sides[:1])
        conn.commit()
