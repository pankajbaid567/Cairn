"""Canary matrix v2: the sync states (plan 3.22). A token that lives only in a security-team repo is
probed on search and answer as every persona in each state: readable, after revocation, in flight
(an ACL event stored but not applied), pending at wake, and after deletion. Every cell where the
persona may not read the token must not contain it, and the readable cells must (positive control).
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient

from cairn_connectors.github.fake import Delivery, FakeGitHub
from cairn_connectors.github.sync import drain, sync_permissions
from cairn_core.testing import DbTestConfig, FreshDb, temporary_database
from cairn_core.webhook import INSERT_SQL, insert_params, thin_row

from .conftest import make_app_client

pytestmark = [pytest.mark.integration, pytest.mark.matrix]
ORG = "kestrel-pay-demo"
SECRET = "kestrel-test-webhook-secret"
TOKEN = "KP-SEC-CANARY-4417"
PERSONAS = ("asha", "ben", "carla", "priya")


@pytest.fixture
def world(
    admin_conn: psycopg.Connection[Any], admin_config: DbTestConfig
) -> Iterator[tuple[FreshDb, FakeGitHub, TestClient, dict[str, str]]]:
    if admin_config.settings.ingest_db_password is None:
        pytest.skip("CAIRN_INGEST_DB_PASSWORD is not set (make env-update)")
    with temporary_database(admin_conn, admin_config) as db:
        gh = FakeGitHub(org=ORG, secret=SECRET)
        gh.add_member("asha", ["payments"])
        gh.add_member("ben", ["security"])
        gh.add_member("carla", ["hr"])
        gh.add_member("priya", ["security"])
        gh.add_repo("security-policies")
        gh.grant_team("security-policies", "security")
        gen = make_app_client(
            db, CAIRN_GITHUB_WEBHOOK_SECRET=SECRET, CAIRN_SYNC_WAIT_S=0.4, CAIRN_ANSWER_CACHE_SIZE=0
        )
        client = next(gen)
        cookies = {}
        for p in PERSONAS:
            with psycopg.connect(db.ingest_dsn, prepare_threshold=None) as c:  # type: ignore[arg-type]
                if p == PERSONAS[0]:
                    sync_permissions(c, gh, ORG)
                    c.commit()
            r = client.post("/auth/dev-login", json={"persona": p})
            assert r.status_code == 204, r.text
            cookies[p] = f"cairn_session={r.cookies.get('cairn_session')}"
        client.cookies.clear()
        try:
            yield db, gh, client, cookies
        finally:
            next(gen, None)


def receive(db: FreshDb, d: Delivery) -> None:
    import json

    row = thin_row(d.headers, json.loads(d.body))
    assert row is not None
    with db.connect_ingest() as c:
        c.execute(INSERT_SQL, insert_params(row))


def apply_all(db: FreshDb, gh: FakeGitHub) -> None:
    assert db.ingest_dsn is not None
    with psycopg.connect(db.ingest_dsn, prepare_threshold=None) as c:
        drain(c, gh, ORG)


def seen_by(client: TestClient, cookies: dict[str, str], persona: str) -> str:
    h = {"cookie": cookies[persona]}
    a = client.post("/search", json={"query": TOKEN}, headers=h).text
    b = client.post("/answer", json={"question": f"What is {TOKEN}?"}, headers=h).text
    return a + b


def readers_now(db: FreshDb) -> set[str]:
    with db.connect_owner() as c:
        rows = c.execute(
            "SELECT u.external_id FROM cairn.principals u WHERE u.kind = 'user' AND EXISTS ("
            "SELECT 1 FROM cairn.items i WHERE i.source_key LIKE 'github:%%' AND i.acl && ARRAY("
            "SELECT group_id FROM cairn.memberships m WHERE m.member_id = u.principal_id "
            "AND m.valid_period @> now() UNION SELECT u.principal_id))"
        ).fetchall()
    return {str(r[0]).split(":", 1)[1] for r in rows}


def matrix(
    world: tuple[FreshDb, FakeGitHub, TestClient, dict[str, str]], allowed: set[str], label: str
) -> int:
    _, _, client, cookies = world
    positives = 0
    for persona in PERSONAS:
        text = seen_by(client, cookies, persona)
        if persona in allowed:
            assert TOKEN in text, f"{label}: {persona} should reach the canary (positive control)"
            positives += 1
        else:
            assert TOKEN not in text, f"LEAK in state {label}: {persona}"
    return positives


def test_the_canary_never_leaks_in_any_sync_state(
    world: tuple[FreshDb, FakeGitHub, TestClient, dict[str, str]],
) -> None:
    db, gh, client, cookies = world
    receive(db, gh.put_file("security-policies", "keys.md", f"# Keys\n\nRotation token {TOKEN}."))
    apply_all(db, gh)
    assert readers_now(db) == {"ben", "priya"}

    # 1. readable: only the security team
    assert matrix(world, {"ben", "priya"}, "readable") == 2

    # 2. in flight: Ben is removed on GitHub and the event is stored but not yet applied.
    #    Every path fails closed for everyone, so the canary reaches nobody.
    receive(db, gh.remove_team_member("security", "ben"))
    started = time.monotonic()
    for persona in PERSONAS:
        assert seen_by(client, cookies, persona).count("permissions_syncing") == 2
        assert TOKEN not in seen_by(client, cookies, persona)
    assert time.monotonic() - started < 10

    # 3. after revocation: applied, Ben is out and Priya still reads
    apply_all(db, gh)
    assert readers_now(db) == {"priya"}
    assert matrix(world, {"priya"}, "after revocation") == 1

    # 4. pending at wake: a poll row is queued before the first request (the API just started)
    with db.connect_ingest() as c:
        c.execute(
            "INSERT INTO cairn.inbox (delivery_id, event, class, priority) "
            "VALUES ('boot', 'poll', 'acl', 0)"
        )
    for persona in PERSONAS:
        assert TOKEN not in seen_by(client, cookies, persona)
    apply_all(db, gh)
    assert matrix(world, {"priya"}, "after wake") == 1

    # 5. after deletion: the file is deleted on GitHub; nobody reaches the canary on any path
    receive(db, gh.delete_file("security-policies", "keys.md"))
    apply_all(db, gh)
    with db.connect_owner() as c:
        row = c.execute(
            "SELECT count(*) FROM cairn.chunks WHERE text LIKE %s", (f"%{TOKEN}%",)
        ).fetchone()
    assert row == (0,)
    assert matrix(world, set(), "after deletion") == 0
