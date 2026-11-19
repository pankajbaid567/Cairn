"""GitHub sync against an in-memory organisation and a real database (plan 3.5 to 3.11, 3.16, 3.19)."""  # noqa: E501

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import psycopg
import pytest

from cairn_connectors.github.fake import Delivery, FakeGitHub
from cairn_connectors.github.models import GhComment
from cairn_connectors.github.sync import drain, purge_items, reconcile, sync_permissions
from cairn_core.testing import DbTestConfig, FreshDb, temporary_database
from cairn_core.webhook import INSERT_SQL, insert_params, thin_row

pytestmark = pytest.mark.integration
ORG = "kestrel-pay-demo"


@pytest.fixture
def db(admin_conn: psycopg.Connection[Any], admin_config: DbTestConfig) -> Iterator[FreshDb]:
    if admin_config.settings.ingest_db_password is None:
        pytest.skip("CAIRN_INGEST_DB_PASSWORD is not set (make env-update)")
    with temporary_database(admin_conn, admin_config) as database:
        yield database


@pytest.fixture
def conn(db: FreshDb) -> Iterator[psycopg.Connection[Any]]:
    assert db.ingest_dsn is not None
    with psycopg.connect(db.ingest_dsn, prepare_threshold=None) as c:
        yield c


@pytest.fixture
def gh() -> FakeGitHub:
    g = FakeGitHub(org=ORG)
    g.add_member("asha", ["payments"])
    g.add_member("ben", ["mobile", "security"])
    g.add_member("carla", ["hr"])
    g.add_repo("payment-service")
    g.grant_team("payment-service", "payments")
    g.add_repo("security-policies")
    g.grant_team("security-policies", "security")
    g.add_repo("eng-docs", private=False)
    return g


def receive(conn: psycopg.Connection[Any], d: Delivery) -> None:
    """What either receiver does: thin row, ON CONFLICT DO NOTHING."""
    import json

    row = thin_row(d.headers, json.loads(d.body))
    assert row is not None
    conn.execute(INSERT_SQL, insert_params(row))
    conn.commit()


def groups_of(conn: psycopg.Connection[Any], user: str) -> list[int]:
    rows = conn.execute(
        "SELECT principal_id FROM cairn.principals WHERE external_id = 'group:public' "
        "UNION SELECT principal_id FROM cairn.principals WHERE external_id = %s "
        "UNION SELECT group_id FROM cairn.memberships m JOIN cairn.principals u ON u.principal_id = m.member_id "  # noqa: E501
        "WHERE u.external_id = %s AND m.valid_period @> now()",
        (f"user:{user}", f"user:{user}"),
    ).fetchall()
    return [int(r[0]) for r in rows]


def can_read(conn: psycopg.Connection[Any], user: str, external_id: str) -> bool:
    row = conn.execute(
        "SELECT acl && %s::int[] FROM cairn.items WHERE external_id = %s",
        (groups_of(conn, user), external_id),
    ).fetchone()
    return bool(row and row[0])


def count(conn: psycopg.Connection[Any], sql: str, *args: Any) -> int:
    row = conn.execute(sql, args).fetchone()
    assert row is not None
    return int(row[0])


def test_permission_sync_is_idempotent_and_follows_changes(
    conn: psycopg.Connection[Any], gh: FakeGitHub
) -> None:
    first = sync_permissions(conn, gh, ORG)
    conn.commit()
    assert first.memberships_added == 4  # asha, ben x2, carla
    again = sync_permissions(conn, gh, ORG)
    assert (again.memberships_added, again.memberships_removed) == (0, 0)
    gh.remove_team_member("security", "ben")
    out = sync_permissions(conn, gh, ORG)
    assert (out.memberships_added, out.memberships_removed) == (0, 1)
    assert count(conn, "SELECT count(*) FROM cairn.acl_versions WHERE reason = 'github'") == 2


