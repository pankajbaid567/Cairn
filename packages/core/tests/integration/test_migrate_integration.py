"""Migration runner against the compose db (Requirements 6.1 to 6.9; needs `make up-db`).

Read-only checks use the session's ``fresh_db``. Every test that edits the bookkeeping
table, fails a file or removes an extension builds its own throwaway database, so the
shared ``fresh_db`` and the cluster's ``cairn`` database are never touched.
"""

from __future__ import annotations

import threading
import time
from typing import Any

import psycopg
import pytest

from cairn_core.migrate import (
    ChecksumMismatch,
    MigrationFailed,
    UnknownAppliedMigration,
    advisory_lock,
    bundled_migrations,
    migrate,
    parse_migration,
    read_applied,
)
from cairn_core.testing import DbTestConfig, FreshDb, temporary_database

pytestmark = pytest.mark.integration

BUNDLED = bundled_migrations()
# Test files come after the last bundled version, so they stay valid when a migration is added.
NEXT = BUNDLED[-1].version + 1
BUNDLED_VERSIONS = [m.version for m in BUNDLED]
PROBE = parse_migration(
    f"{NEXT:04d}_probe.sql",
    b"CREATE TABLE cairn.probe (id integer);\nGRANT SELECT ON cairn.probe TO cairn_app;\n",
)
FAILING = parse_migration(
    f"{NEXT:04d}_failing.sql", b"CREATE TABLE cairn.rollback_probe (id integer);\nSELECT 1 / 0;\n"
)


def _exists(conn: psycopg.Connection[Any], relation: str) -> bool:
    row = conn.execute("SELECT to_regclass(%s) IS NOT NULL", (relation,)).fetchone()
    assert row is not None
    return bool(row[0])


def _recorded_versions(conn: psycopg.Connection[Any]) -> list[int]:
    return [a.version for a in read_applied(conn)]


def test_fresh_database_records_the_baseline(fresh_db: FreshDb) -> None:
    """6.1 and 6.2: version, name and sha256 of the file, applied by the owner role."""
    with fresh_db.connect_owner() as conn:
        rows = conn.execute(
            "SELECT version, name, checksum, applied_by FROM cairn_meta.schema_migrations "
            "ORDER BY version"
        ).fetchall()
    assert rows == [(m.version, m.name, m.checksum, "cairn_owner") for m in BUNDLED]
    assert rows[0][:2] == (1, "baseline")


def test_second_run_applies_nothing(fresh_db: FreshDb) -> None:
    """6.3: nothing applied, and the recorded rows (including applied_at) are unchanged."""
    with fresh_db.connect_owner() as conn:
        before = read_applied(conn)
        assert migrate(conn, BUNDLED) == []
        assert read_applied(conn) == before


def test_edited_checksum_aborts_and_applies_no_file(
    admin_conn: psycopg.Connection[Any], admin_config: DbTestConfig
) -> None:
    """6.4: the version is named, and a pending file after it does not run."""
    with temporary_database(admin_conn, admin_config) as db, db.connect_owner() as conn:
        conn.execute("UPDATE cairn_meta.schema_migrations SET checksum = repeat('0', 64)")
        with pytest.raises(ChecksumMismatch) as raised:
            migrate(conn, [*BUNDLED, PROBE])
        assert raised.value.version == 1
        assert not _exists(conn, "cairn.probe")
        assert _recorded_versions(conn) == BUNDLED_VERSIONS


def test_database_ahead_of_the_code_aborts(
    admin_conn: psycopg.Connection[Any], admin_config: DbTestConfig
) -> None:
    """6.5: a recorded version with no bundled file; pending files do not run."""
    with temporary_database(admin_conn, admin_config) as db, db.connect_owner() as conn:
        conn.execute(
            "INSERT INTO cairn_meta.schema_migrations (version, name, checksum) "
            "VALUES (99, 'from_the_future', repeat('a', 64))"
        )
        with pytest.raises(UnknownAppliedMigration):
            migrate(conn, [*BUNDLED, PROBE])
        assert not _exists(conn, "cairn.probe")
        assert _recorded_versions(conn) == [*BUNDLED_VERSIONS, 99]


def test_failing_file_rolls_back_and_records_nothing(
    admin_conn: psycopg.Connection[Any], admin_config: DbTestConfig
) -> None:
    """6.6: the table the file created before it failed is gone; only bundled files are recorded."""
    with temporary_database(admin_conn, admin_config) as db, db.connect_owner() as conn:
        with pytest.raises(MigrationFailed) as raised:
            migrate(conn, [*BUNDLED, FAILING])
        assert raised.value.version == NEXT
        assert not _exists(conn, "cairn.rollback_probe")
        assert _recorded_versions(conn) == BUNDLED_VERSIONS
        # The lock was released: a later run plans again and applies the good file.
        assert migrate(conn, [*BUNDLED, PROBE]) == [NEXT]


