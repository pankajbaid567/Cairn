"""Phase 3 on the API: the local receiver, the sync barrier, L4 recheck and the L7 answer cache
(plan 3.16, 3.21, 3.29)."""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from cairn_core.testing import FreshDb
from cairn_eval.seed import SeededCorpus

from .conftest import login, make_app_client

pytestmark = pytest.mark.integration

FIXTURES = Path(__file__).resolve().parents[4] / "tests" / "fixtures" / "webhooks"
INDEX = json.loads((FIXTURES / "index.json").read_text())
SECRET = INDEX["secret"]


def fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / f"{name}.json").read_text())


@pytest.fixture(scope="module")
def sclient(seeded: tuple[FreshDb, SeededCorpus]) -> Iterator[TestClient]:
    yield from make_app_client(
        seeded[0],
        CAIRN_GITHUB_WEBHOOK_SECRET=SECRET,
        CAIRN_SYNC_WAIT_S=0.6,
        CAIRN_ANSWER_CACHE_SIZE=16,
    )


def post(client: TestClient, name: str, **overrides: str) -> Any:
    f = fixture(name)
    return client.post("/webhooks/github", content=f["body"], headers={**f["headers"], **overrides})


def inbox(db: FreshDb) -> list[tuple[Any, ...]]:
    with db.connect_owner() as c:
        return c.execute(
            "SELECT delivery_id, event, action, class, priority, repo_name, object_number, applied_at "  # noqa: E501
            "FROM cairn.inbox ORDER BY delivery_id"
        ).fetchall()


@pytest.fixture
def clean_inbox(seeded: tuple[FreshDb, SeededCorpus]) -> Iterator[None]:
    with seeded[0].connect_ingest() as c:
        c.execute("UPDATE cairn.inbox SET applied_at = now() WHERE applied_at IS NULL")
    yield
    with seeded[0].connect_ingest() as c:
        c.execute("UPDATE cairn.inbox SET applied_at = now() WHERE applied_at IS NULL")


def test_every_fixture_produces_the_expected_thin_row_or_is_ignored(
    sclient: TestClient, seeded: tuple[FreshDb, SeededCorpus], clean_inbox: None
) -> None:
    db = seeded[0]
    for entry in INDEX["fixtures"]:
        f = fixture(entry["name"])
        r = post(sclient, entry["name"])
        assert r.status_code == 202, (entry["name"], r.text)
        assert r.json()["status"] == ("ignored" if f["expected"] is None else "accepted")
    rows = {r[0]: r for r in inbox(db)}
    for entry in INDEX["fixtures"]:
        f = fixture(entry["name"])
        delivery = f["headers"]["x-github-delivery"]
        if f["expected"] is None:
            assert delivery not in rows
            continue
        e = f["expected"]
        got = rows[delivery]
        assert (got[1], got[2], got[3], got[5], got[6]) == (
            e["event"],
            e["action"],
            e["class"],
            e["repo_name"],
            e["object_number"],
        )
        assert got[4] == (0 if e["class"] == "acl" else 1)


def test_a_bad_signature_gets_401_and_stores_nothing(
    sclient: TestClient, seeded: tuple[FreshDb, SeededCorpus], clean_inbox: None
) -> None:
    before = len(inbox(seeded[0]))
    r = post(sclient, "membership_removed", **{"x-hub-signature-256": "sha256=" + "0" * 64})
    assert r.status_code == 401
    assert len(inbox(seeded[0])) == before
    missing = sclient.post(
        "/webhooks/github",
        content=fixture("membership_removed")["body"],
        headers={"x-github-event": "membership", "x-github-delivery": "no-sig"},
    )
    assert missing.status_code == 401


def test_a_duplicate_delivery_is_a_no_op(
    sclient: TestClient, seeded: tuple[FreshDb, SeededCorpus], clean_inbox: None
) -> None:
    db = seeded[0]
    for _ in range(3):
        assert post(sclient, "repository_privatized").status_code == 202
    delivery = fixture("repository_privatized")["headers"]["x-github-delivery"]
    assert sum(1 for r in inbox(db) if r[0] == delivery) == 1


def test_an_inbox_row_holds_no_payload_text(
    sclient: TestClient, seeded: tuple[FreshDb, SeededCorpus], clean_inbox: None
) -> None:
    assert post(sclient, "issue_comment_created").status_code == 202
    with seeded[0].connect_owner() as c:
        text = str(c.execute("SELECT * FROM cairn.inbox").fetchall())
    assert "private text must not be stored" not in text