def test_pull_requests_issues_and_markdown_are_imported_with_the_repo_acl(
    conn: psycopg.Connection[Any], gh: FakeGitHub
) -> None:
    gh.put_pull(
        "payment-service",
        123,
        "Use PostgreSQL",
        "Move to PostgreSQL for transactions.",
        reviews=[GhComment("dev", "Approved.", gh.now())],
        labels=["decision"],
    )
    gh.put_issue("payment-service", 7, "Refund bug", "Double refunds happen.")
    gh.put_file(
        "eng-docs", "adr/ADR-020.md", "# ADR-020: Pick a queue\n\nDecision: use Postgres queues."
    )
    sync_permissions(conn, gh, ORG)
    for d in (
        gh.put_pull(
            "payment-service",
            123,
            "Use PostgreSQL",
            "Move to PostgreSQL for transactions.",
            labels=["decision"],
        ),
        gh.put_issue("payment-service", 8, "Another", "text"),
        gh.put_file(
            "eng-docs",
            "adr/ADR-020.md",
            "# ADR-020: Pick a queue\n\nDecision: use Postgres queues.",
        ),
    ):
        receive(conn, d)
    gh.put_issue("payment-service", 8, "Another", "text")
    stats = drain(conn, gh, ORG)
    assert stats.content_applied == 3
    assert stats.failed == 0
    kinds = dict(conn.execute("SELECT external_id, source_kind FROM cairn.items").fetchall())
    assert kinds[f"github:{ORG}/payment-service/pull/123"] == "pull_request"
    assert kinds[f"github:{ORG}/eng-docs/blob/adr/ADR-020.md"] == "adr_file"
    meta = conn.execute(
        "SELECT metadata FROM cairn.revisions r JOIN cairn.items i USING (item_id) WHERE i.external_id = %s",  # noqa: E501
        (f"github:{ORG}/payment-service/pull/123",),
    ).fetchone()
    assert meta is not None
    assert meta[0]["decision_label"] is True
    assert "Approved." not in str(meta[0])
    assert can_read(conn, "asha", f"github:{ORG}/payment-service/pull/123")
    assert not can_read(conn, "ben", f"github:{ORG}/payment-service/pull/123")  # not on payments
    assert can_read(conn, "carla", f"github:{ORG}/eng-docs/blob/adr/ADR-020.md")  # public repo
    assert (
        count(
            conn,
            "SELECT count(*) FROM cairn.items WHERE source_key = %s",
            f"github:{ORG}/payment-service",
        )
        == 2
    )


def test_editing_a_pr_adds_a_revision_and_a_repeat_changes_nothing(
    conn: psycopg.Connection[Any], gh: FakeGitHub
) -> None:
    sync_permissions(conn, gh, ORG)
    receive(conn, gh.put_pull("payment-service", 5, "t", "first body"))
    drain(conn, gh, ORG)
    receive(conn, gh.put_pull("payment-service", 5, "t", "second body"))
    drain(conn, gh, ORG)
    assert count(conn, "SELECT count(*) FROM cairn.revisions") == 2
    receive(conn, gh.put_pull("payment-service", 5, "t", "second body"))
    drain(conn, gh, ORG)
    assert count(conn, "SELECT count(*) FROM cairn.revisions") == 2  # same content: no new revision


def test_revoking_a_team_removes_access_and_a_removed_repo_access_fails_closed(
    conn: psycopg.Connection[Any], gh: FakeGitHub
) -> None:
    sync_permissions(conn, gh, ORG)
    receive(
        conn,
        gh.put_file(
            "security-policies", "token-rotation.md", "# Token rotation\n\nRotate every 30 days."
        ),
    )
    drain(conn, gh, ORG)
    doc = f"github:{ORG}/security-policies/blob/token-rotation.md"
    assert can_read(conn, "ben", doc)
    receive(conn, gh.remove_team_member("security", "ben"))
    drain(conn, gh, ORG)
    assert not can_read(conn, "ben", doc)
    assert (
        count(
            conn,
            "SELECT count(*) FROM cairn.chunks c JOIN cairn.items i USING (item_id) "
            "WHERE i.external_id = %s AND NOT c.acl && %s::int[]",
            doc,
            groups_of(conn, "ben"),
        )
        >= 1
    )
    gh.repo_teams["security-policies"].clear()  # no team may read it any more
    receive(conn, gh.remove_collaborator("security-policies", "nobody"))
    drain(conn, gh, ORG)
    acl = conn.execute("SELECT acl FROM cairn.items WHERE external_id = %s", (doc,)).fetchone()
    nobody = conn.execute(
        "SELECT principal_id FROM cairn.principals WHERE external_id = 'group:nobody'"
    ).fetchone()
    assert acl is not None
    assert nobody is not None
    assert list(acl[0]) == [nobody[0]]


