"""Store behaviour against a real database as ``cairn_ingest`` (tests 1.18, Properties 2, 8-10)."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from typing import Any

import psycopg
import pytest

from cairn_connectors.chat_export import ChannelGroup
from cairn_connectors.documents import (
    AclRef,
    DirectorySnapshot,
    DirGroup,
    DirMembership,
    DirUser,
    SourceDocument,
)
from cairn_connectors.normalize import RevisionPlan
from cairn_connectors.store import (
    ACL_EMPTY,
    ACL_UNRESOLVED,
    AclError,
    apply_chat_groups,
    apply_directory,
    import_document,
    resolve_acl,
)
from cairn_core.testing import DbTestConfig, FreshDb, temporary_database

pytestmark = pytest.mark.integration

T0 = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)


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


def directory(extra: tuple[DirMembership, ...] = ()) -> DirectorySnapshot:
    return DirectorySnapshot(
        date(2026, 10, 27),
        (DirUser("ben", "Ben"), DirUser("asha", "Asha"), DirUser("rook", "Rook", True)),
        (DirGroup("team:payments", "Payments"), DirGroup("team:mobile", "Mobile")),
        (
            DirMembership("asha", "team:payments", date(2025, 1, 6)),
            DirMembership("ben", "team:mobile", date(2026, 3, 2), date(2026, 9, 1)),
            DirMembership("ben", "team:payments", date(2026, 9, 1)),
            *extra,
        ),
    )


def doc(
    text: str = "# Title\n\nHello world.",
    *,
    acl: frozenset[AclRef] | None = None,
    at: datetime = T0,
    ext: str = "doc:eng-docs/a.md",
    title: str = "Doc A",
) -> SourceDocument:
    return SourceDocument(
        "docs_folder",
        ext,
        "doc",
        title,
        text,
        at,
        acl if acl is not None else frozenset({AclRef("team", "payments")}),
        {"repo": "eng-docs"},
    )


def count(conn: psycopg.Connection[Any], table: str, where: str = "true") -> int:
    row = conn.execute(f"SELECT count(*) FROM cairn.{table} WHERE {where}").fetchone()  # noqa: S608
    assert row is not None
    return int(row[0])


def test_directory_is_idempotent_and_versions_acls(conn: psycopg.Connection[Any]) -> None:
    first = apply_directory(conn, directory())
    assert first.principals_added == 5
    assert first.memberships_added == 3
    assert first.changed
    versions = count(conn, "acl_versions")
    second = apply_directory(conn, directory())
    assert not second.changed
    assert count(conn, "acl_versions") == versions  # no change, no new version
    # an end date change is a membership change and one new version
    changed = apply_directory(
        conn, directory(extra=(DirMembership("asha", "team:mobile", date(2026, 1, 1)),))
    )
    assert changed.memberships_added == 1
    assert count(conn, "acl_versions") == versions + 1


def test_directory_removes_memberships_absent_from_the_snapshot(
    conn: psycopg.Connection[Any],
) -> None:
    apply_directory(conn, directory())
    smaller = DirectorySnapshot(
        date(2026, 10, 27), directory().users, directory().groups, directory().memberships[:1]
    )
    out = apply_directory(conn, smaller)
    assert out.memberships_removed == 2
    assert count(conn, "memberships", "source = 'directory'") == 1


def test_resolve_acl(conn: psycopg.Connection[Any]) -> None:
    apply_directory(conn, directory())
    ok = resolve_acl(conn, [AclRef("team", "payments"), AclRef("user", "ben")])
    assert isinstance(ok, tuple)
    assert list(ok) == sorted(ok)
    assert len(ok) == 2
    bad = resolve_acl(conn, [AclRef("team", "payments"), AclRef("team", "ghosts")])
    assert isinstance(bad, AclError)
    assert bad.code == ACL_UNRESOLVED
    assert bad.missing == ("team:ghosts",)
    assert resolve_acl(conn, []) == AclError(ACL_EMPTY)


def test_reimport_creates_no_revision_and_one_edit_creates_exactly_one(
    conn: psycopg.Connection[Any],
) -> None:
    apply_directory(conn, directory())
    a = import_document(conn, doc())
    assert a.revision is RevisionPlan.INSERT
    assert a.chunks == 1
    b = import_document(conn, doc())
    assert b.revision is RevisionPlan.SKIP_UNCHANGED
    assert not b.acl_changed
    assert count(conn, "revisions") == 1
    assert count(conn, "chunks") == 1
    c = import_document(conn, doc("# Title\n\nHello again.", at=T0 + timedelta(days=1)))
    assert c.revision is RevisionPlan.INSERT
    assert count(conn, "revisions") == 2
    assert count(conn, "chunks", "is_current") == 1
    assert count(conn, "chunks") == 2
    assert count(conn, "outbox", "dispatched_at IS NULL") == 2


def test_older_source_time_is_rejected(conn: psycopg.Connection[Any]) -> None:
    apply_directory(conn, directory())
    import_document(conn, doc(at=T0))
    out = import_document(conn, doc("# Title\n\nOlder text.", at=T0 - timedelta(days=1)))
    assert out.revision is RevisionPlan.REJECT_OLDER
    assert count(conn, "revisions") == 1


def test_chunk_text_equals_revision_slice(conn: psycopg.Connection[Any]) -> None:
    apply_directory(conn, directory())
    body = "# One\n\n" + "alpha beta gamma. " * 80 + "\n\n## Two\n\nshort section"
    import_document(conn, doc(body))
    rows = conn.execute(
        "SELECT c.text, substr(r.text, c.start_offset + 1, c.end_offset - c.start_offset) "
        "FROM cairn.chunks c JOIN cairn.revisions r USING (revision_id)"
    ).fetchall()
    assert len(rows) >= 2
    assert all(a == b for a, b in rows)


def test_unknown_acl_rejects_the_document_and_writes_nothing(conn: psycopg.Connection[Any]) -> None:
    apply_directory(conn, directory())
    out = import_document(conn, doc(acl=frozenset({AclRef("team", "ghosts")})))
    assert out.error == ACL_UNRESOLVED
    assert out.revision is None
    assert count(conn, "items") == 0
    assert count(conn, "revisions") == 0


def test_acl_change_updates_every_copy_and_bumps_the_version(conn: psycopg.Connection[Any]) -> None:
    apply_directory(conn, directory())
    import_document(conn, doc())
    before = count(conn, "acl_versions")
    out = import_document(
        conn, doc(acl=frozenset({AclRef("team", "mobile"), AclRef("user", "ben")}))
    )
    assert out.acl_changed
    assert out.revision is RevisionPlan.SKIP_UNCHANGED
    assert count(conn, "acl_versions") == before + 1
    item_acl = conn.execute("SELECT acl FROM cairn.items").fetchone()
    assert item_acl is not None
    expected = [
        int(r[0])
        for r in conn.execute("SELECT principal_id FROM cairn.item_acls ORDER BY 1").fetchall()
    ]
    assert list(item_acl[0]) == expected
    for table in ("revisions", "chunks"):
        distinct = conn.execute(f"SELECT DISTINCT acl FROM cairn.{table}").fetchall()  # noqa: S608
        assert [list(r[0]) for r in distinct] == [expected]


def test_title_only_change_updates_chunk_titles_without_a_revision(
    conn: psycopg.Connection[Any],
) -> None:
    apply_directory(conn, directory())
    import_document(conn, doc(title="Old title"))
    import_document(conn, doc(title="New title"))
    assert count(conn, "revisions") == 1
    assert count(conn, "chunks", "title = 'New title'") == 1


def test_chat_groups_write_channel_memberships(conn: psycopg.Connection[Any]) -> None:
    apply_directory(conn, directory())
    group = ChannelGroup("incidents", 1_770_000_000, ("asha", "ben"))
    out = apply_chat_groups(conn, [group])
    assert out.principals_added == 1
    assert out.memberships_added == 2
    assert not apply_chat_groups(conn, [group]).changed
    with pytest.raises(ValueError, match="unknown member"):
        apply_chat_groups(conn, [ChannelGroup("x", 1_770_000_000, ("nobody",))])