def test_the_edge_role_can_insert_into_the_inbox_and_do_nothing_else(
    seeded: tuple[FreshDb, SeededCorpus],
) -> None:
    import psycopg

    db = seeded[0]
    if db.edge_dsn is None:
        pytest.skip("CAIRN_EDGE_DB_PASSWORD is not set (make env-update)")
    from cairn_core.webhook import INSERT_SQL

    params = ("edge-probe-1", "membership", "removed", "acl", 0, 1, 2, 3, "o/r", None)
    with db.connect_edge() as conn:
        conn.execute(INSERT_SQL, params)
        conn.execute(INSERT_SQL, params)  # ON CONFLICT DO NOTHING needs no SELECT right
        for sql in (
            "SELECT * FROM cairn.inbox",
            "UPDATE cairn.inbox SET applied_at = now()",
            "DELETE FROM cairn.inbox",
            "SELECT * FROM cairn.items",
            "INSERT INTO cairn.items (connector, external_id, source_kind, title, acl) VALUES ('x','y','doc','t','{1}')",  # noqa: E501
        ):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute(sql)
    with db.connect_ingest() as c:
        c.execute("UPDATE cairn.inbox SET applied_at = now() WHERE applied_at IS NULL")


# --- drain-before-serve (3.29) -------------------------------------------------------------


def add_pending(db: FreshDb, delivery: str, klass: str) -> None:
    with db.connect_ingest() as c:
        c.execute(
            "INSERT INTO cairn.inbox (delivery_id, event, class, priority) VALUES (%s, %s, %s, %s)",
            (
                delivery,
                "membership" if klass == "acl" else "push",
                klass,
                0 if klass == "acl" else 1,
            ),
        )


def test_a_pending_acl_event_closes_every_query_path_with_503(
    sclient: TestClient, seeded: tuple[FreshDb, SeededCorpus], clean_inbox: None
) -> None:
    db = seeded[0]
    login(sclient, "asha")
    add_pending(db, "barrier-1", "acl")
    for method, path, payload in (
        ("post", "/search", {"query": "postgres"}),
        ("post", "/answer", {"question": "postgres"}),
        ("get", "/decisions/ADR-007", None),
        ("get", "/decisions/ADR-007/timeline", None),
        ("get", "/decisions?q=adr", None),
    ):
        r = (
            sclient.request(method, path, json=payload)
            if payload
            else sclient.request(method, path)
        )
        assert r.status_code == 503, (path, r.status_code)
        assert r.json() == {"error": "permissions_syncing"}
        assert r.headers["retry-after"]


def test_a_pending_content_event_does_not_block_queries(
    sclient: TestClient, seeded: tuple[FreshDb, SeededCorpus], clean_inbox: None
) -> None:
    login(sclient, "asha")
    add_pending(seeded[0], "barrier-content", "content")
    assert sclient.post("/search", json={"query": "postgres"}).status_code == 200


def test_the_barrier_waits_and_proceeds_once_the_event_is_applied(
    sclient: TestClient, seeded: tuple[FreshDb, SeededCorpus], clean_inbox: None
) -> None:
    db = seeded[0]
    login(sclient, "asha")
    add_pending(db, "barrier-wait", "acl")

    def apply_soon() -> None:
        time.sleep(0.25)
        with db.connect_ingest() as c:
            c.execute(
                "UPDATE cairn.inbox SET applied_at = now() WHERE delivery_id = 'barrier-wait'"
            )

    t = threading.Thread(target=apply_soon)
    t.start()
    started = time.monotonic()
    r = sclient.post("/search", json={"query": "postgres"})
    t.join()
    assert r.status_code == 200
    assert 0.2 <= time.monotonic() - started < 0.6  # it waited, and not for the whole limit


def test_events_received_after_the_request_do_not_block_it(
    sclient: TestClient, seeded: tuple[FreshDb, SeededCorpus], clean_inbox: None
) -> None:
    login(sclient, "asha")
    assert sclient.post("/search", json={"query": "postgres"}).status_code == 200
    add_pending(seeded[0], "barrier-later", "acl")
    assert sclient.post("/search", json={"query": "postgres"}).status_code == 503


# --- L4 and L7 (3.9, 3.21) -----------------------------------------------------------------