def test_making_a_repo_public_and_private_changes_item_acls(
    conn: psycopg.Connection[Any], gh: FakeGitHub
) -> None:
    sync_permissions(conn, gh, ORG)
    receive(
        conn, gh.put_file("payment-service", "README.md", "# Payment service\n\nSettles payments.")
    )
    drain(conn, gh, ORG)
    doc = f"github:{ORG}/payment-service/blob/README.md"
    assert not can_read(conn, "carla", doc)
    receive(conn, gh.set_visibility("payment-service", private=False))
    drain(conn, gh, ORG)
    assert can_read(conn, "carla", doc)
    receive(conn, gh.set_visibility("payment-service", private=True))
    drain(conn, gh, ORG)
    assert not can_read(conn, "carla", doc)


def test_new_enemy_revoke_then_add_a_file_ben_never_sees_it(
    conn: psycopg.Connection[Any], gh: FakeGitHub
) -> None:
    """The revocation and the new file arrive together; ACL rows apply first and content waits."""
    sync_permissions(conn, gh, ORG)
    conn.commit()
    receive(conn, gh.remove_team_member("security", "ben"))
    receive(
        conn,
        gh.put_file(
            "security-policies", "incident-keys.md", "# Keys\n\nThe rotation keys live here."
        ),
    )
    stats = drain(conn, gh, ORG)
    assert stats.acl_applied == 1
    assert stats.content_applied == 1
    assert not can_read(conn, "ben", f"github:{ORG}/security-policies/blob/incident-keys.md")
    order = conn.execute(
        "SELECT class FROM cairn.inbox ORDER BY applied_at, delivery_id"
    ).fetchall()
    assert [r[0] for r in order] == ["acl", "content"]


def test_content_waits_while_an_acl_row_arrives_during_the_pass(
    conn: psycopg.Connection[Any], gh: FakeGitHub
) -> None:
    sync_permissions(conn, gh, ORG)
    conn.commit()
    receive(conn, gh.put_file("payment-service", "a.md", "# A\n\ntext"))
    conn.execute(
        "INSERT INTO cairn.inbox (delivery_id, event, class, priority) VALUES ('late-acl', 'membership', 'acl', 0)"  # noqa: E501
    )
    conn.commit()
    # Pretend the ACL row was received after this drain began: it is pending, so content must wait.
    gh.fail_next = 1  # the permission sync fails, leaving the ACL row pending
    stats = drain(conn, gh, ORG)
    assert stats.failed == 1
    assert stats.waited_on_acl is True
    assert stats.content_applied == 0
    assert count(conn, "SELECT count(*) FROM cairn.items WHERE source_key LIKE 'github:%%'") == 0
    stats = drain(conn, gh, ORG)  # GitHub is back
    assert stats.acl_applied == 1
    assert stats.content_applied == 1


def test_out_of_order_and_duplicate_deliveries_keep_the_newest_state(
    conn: psycopg.Connection[Any], gh: FakeGitHub
) -> None:
    sync_permissions(conn, gh, ORG)
    d1 = gh.put_pull("payment-service", 9, "t", "old body")
    d2 = gh.put_pull("payment-service", 9, "t", "new body")
    for d in (d2, d1, d2, d1):  # reversed, then duplicated
        receive(conn, d)
    assert (
        count(conn, "SELECT count(*) FROM cairn.inbox WHERE event = 'pull_request'") == 2
    )  # deduped by id
    drain(conn, gh, ORG)
    text = conn.execute(
        "SELECT r.text FROM cairn.revisions r JOIN cairn.items i USING (item_id) "
        "WHERE i.external_id = %s ORDER BY revision_id DESC",
        (f"github:{ORG}/payment-service/pull/9",),
    ).fetchone()
    assert text is not None
    assert "new body" in text[0]
    assert (
        count(conn, "SELECT count(*) FROM cairn.revisions") == 1
    )  # fetch-on-notify reads the newest state once


