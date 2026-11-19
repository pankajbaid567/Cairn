"""Unit tests for cairn_core.migrate and the cairn-db migrate/status commands.

No database is needed. Discovery and planning are pure. ``migrate()`` is checked
against a small recording connection that mimics only what the runner relies on
(autocommit, ``transaction()``, the lock, the bookkeeping table). The runner's
behavior against a real Postgres is covered by the integration tests in task 4.2.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import psycopg
import pytest
from click.testing import CliRunner

from cairn_core import cli
from cairn_core.config import AdminSettings
from cairn_core.migrate import (
    NO_TRANSACTION_DIRECTIVE,
    AppliedMigration,
    ChecksumMismatch,
    InvalidMigrationFile,
    Migration,
    MigrationFailed,
    MigrationGap,
    UnknownAppliedMigration,
    bundled_migrations,
    discover,
    head,
    migrate,
    parse_migration,
    plan,
    read_applied,
    status,
)

NOW = datetime(2026, 10, 27, 9, 0, tzinfo=UTC)

PREVIOUS_BASELINE_CHECKSUM = "5f77c7547931c0c4a563e51937905cbe37962baee0e43858f228f0c4670e3b1e"
"""sha256 of ``0001_baseline.sql`` before its one-time in-place edit (retarget FT-04, 2 Nov 2026),
when the extension check still listed three extensions. Only the retired ``cairn_pgdata`` volume
records it (Phase 0 task 4.1 notes: ``applied  0001  baseline  5f77c7547931``)."""


def mig(version: int, name: str = "step", sql: str | None = None) -> Migration:
    text = sql or f"SELECT {version};\n"
    return parse_migration(f"{version:04d}_{name}.sql", text.encode())


def rec(m: Migration, *, checksum: str | None = None) -> AppliedMigration:
    return AppliedMigration(m.version, m.name, checksum or m.checksum, NOW)


# --- discovery ---------------------------------------------------------------------------


def test_parse_computes_sha256_of_the_file_bytes() -> None:
    data = b"CREATE TABLE t (a int);\n"
    m = parse_migration("0003_add_t.sql", data)
    assert (m.version, m.name, m.transactional) == (3, "add_t", True)
    assert m.checksum == hashlib.sha256(data).hexdigest()
    assert m.sql == data.decode()
    assert m.filename == "0003_add_t.sql"


def test_checksum_changes_with_any_byte() -> None:
    assert mig(1, sql="SELECT 1;\n").checksum != mig(1, sql="SELECT 1; \n").checksum


@pytest.mark.parametrize(
    "filename",
    [
        "1_step.sql",  # not four digits
        "00001_step.sql",
        "0001-step.sql",
        "0001_Step.sql",  # uppercase
        "0001_.sql",
        "0001_1step.sql",  # name must start with a letter
        "step.sql",
        "0000_zero.sql",  # versions start at 1
    ],
)
def test_bad_file_names_are_rejected(filename: str) -> None:
    with pytest.raises(InvalidMigrationFile, match="invalid migration file"):
        parse_migration(filename, b"SELECT 1;")


def test_empty_and_non_utf8_files_are_rejected() -> None:
    with pytest.raises(InvalidMigrationFile, match="empty"):
        parse_migration("0001_x.sql", b" \n\n")
    with pytest.raises(InvalidMigrationFile, match="UTF-8"):
        parse_migration("0001_x.sql", b"\xff\xfe\x00")


def test_no_transaction_directive_must_be_the_first_line() -> None:
    first = parse_migration("0002_idx.sql", f"{NO_TRANSACTION_DIRECTIVE}\nSELECT 1;\n".encode())
    assert first.transactional is False
    padded = parse_migration("0002_idx.sql", f"{NO_TRANSACTION_DIRECTIVE}  \r\nSELECT 1;".encode())
    assert padded.transactional is False
    later = parse_migration("0002_idx.sql", f"SELECT 1;\n{NO_TRANSACTION_DIRECTIVE}\n".encode())
    assert later.transactional is True


def test_discover_sorts_by_version_and_ignores_other_files(tmp_path: Path) -> None:
    (tmp_path / "0002_second.sql").write_text("SELECT 2;")
    (tmp_path / "0001_first.sql").write_text("SELECT 1;")
    (tmp_path / "README.md").write_text("not a migration")
    (tmp_path / "sub").mkdir()
    found = discover(tmp_path)
    assert [(m.version, m.name) for m in found] == [(1, "first"), (2, "second")]


def test_discover_rejects_a_badly_named_sql_file(tmp_path: Path) -> None:
    (tmp_path / "add_table.sql").write_text("SELECT 1;")
    with pytest.raises(InvalidMigrationFile):
        discover(tmp_path)


def test_the_baseline_ships_as_package_data() -> None:
    migrations = bundled_migrations()
    assert migrations[0].filename == "0001_baseline.sql"
    assert migrations[0].transactional is True
    assert head(migrations) >= 1
    sql = migrations[0].sql
    # Requirements 6.8 and 6.9: extension check, schema cairn for the owner, app grants.
    assert "ARRAY['btree_gist', 'vector']" in sql  # exactly the two required extensions
    for needle in (
        "missing extensions",
        "CREATE SCHEMA cairn AUTHORIZATION cairn_owner",
        "GRANT USAGE ON SCHEMA cairn TO cairn_app",
        "GRANT USAGE ON SCHEMA cairn_meta TO cairn_app",
        "GRANT SELECT ON cairn_meta.schema_migrations TO cairn_app",
    ):
        assert needle in sql
    assert bundled_migrations() == migrations  # discovery is deterministic


def test_provider_usage_migration_ships_as_package_data() -> None:
    # Requirements 15.5 and 15.6: the budget table, granted to the app role without DELETE.
    migrations = bundled_migrations()
    usage = migrations[1]
    assert (usage.version, usage.name, usage.transactional) == (2, "provider_usage", True)
    for needle in (
        "CREATE TABLE cairn_meta.provider_usage",
        "PRIMARY KEY (day, provider)",
        "REVOKE ALL ON cairn_meta.provider_usage FROM PUBLIC",
        "GRANT SELECT, INSERT, UPDATE ON cairn_meta.provider_usage TO cairn_app",
    ):
        assert needle in usage.sql
    assert "GRANT DELETE" not in usage.sql
    assert head(migrations) >= 2


def test_a_database_with_the_previous_baseline_checksum_stops_with_checksum_mismatch() -> None:
    # Requirement 2.7: the edited baseline no longer matches what the retired volume recorded.
    migrations = bundled_migrations()
    baseline = migrations[0]
    assert baseline.checksum != PREVIOUS_BASELINE_CHECKSUM
    old = AppliedMigration(1, "baseline", PREVIOUS_BASELINE_CHECKSUM, NOW)
    with pytest.raises(ChecksumMismatch, match="version 1") as info:
        plan(migrations, [old])
    assert info.value.version == 1
    conn = FakeConn([old])
    with pytest.raises(ChecksumMismatch):
        migrate(as_conn(conn), migrations)
    assert not [e for e in conn.events if e.startswith(("sql:", "record:"))]


def test_the_bundled_set_plans_cleanly_on_an_empty_database() -> None:
    migrations = bundled_migrations()
    assert plan(migrations, []) == migrations


# --- planning ----------------------------------------------------------------------------


def test_head_of_nothing_is_zero() -> None:
    assert head([]) == 0
    assert head([mig(1), mig(2)]) == 2


def test_plan_on_an_empty_database_returns_everything_in_order() -> None:
    ms = [mig(1), mig(2), mig(3)]
    assert plan(ms, []) == ms


def test_plan_returns_the_pending_suffix() -> None:
    ms = [mig(1), mig(2), mig(3)]
    assert plan(ms, [rec(ms[0])]) == ms[1:]
    assert plan(ms, [rec(ms[1]), rec(ms[0])]) == ms[2:]  # recorded order does not matter


def test_plan_applies_nothing_when_all_are_recorded() -> None:
    ms = [mig(1), mig(2)]
    assert plan(ms, [rec(m) for m in ms]) == []


def test_plan_does_not_modify_its_inputs() -> None:
    ms = [mig(2), mig(1)]
    applied = [rec(ms[1])]
    plan(ms, applied)
    assert ms == [mig(2), mig(1)]
    assert applied == [rec(ms[1])]


def test_plan_names_the_version_on_a_checksum_mismatch() -> None:
    ms = [mig(1), mig(2)]
    with pytest.raises(ChecksumMismatch, match="version 2") as info:
        plan(ms, [rec(ms[0]), rec(ms[1], checksum="0" * 64)])
    assert info.value.version == 2


def test_plan_refuses_a_database_that_is_ahead() -> None:
    ms = [mig(1)]
    with pytest.raises(UnknownAppliedMigration, match="version 2") as info:
        plan(ms, [rec(ms[0]), rec(mig(2))])
    assert info.value.version == 2


def test_plan_refuses_gaps_and_duplicates_in_bundled_files() -> None:
    with pytest.raises(MigrationGap, match="bundled"):
        plan([mig(1), mig(3)], [])
    with pytest.raises(MigrationGap, match="bundled"):
        plan([mig(2), mig(3)], [])  # does not start at 1
    with pytest.raises(MigrationGap, match="bundled"):
        plan([mig(1, "a"), mig(1, "b")], [])


def test_plan_refuses_a_gap_in_recorded_versions() -> None:
    ms = [mig(1), mig(2), mig(3)]
    with pytest.raises(MigrationGap, match="recorded"):
        plan(ms, [rec(ms[0]), rec(ms[2])])
    with pytest.raises(MigrationGap, match="recorded"):
        plan(ms, [rec(ms[1])])


def test_status_reports_counts() -> None:
    ms = [mig(1), mig(2), mig(3)]
    report = status(ms, [rec(ms[0])])
    assert (report.head, report.current) == (3, 1)
    assert [a.version for a in report.applied] == [1]
    assert [m.version for m in report.pending] == [2, 3]
    empty = status([], [])
    assert (empty.head, empty.current, empty.pending) == (0, 0, ())


# --- migrate() against a recording connection ------------------------------------------------


class FakeResult:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self._rows = rows

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[tuple[Any, ...]]:
        return list(self._rows)


class FakeConn:
    """Records what ``migrate`` does. Rows written inside a failed transaction vanish."""

    def __init__(
        self,
        applied: list[AppliedMigration] | None = None,
        *,
        fail_on: str | None = None,
        autocommit: bool = True,
        table_exists: bool = True,
    ) -> None:
        self.autocommit = autocommit
        self.events: list[str] = []
        self.rows: dict[int, tuple[str, str]] = {
            a.version: (a.name, a.checksum) for a in applied or []
        }
        self.table_exists = table_exists
        self.fail_on = fail_on
        self._pending: list[tuple[int, str, str]] | None = None

    @contextmanager
    def transaction(self) -> Iterator[None]:
        assert self._pending is None, "nested transaction"
        self.events.append("begin")
        self._pending = []
        try:
            yield
        except BaseException:
            self._pending = None
            self.events.append("rollback")
            raise
        for version, name, checksum in self._pending:
            self.rows[version] = (name, checksum)
        self._pending = None
        self.events.append("commit")

    def execute(self, query: str, params: tuple[Any, ...] | None = None) -> FakeResult:
        text = str(query)
        if "pg_advisory_unlock" in text:
            self.events.append("unlock")
        elif "pg_advisory_lock" in text:
            assert params == ("cairn_migrate",)
            self.events.append("lock")
        elif "CREATE SCHEMA IF NOT EXISTS cairn_meta" in text:
            self.events.append("bookkeeping")
            self.table_exists = True
        elif "to_regclass" in text:
            return FakeResult([(self.table_exists,)])
        elif "FROM cairn_meta.schema_migrations" in text:
            return FakeResult(
                [(v, n, c, NOW) for v, (n, c) in sorted(self.rows.items())],
            )
        elif "INSERT INTO cairn_meta.schema_migrations" in text:
            assert params is not None
            version, name, checksum = params
            self.events.append(f"record:{version}")
            if self._pending is None:
                self.rows[version] = (name, checksum)
            else:
                self._pending.append((version, name, checksum))
        else:
            self.events.append(f"sql:{text.strip()}")
            if self.fail_on and self.fail_on in text:
                raise psycopg.errors.UndefinedTable("relation does not exist")
        return FakeResult([])


def as_conn(fake: FakeConn) -> psycopg.Connection[Any]:
    return cast("psycopg.Connection[Any]", fake)


def test_migrate_applies_pending_files_in_order_under_the_lock() -> None:
    ms = [mig(1), mig(2), mig(3)]
    conn = FakeConn([rec(ms[0])])
    assert migrate(as_conn(conn), ms) == [2, 3]
    assert conn.events == [
        "lock",
        "begin",
        "bookkeeping",
        "commit",
        "begin",
        "sql:SELECT 2;",
        "record:2",
        "commit",
        "begin",
        "sql:SELECT 3;",
        "record:3",
        "commit",
        "unlock",
    ]
    assert sorted(conn.rows) == [1, 2, 3]


def test_a_second_migrate_applies_nothing() -> None:
    ms = [mig(1), mig(2)]
    conn = FakeConn(table_exists=False)
    assert migrate(as_conn(conn), ms) == [1, 2]
    conn.events.clear()
    assert migrate(as_conn(conn), ms) == []
    assert not [e for e in conn.events if e.startswith(("sql:", "record:"))]
    assert conn.events[0] == "lock"
    assert conn.events[-1] == "unlock"


def test_a_failing_file_rolls_back_records_nothing_and_stops() -> None:
    ms = [mig(1), mig(2, sql="SELECT boom;\n"), mig(3)]
    conn = FakeConn(fail_on="boom")
    with pytest.raises(MigrationFailed, match=r"0002_step failed: UndefinedTable") as info:
        migrate(as_conn(conn), ms)
    assert info.value.version == 2
    assert sorted(conn.rows) == [1]  # file 2 recorded nothing, file 3 never ran
    assert "sql:SELECT 3;" not in conn.events
    assert conn.events[-2:] == ["rollback", "unlock"]  # the lock is released on failure


def test_checksum_mismatch_stops_before_any_file_runs() -> None:
    ms = [mig(1), mig(2)]
    conn = FakeConn([rec(ms[0], checksum="f" * 64)])
    with pytest.raises(ChecksumMismatch, match="version 1"):
        migrate(as_conn(conn), ms)
    assert not [e for e in conn.events if e.startswith(("sql:", "record:"))]
    assert conn.events[-1] == "unlock"


def test_a_database_ahead_of_the_code_runs_nothing() -> None:
    conn = FakeConn([rec(mig(1)), rec(mig(2))])
    with pytest.raises(UnknownAppliedMigration):
        migrate(as_conn(conn), [mig(1)])
    assert not [e for e in conn.events if e.startswith(("sql:", "record:"))]


def test_a_gap_in_bundled_files_runs_nothing() -> None:
    conn = FakeConn()
    with pytest.raises(MigrationGap):
        migrate(as_conn(conn), [mig(1), mig(3)])
    assert not [e for e in conn.events if e.startswith(("sql:", "record:"))]


def test_no_transaction_files_run_outside_a_transaction() -> None:
    concurrent = parse_migration(
        "0002_idx.sql",
        f"{NO_TRANSACTION_DIRECTIVE}\nCREATE INDEX CONCURRENTLY i ON t (a);\n".encode(),
    )
    conn = FakeConn()
    assert migrate(as_conn(conn), [mig(1), concurrent]) == [1, 2]
    after_first = conn.events[conn.events.index("record:1") :]
    assert after_first == [
        "record:1",
        "commit",
        f"sql:{NO_TRANSACTION_DIRECTIVE}\nCREATE INDEX CONCURRENTLY i ON t (a);",
        "record:2",
        "unlock",
    ]


def test_migrate_requires_an_autocommit_connection() -> None:
    conn = FakeConn(autocommit=False)
    with pytest.raises(ValueError, match="autocommit"):
        migrate(as_conn(conn), [mig(1)])
    assert conn.events == []


def test_read_applied_is_empty_when_the_table_is_missing() -> None:
    assert read_applied(as_conn(FakeConn(table_exists=False))) == []
    conn = FakeConn([rec(mig(2)), rec(mig(1))])
    assert [a.version for a in read_applied(as_conn(conn))] == [1, 2]


# --- cairn-db migrate / status -------------------------------------------------------------


SECRET = "owner-pw-FAKE-0123456789"


@pytest.fixture
def admin_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for alias in {f.alias for f in AdminSettings.model_fields.values() if f.alias is not None}:
        monkeypatch.delenv(alias, raising=False)
    monkeypatch.chdir(tmp_path)  # no .env here
    monkeypatch.setenv("CAIRN_OWNER_DB_PASSWORD", SECRET)
    monkeypatch.setenv("CAIRN_APP_DB_PASSWORD", "app-pw-FAKE-0123456789")
    monkeypatch.setenv("CAIRN_DBOS_DB_PASSWORD", "dbos-pw-FAKE-0123456789")


class ClosingFake(FakeConn):
    """FakeConn that also works as a context manager, like psycopg.Connection."""

    def __enter__(self) -> ClosingFake:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


def use_fake(monkeypatch: pytest.MonkeyPatch, fake: ClosingFake) -> list[dict[str, str]]:
    seen: list[dict[str, str]] = []

    def connect_as(settings: AdminSettings, *, user: str, password: str, **_: Any) -> ClosingFake:
        seen.append({"user": user, "password": password})
        return fake

    monkeypatch.setattr(cli, "connect_as", connect_as)
    return seen


def test_cli_migrate_applies_then_reports_up_to_date(
    admin_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = ClosingFake(table_exists=False)
    seen = use_fake(monkeypatch, fake)
    runner = CliRunner()
    first = runner.invoke(cli.main, ["migrate"])
    assert first.exit_code == 0, first.output
    assert "applied 0001_baseline.sql" in first.output
    assert seen == [{"user": "cairn_owner", "password": SECRET}]  # owner role, not app
    second = runner.invoke(cli.main, ["migrate"])
    assert second.exit_code == 0
    assert "nothing applied" in second.output
    assert "applied 0001" not in second.output
    assert SECRET not in first.output + second.output


def test_cli_status_lists_applied_and_pending(
    admin_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundled = bundled_migrations()
    baseline = bundled[0]
    use_fake(monkeypatch, ClosingFake([rec(m) for m in bundled]))
    result = CliRunner().invoke(cli.main, ["status"])
    assert result.exit_code == 0, result.output
    assert f"head: {head(bundled)}  current: {head(bundled)}  pending: 0" in result.output
    assert f"applied  0001  baseline  {baseline.checksum[:12]}" in result.output

    use_fake(monkeypatch, ClosingFake(table_exists=False))
    fresh = CliRunner().invoke(cli.main, ["status"])
    assert fresh.exit_code == 0
    assert "pending  0001  baseline" in fresh.output


def test_cli_exits_one_and_names_the_version_on_drift(
    admin_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    baseline = bundled_migrations()[0]
    for command in ("migrate", "status"):
        use_fake(monkeypatch, ClosingFake([rec(baseline, checksum="a" * 64)]))
        result = CliRunner().invoke(cli.main, [command])
        assert result.exit_code == 1
        assert "version 1" in result.output
        assert SECRET not in result.output


def test_cli_exits_one_on_a_database_error_without_details(
    admin_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(settings: AdminSettings, **_: Any) -> ClosingFake:
        raise psycopg.OperationalError(f"connection to host 10.0.0.9 failed, password={SECRET}")

    monkeypatch.setattr(cli, "connect_as", refuse)
    result = CliRunner().invoke(cli.main, ["migrate"])
    assert result.exit_code == 1
    assert "database error: OperationalError" in result.output
    assert SECRET not in result.output
    assert "10.0.0.9" not in result.output


def test_cli_exits_two_on_missing_configuration(
    admin_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CAIRN_OWNER_DB_PASSWORD")
    result = CliRunner().invoke(cli.main, ["migrate"])
    assert result.exit_code == 2
    assert "CAIRN_OWNER_DB_PASSWORD" in result.output


def test_cli_lists_its_commands() -> None:
    result = CliRunner().invoke(cli.main, ["--help"])
    assert result.exit_code == 0
    assert "migrate" in result.output
    assert "status" in result.output