def test_concurrent_run_waits_for_the_lock_and_then_plans(
    admin_conn: psycopg.Connection[Any], admin_config: DbTestConfig
) -> None:
    """6.7: while one run holds the lock a second run blocks before planning, then sees the
    first run's work and applies nothing."""
    with temporary_database(admin_conn, admin_config, apply_migrations=False) as db:
        result: dict[str, list[int] | BaseException] = {}

        def second_run() -> None:
            try:
                with db.connect_owner() as conn:
                    result["applied"] = migrate(conn, BUNDLED)
            except BaseException as exc:
                result["error"] = exc

        def waiting_for_lock() -> int:
            row = admin_conn.execute(
                "SELECT count(*) FROM pg_locks l JOIN pg_database d ON d.oid = l.database "
                "WHERE l.locktype = 'advisory' AND NOT l.granted AND d.datname = %s",
                (db.name,),
            ).fetchone()
            assert row is not None
            return int(row[0])

        with db.connect_owner() as first:
            thread = threading.Thread(target=second_run, daemon=True)
            with advisory_lock(first):
                thread.start()
                deadline = time.monotonic() + 15
                while waiting_for_lock() == 0 and time.monotonic() < deadline:
                    time.sleep(0.05)
                assert waiting_for_lock() == 1, "the second run did not wait for the lock"
                assert thread.is_alive()
                assert not result
                # Re-entering the session lock is allowed; the work happens under the hold.
                assert migrate(first, BUNDLED) == BUNDLED_VERSIONS
                assert thread.is_alive(), "the second run got through while the lock was held"
            thread.join(timeout=15)
        assert not thread.is_alive()
        assert "error" not in result, result.get("error")
        assert result["applied"] == []


def test_baseline_grants(fresh_db: FreshDb) -> None:
    """6.8: schema cairn belongs to the owner; the app role gets exactly the baseline grants."""
    with fresh_db.connect_superuser() as conn:
        owner = conn.execute(
            "SELECT r.rolname FROM pg_namespace n JOIN pg_roles r ON r.oid = n.nspowner "
            "WHERE n.nspname = 'cairn'"
        ).fetchone()
        assert owner == ("cairn_owner",)

        def has(function: str, target: str, privilege: str) -> bool:
            row = conn.execute(
                f"SELECT {function}('cairn_app', %s, %s)",
                (target, privilege),
            ).fetchone()
            assert row is not None
            return bool(row[0])

        assert has("has_schema_privilege", "cairn", "USAGE")
        assert has("has_schema_privilege", "cairn_meta", "USAGE")
        assert not has("has_schema_privilege", "cairn", "CREATE")
        assert not has("has_schema_privilege", "cairn_meta", "CREATE")
        assert has("has_table_privilege", "cairn_meta.schema_migrations", "SELECT")
        assert not has("has_table_privilege", "cairn_meta.schema_migrations", "INSERT")
        assert not has("has_table_privilege", "cairn_meta.schema_migrations", "UPDATE")
    # And it works for real, as the app role itself.
    with fresh_db.connect_app() as app:
        row = app.execute("SELECT max(version) FROM cairn_meta.schema_migrations").fetchone()
        assert row == (BUNDLED[-1].version,)
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            app.execute("CREATE TABLE cairn.app_cannot_create (id integer)")


@pytest.mark.parametrize(
    "dropped",
    [("btree_gist",), ("vector", "btree_gist")],
    ids=["one-missing", "all-missing"],
)
def test_missing_extensions_fail_the_baseline_and_are_named(
    dropped: tuple[str, ...],
    admin_conn: psycopg.Connection[Any],
    admin_config: DbTestConfig,
) -> None:
    """6.9: the message names every missing extension and nothing else."""
    with temporary_database(admin_conn, admin_config, apply_migrations=False) as db:
        with db.connect_superuser() as su:
            for extension in dropped:
                su.execute(f'DROP EXTENSION "{extension}"')
        with db.connect_owner() as conn, pytest.raises(MigrationFailed) as raised:
            migrate(conn, BUNDLED)
        reason = raised.value.reason
        for extension in dropped:
            assert extension in reason
        for extension in {"vector", "btree_gist"} - set(dropped):
            assert extension not in reason
        with db.connect_owner() as conn:
            assert _recorded_versions(conn) == []
            assert not _exists(conn, "cairn.probe")