def test_a_deleted_file_a_deleted_pr_and_a_deleted_repo_are_purged(
    conn: psycopg.Connection[Any], gh: FakeGitHub
) -> None:
    sync_permissions(conn, gh, ORG)
    receive(
        conn, gh.put_file("eng-docs", "design/ledger-kafka.md", "# Ledger Kafka\n\nWe use Kafka.")
    )
    receive(conn, gh.put_pull("payment-service", 1, "t", "body"))
    drain(conn, gh, ORG)
    assert count(conn, "SELECT count(*) FROM cairn.chunks") >= 2
    receive(conn, gh.delete_file("eng-docs", "design/ledger-kafka.md"))
    drain(conn, gh, ORG)
    assert (
        count(conn, "SELECT count(*) FROM cairn.items WHERE external_id LIKE '%%ledger-kafka%%'")
        == 0
    )
    del gh.pulls[("payment-service", 1)]
    receive(
        conn, gh.put_issue("payment-service", 2, "x", "y")
    )  # something to trigger a read of the repo
    gh.put_pull("payment-service", 3, "t", "body")
    receive(conn, gh.delete_repo("payment-service"))
    drain(conn, gh, ORG)
    assert (
        count(
            conn,
            "SELECT count(*) FROM cairn.items WHERE source_key = %s",
            f"github:{ORG}/payment-service",
        )
        == 0
    )
    for table in ("chunks", "revisions", "embeddings"):
        assert (
            count(
                conn,
                f"SELECT count(*) FROM cairn.{table} t WHERE NOT EXISTS (SELECT 1 FROM cairn.items i WHERE i.item_id = t.item_id)"  # noqa: E501, S608
                if table != "embeddings"
                else "SELECT count(*) FROM cairn.embeddings e WHERE NOT EXISTS (SELECT 1 FROM cairn.chunks c WHERE c.chunk_id = e.chunk_id)",  # noqa: E501
            )
            == 0
        )


def test_reconcile_redelivers_failures_and_backfills_what_was_missed(
    conn: psycopg.Connection[Any], gh: FakeGitHub
) -> None:
    sync_permissions(conn, gh, ORG)
    gh.put_pull(
        "payment-service", 11, "missed", "the webhook never arrived"
    )  # no receive(): a lost event
    gh.put_file("eng-docs", "README.md", "# Eng docs\n\nWelcome.")
    gh.delivery_log.append(
        __import__("cairn_connectors.github.models", fromlist=["GhDelivery"]).GhDelivery(
            "failed-1", "pull_request", 500, gh.now()
        )
    )
    out = reconcile(conn, gh, ORG)
    assert out.redelivered == 1
    assert gh.redelivered == ["failed-1"]
    assert out.backfilled >= 2
    pending = conn.execute(
        "SELECT priority FROM cairn.inbox WHERE delivery_id LIKE 'reconcile-%'"
    ).fetchall()
    assert pending
    assert all(p[0] == 2 for p in pending)
    stats = drain(conn, gh, ORG)
    assert stats.content_applied >= 2
    assert (
        count(
            conn,
            "SELECT count(*) FROM cairn.items WHERE external_id = %s",
            f"github:{ORG}/payment-service/pull/11",
        )
        == 1
    )
    assert reconcile(conn, gh, ORG).backfilled == 0  # nothing left to recover


def test_freshness_is_tracked_per_source(conn: psycopg.Connection[Any], gh: FakeGitHub) -> None:
    sync_permissions(conn, gh, ORG)
    receive(conn, gh.put_pull("payment-service", 1, "t", "b"))
    drain(conn, gh, ORG)
    row = conn.execute(
        "SELECT last_event_at, last_synced_at, last_error FROM cairn.sync_sources WHERE source_key = %s",  # noqa: E501
        (f"github:{ORG}/payment-service",),
    ).fetchone()
    assert row is not None
    assert row[0] is not None
    assert row[1] is not None
    assert row[2] is None
    gh.fail_next = 100
    receive(conn, gh.put_pull("payment-service", 2, "t", "b"))
    drain(conn, gh, ORG)
    bad = conn.execute(
        "SELECT last_error FROM cairn.sync_sources WHERE source_key = %s",
        (f"github:{ORG}/payment-service",),
    ).fetchone()
    assert bad is not None
    assert "github" in str(bad[0])
    assert purge_items(conn, []).items == 0