def test_an_answer_cached_for_asha_is_never_served_to_ben(
    sclient: TestClient, clean_inbox: None
) -> None:
    q = {"question": "Why did we move payment-service to PostgreSQL?"}
    login(sclient, "asha")
    first = sclient.post("/answer", json=q).json()
    again = sclient.post("/answer", json=q).json()
    assert first["abstained"] is False
    assert again.get("cached") is True
    assert first.get("cached") is None
    login(sclient, "carla")  # a different principal set
    other = sclient.post("/answer", json=q).json()
    assert other.get("cached") is None
    assert other["citations"] != first["citations"] or other["answer"] != first["answer"]
    # and each principal's own repeat is cached separately
    assert sclient.post("/answer", json=q).json().get("cached") is True


def test_any_sync_or_purge_invalidates_cached_answers(
    sclient: TestClient, seeded: tuple[FreshDb, SeededCorpus], clean_inbox: None
) -> None:
    q = {"question": "Which database does payment-service use?"}
    login(sclient, "asha")
    sclient.post("/answer", json=q)
    assert sclient.post("/answer", json=q).json().get("cached") is True
    with seeded[0].connect_ingest() as c:
        c.execute("INSERT INTO cairn.acl_versions (reason) VALUES ('github')")
    assert sclient.post("/answer", json=q).json().get("cached") is None


def test_l4_withholds_an_answer_when_access_changed_during_composition(
    sclient: TestClient, monkeypatch: pytest.MonkeyPatch, clean_inbox: None
) -> None:
    import cairn_api.search as search_module

    async def revoked(*_a: Any, **_k: Any) -> bool:
        return False

    monkeypatch.setattr(search_module, "still_visible", revoked)
    login(sclient, "asha")
    r = sclient.post(
        "/answer", json={"question": "Why PostgreSQL for payment-service transactions?"}
    )
    body = r.json()
    assert r.status_code == 200
    assert body["abstained"] is True
    assert body["citations"] == []


def test_answers_carry_a_freshness_badge_per_source(
    sclient: TestClient, seeded: tuple[FreshDb, SeededCorpus], clean_inbox: None
) -> None:
    login(sclient, "asha")
    body = sclient.post(
        "/answer", json={"question": "Why did we move payment-service to PostgreSQL? badge"}
    ).json()
    assert body["freshness"]["weakest_badge"] == "Imported"  # the seeded corpus has no live feed
    assert all(v == "Imported" for v in body["freshness"]["sources"].values())


def test_badge_rules() -> None:
    from cairn_retrieval.freshness import badge, weakest

    assert badge("import:docs_folder", None, None) == "Imported"
    assert badge("github:o/r", 10.0, None) == "Live"
    assert badge("github:o/r", 10.0, 301.0) == "Delayed"
    assert badge("github:o/r", 90000.0, None) == "Stale"
    assert badge("github:o/r", None, None) == "Stale"
    assert weakest(["Live", "Imported"]) == "Imported"
    assert weakest(["Live", "Delayed", "Imported"]) == "Delayed"
    assert weakest(["Delayed", "Stale"]) == "Stale"
    assert weakest([]) == "Imported"


def test_sources_page_lists_only_sources_the_persona_can_read(
    sclient: TestClient, clean_inbox: None
) -> None:
    login(sclient, "asha")
    body = sclient.get("/sync/sources").json()
    assert {s["source"] for s in body["sources"]} >= {"import:docs_folder"}
    assert all(
        s["badge"] == "Imported" for s in body["sources"] if s["source"].startswith("import:")
    )
    assert {"pending_acl", "pending_content", "oldest_pending_s", "last_applied_at"} <= set(body)
    sclient.cookies.clear()
    assert sclient.get("/sync/sources").status_code == 401


def test_audit_page_shows_only_the_callers_own_entries(
    sclient: TestClient, clean_inbox: None
) -> None:
    login(sclient, "asha")
    sclient.post("/search", json={"query": "audit-page-asha-marker"})
    mine = sclient.get("/audit/mine").json()["entries"]
    assert mine
    assert all(set(e) >= {"audit_id", "at", "endpoint", "outcome", "returned"} for e in mine)
    asha_ids = {e["audit_id"] for e in mine}
    login(sclient, "carla")
    carla = sclient.get("/audit/mine").json()["entries"]
    assert not asha_ids & {e["audit_id"] for e in carla}  # never another person's rows
    sclient.post("/search", json={"query": "audit-page-carla-marker"})
    newest = sclient.get("/audit/mine").json()["entries"][0]
    assert newest["audit_id"] not in {
        e["audit_id"] for e in carla
    }  # the list is capped, so compare ids
    assert newest["endpoint"] == "search"
