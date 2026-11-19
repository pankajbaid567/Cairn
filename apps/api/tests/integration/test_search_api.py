"""API behaviour end to end on the seeded Kestrel Pay database (plan 1.9 to 1.13, 1.23)."""

from __future__ import annotations

import re
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient

from cairn_api.sessions import COOKIE_NAME, sign
from cairn_core.testing import FreshDb
from cairn_eval.seed import SeededCorpus

from .conftest import SECRET, login

pytestmark = pytest.mark.integration

CITATION = re.compile(r"^chunk:.+@[0-9a-f]{8}:\d+-\d+$")


def test_unauthenticated_requests_are_refused(client: TestClient) -> None:
    client.cookies.clear()
    assert client.get("/me").status_code == 401
    assert client.post("/search", json={"query": "postgres"}).status_code == 401
    assert client.post("/answer", json={"question": "why postgres"}).json() == {
        "error": "unauthenticated"
    }


def test_a_tampered_or_expired_cookie_is_refused(client: TestClient) -> None:
    good = sign("asha", SECRET.encode(), 3600)
    for bad in (
        good[:-2] + "xx",
        "x" + good,
        sign("asha", SECRET.encode(), -10),
        sign("asha", b"other-secret" * 4, 3600),
    ):
        client.cookies.clear()
        client.cookies.set(COOKIE_NAME, bad)
        assert client.get("/me").status_code == 401


def test_personas_login_me_and_logout(client: TestClient) -> None:
    client.cookies.clear()
    personas = client.get("/auth/personas").json()
    assert personas["demo"] is False
    assert [p["persona"] for p in personas["personas"]] == [
        "asha",
        "ben",
        "carla",
        "priya",
        "dev",
    ]
    login(client, "ben")
    assert client.get("/me").json() == {"persona": "ben", "display_name": "Ben"}
    assert client.post("/auth/logout").status_code == 204
    client.cookies.clear()
    assert client.post("/auth/dev-login", json={"persona": "mallory"}).status_code == 422


def test_invalid_requests_never_echo_their_input(client: TestClient) -> None:
    login(client, "asha")
    for body in (
        {},
        {"query": ""},
        {"query": "x" * 301},
        {"query": "ok", "limit": 99},
        {"query": "ok", "extra": "SECRET-INPUT"},
    ):
        r = client.post("/search", json=body)
        assert r.status_code == 422
        assert r.json() == {"error": "invalid_request"}
        assert "SECRET-INPUT" not in r.text
    assert client.post("/answer", json={"question": "q" * 501}).json() == {
        "error": "invalid_request"
    }


def test_search_returns_cited_results_and_an_audit_row(
    client: TestClient, seeded: tuple[FreshDb, SeededCorpus]
) -> None:
    login(client, "asha")
    r = client.post(
        "/search", json={"query": "Why did payment-service move from MongoDB to PostgreSQL?"}
    )
    assert r.status_code == 200
    assert r.headers["cache-control"] == "no-store"
    body = r.json()
    assert body["searched"] == ["chat_export", "meeting_notes", "docs_folder"]
    assert body["degraded"] == []
    assert body["results"]
    assert all(CITATION.match(x["citation"]) for x in body["results"])
    assert [x["rank"] for x in body["results"]] == list(range(1, len(body["results"]) + 1))
    adr7 = seeded[1].external_id("adr-file-007")
    assert adr7
    assert any(x["citation"].startswith(f"chunk:{adr7}@") for x in body["results"])
    with seeded[0].connect_owner() as owner:
        row = owner.execute(
            "SELECT endpoint, outcome, question_hmac, array_length(returned_item_ids, 1) "
            "FROM cairn.audit_log WHERE audit_id = %s",
            (body["audit_id"],),
        ).fetchone()
    assert row is not None
    assert row[0] == "search"
    assert row[1] == "results"
    assert len(row[2]) == 64
    assert row[3] > 0


def test_every_citation_offset_resolves_to_the_exact_source_text(
    client: TestClient, seeded: tuple[FreshDb, SeededCorpus]
) -> None:
    login(client, "asha")
    body = client.post(
        "/answer", json={"question": "Why did payment-service move from MongoDB to PostgreSQL?"}
    ).json()
    assert not body["abstained"]
    assert body["citations"]
    with seeded[0].connect_owner() as owner:
        for c in body["citations"]:
            m = re.match(r"^chunk:(.+)@([0-9a-f]{8}):(\d+)-(\d+)$", c["id"])
            assert m is not None
            ext, h8, start, end = m.group(1), m.group(2), int(m.group(3)), int(m.group(4))
            row = owner.execute(
                "SELECT substr(r.text, %s, %s) FROM cairn.revisions r JOIN cairn.items i USING (item_id) "  # noqa: E501
                "WHERE i.external_id = %s AND starts_with(r.content_hash, %s)",
                (start + 1, end - start, ext, h8),
            ).fetchone()
            assert row is not None
            assert row[0].startswith(c["quote"][:50])


def test_answer_cites_only_visible_evidence_and_is_audited(
    client: TestClient, seeded: tuple[FreshDb, SeededCorpus]
) -> None:
    login(client, "asha")
    body = client.post(
        "/answer", json={"question": "Why did payment-service move from MongoDB to PostgreSQL?"}
    ).json()
    assert body["abstained"] is False
    assert body["claims"]
    assert body["model"]["prompt_version"] == "answer-v1"
    ids = {c["id"] for c in body["citations"]}
    assert all(cit in ids for claim in body["claims"] for cit in claim["citations"])
    assert re.findall(r"\[(\d+)\]", body["answer"])
    assert all(1 <= int(n) <= len(ids) for n in re.findall(r"\[(\d+)\]", body["answer"]))
    with seeded[0].connect_owner() as owner:
        row = owner.execute(
            "SELECT outcome, answer_sha256 FROM cairn.audit_log WHERE audit_id = %s",
            (body["audit_id"],),
        ).fetchone()
    assert row is not None
    assert row[0] == "answered"
    assert len(row[1]) == 64


def test_the_not_found_reply_is_identical_with_or_without_hidden_matches(
    client: TestClient,
) -> None:
    """Test 1.23: Ben gets the same reply for a question whose only matches are hidden from him
    (HR notes) as for a question with no matches anywhere."""
    login(client, "ben")
    hidden = client.post("/answer", json={"question": "payroll letters performance"}).json()
    nothing = client.post("/answer", json={"question": "zzqx wvvk plorb"}).json()
    for body in (hidden, nothing):
        body.pop("audit_id")
    assert hidden == nothing
    assert hidden["abstained"] is True
    assert hidden["answer"] == "Not found in sources you can access."
    assert hidden["claims"] == []
    assert hidden["citations"] == []


def test_audit_log_is_append_only_for_every_role(seeded: tuple[FreshDb, SeededCorpus]) -> None:
    db = seeded[0]
    with db.connect_owner() as owner:
        before = owner.execute("SELECT count(*), count(model) FROM cairn.audit_log").fetchone()
    assert before is not None
    assert before[0] > 0
    # app and ingest hold no UPDATE, DELETE or TRUNCATE privilege on the table at all
    for connect in (db.connect_app, db.connect_ingest):
        with connect() as conn:
            for stmt in (
                "UPDATE cairn.audit_log SET model = 'x'",
                "DELETE FROM cairn.audit_log",
                "TRUNCATE cairn.audit_log",
            ):
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    conn.execute(stmt)
    # the owner has the privileges, but no policy lets it touch a row, and the trigger blocks TRUNCATE  # noqa: E501
    with db.connect_owner() as owner:
        assert owner.execute("UPDATE cairn.audit_log SET model = 'x'").rowcount == 0
        assert owner.execute("DELETE FROM cairn.audit_log").rowcount == 0
        with pytest.raises(psycopg.errors.InsufficientPrivilege, match="append-only"):
            owner.execute("TRUNCATE cairn.audit_log")
        assert (
            owner.execute("SELECT count(*), count(model) FROM cairn.audit_log").fetchone() == before
        )


def test_the_app_role_cannot_write_content_or_read_the_acl_tables(
    seeded: tuple[FreshDb, SeededCorpus],
) -> None:
    with seeded[0].connect_app() as conn:
        for stmt in (
            "INSERT INTO cairn.items (connector, external_id, source_kind, title, acl) VALUES ('docs_folder','x','doc','t','{1}')",  # noqa: E501
            "UPDATE cairn.chunks SET title = 'x'",
            "DELETE FROM cairn.chunks",
            "SELECT * FROM cairn.item_acls",
            "SELECT * FROM cairn.outbox",
        ):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute(stmt)


def test_health_endpoints_do_not_need_a_session(client: TestClient) -> None:
    client.cookies.clear()
    assert client.get("/healthz").json() == {"status": "ok"}
    r: Any = client.get("/auth/personas")
    assert r.status_code == 200
