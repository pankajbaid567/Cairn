"""SQL-first, checksummed, forward-only migration runner (design §6, PD-04).

Migrations are plain ``NNNN_name.sql`` files shipped as package data under
``cairn_core/migrations/sql``. The runner records each applied file's version,
name and sha256 checksum in ``cairn_meta.schema_migrations``.

The module has a pure half and a database half:

- Pure, testable without a database: ``parse_migration``, ``discover``, ``plan``,
  ``head`` and ``status``. ``plan`` compares two lists and either returns the
  pending suffix or raises; it never touches its inputs.
- Database: ``read_applied`` and ``migrate``. ``migrate`` needs an **autocommit**
  connection (it opens its own transactions) that belongs to the owner role.

Rules (Requirement 6):

- Versions start at 1 and are contiguous. A gap, a duplicate, a recorded version
  with no bundled file, or an edited (checksum-changed) applied file stops the run
  before any file executes.
- One session advisory lock covers planning and applying, so a concurrent run
  waits and then finds nothing pending.
- Each transactional file runs in one transaction together with its bookkeeping
  row. A failure rolls both back and raises ``MigrationFailed``.
- A file whose first line is ``-- cairn:no-transaction`` runs outside a
  transaction (for ``CREATE INDEX CONCURRENTLY``). It must hold a single
  statement, and it must be safe to run again, because the bookkeeping row is
  written afterwards and a crash in between leaves the file pending.
- There are no down migrations. Fix forward with a new file.
"""

from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from importlib.resources import files
from importlib.resources.abc import Traversable
from typing import Any

import psycopg

__all__ = [
    "BOOKKEEPING_TABLE",
    "LOCK_NAME",
    "NO_TRANSACTION_DIRECTIVE",
    "AppliedMigration",
    "ChecksumMismatch",
    "InvalidMigrationFile",
    "Migration",
    "MigrationError",
    "MigrationFailed",
    "MigrationGap",
    "MigrationStatus",
    "UnknownAppliedMigration",
    "advisory_lock",
    "bundled_migrations",
    "discover",
    "head",
    "migrate",
    "parse_migration",
    "plan",
    "read_applied",
    "status",
]

log = logging.getLogger(__name__)

BOOKKEEPING_TABLE = "cairn_meta.schema_migrations"
LOCK_NAME = "cairn_migrate"
"""Name hashed with ``hashtext`` to get the advisory lock key."""
NO_TRANSACTION_DIRECTIVE = "-- cairn:no-transaction"
"""First line of a migration file that must run outside a transaction."""

_FILE_NAME = re.compile(r"^(?P<version>\d{4})_(?P<name>[a-z][a-z0-9_]*)\.sql$")

_ENSURE_BOOKKEEPING = """
CREATE SCHEMA IF NOT EXISTS cairn_meta;
CREATE TABLE IF NOT EXISTS cairn_meta.schema_migrations (
  version    integer PRIMARY KEY CHECK (version > 0),
  name       text        NOT NULL,
  checksum   text        NOT NULL CHECK (checksum ~ '^[0-9a-f]{64}$'),
  applied_at timestamptz NOT NULL DEFAULT now(),
  applied_by text        NOT NULL DEFAULT current_user
);
"""


# --- errors ---------------------------------------------------------------------------


class MigrationError(Exception):
    """Base class. Every subclass message names the version and never a credential."""


class InvalidMigrationFile(MigrationError):
    """A bundled ``.sql`` file has a bad name, is not UTF-8, or is empty."""

    def __init__(self, filename: str, reason: str) -> None:
        super().__init__(f"invalid migration file {filename}: {reason}")
        self.filename = filename


class ChecksumMismatch(MigrationError):
    """An applied migration's recorded checksum differs from the bundled file (it was edited)."""

    def __init__(self, version: int) -> None:
        super().__init__(
            f"checksum mismatch for migration version {version}: the bundled file differs "
            "from the one that was applied; revert the edit and add a new migration instead"
        )
        self.version = version


class UnknownAppliedMigration(MigrationError):
    """The database records a version that no bundled file provides (database is ahead)."""

    def __init__(self, version: int) -> None:
        super().__init__(
            f"the database records migration version {version}, which no bundled file provides"
        )
        self.version = version


class MigrationGap(MigrationError):
    """Versions are not unique and contiguous from 1."""

    def __init__(self, versions: Sequence[int], *, source: str = "bundled") -> None:
        super().__init__(
            f"{source} migration versions must be unique and contiguous from 1, "
            f"found {list(versions)}"
        )
        self.versions = tuple(versions)
        self.source = source


class MigrationFailed(MigrationError):
    """A migration file raised a database error. Its changes were rolled back."""

    def __init__(self, version: int, name: str, reason: str) -> None:
        super().__init__(f"migration {version:04d}_{name} failed: {reason}")
        self.version = version
        self.migration_name = name
        self.reason = reason


# --- models ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Migration:
    """One bundled migration file."""

    version: int  # from NNNN in NNNN_name.sql, starts at 1, no gaps
    name: str  # lowercase snake case
    sql: str
    checksum: str  # sha256 hex of the file bytes
    transactional: bool  # False only if the first line is the no-transaction directive

    @property
    def filename(self) -> str:
        return f"{self.version:04d}_{self.name}.sql"


@dataclass(frozen=True)
class AppliedMigration:
    """One row of ``cairn_meta.schema_migrations``."""

    version: int
    name: str
    checksum: str
    applied_at: datetime


@dataclass(frozen=True)
class MigrationStatus:
    """Applied and pending migrations, as ``cairn-db status`` prints them."""

    applied: tuple[AppliedMigration, ...]
    pending: tuple[Migration, ...]
    head: int  # highest bundled version, 0 if there are none
    current: int  # highest applied version, 0 if there are none


# --- discovery (reads package data) -------------------------------------------------------


def parse_migration(filename: str, data: bytes) -> Migration:
    """Build a ``Migration`` from a file name and its bytes. Pure."""
    match = _FILE_NAME.match(filename)
    if match is None:
        raise InvalidMigrationFile(filename, "name must match NNNN_snake_case.sql")
    version = int(match["version"])
    if version < 1:
        raise InvalidMigrationFile(filename, "versions start at 0001")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise InvalidMigrationFile(filename, "file is not valid UTF-8") from exc
    if not text.strip():
        raise InvalidMigrationFile(filename, "file is empty")
    first_line = text.split("\n", 1)[0].strip()
    return Migration(
        version=version,
        name=match["name"],
        sql=text,
        checksum=hashlib.sha256(data).hexdigest(),
        transactional=first_line != NO_TRANSACTION_DIRECTIVE,
    )


def discover(directory: Traversable | None = None) -> list[Migration]:
    """Read every ``*.sql`` file in ``directory`` (default: the bundled package data).

    The result is sorted by version. Files that do not end in ``.sql`` are ignored;
    a ``.sql`` file with a bad name raises ``InvalidMigrationFile``. Gaps and
    duplicate versions are not checked here: ``plan`` reports them.
    """
    if directory is None:
        directory = files("cairn_core") / "migrations" / "sql"
    found = [
        parse_migration(entry.name, entry.read_bytes())
        for entry in directory.iterdir()
        if entry.is_file() and entry.name.endswith(".sql")
    ]
    return sorted(found, key=lambda m: (m.version, m.name))


def bundled_migrations() -> list[Migration]:
    """The migrations shipped with ``cairn_core``."""
    return discover()


def head(migrations: Sequence[Migration]) -> int:
    """The highest migration version, or 0 if there are none (the Migration_Head)."""
    return max((m.version for m in migrations), default=0)


# --- planning (pure) ---------------------------------------------------------------------


def plan(available: Sequence[Migration], applied: Sequence[AppliedMigration]) -> list[Migration]:
    """Return the migrations still to apply, in ascending order, or raise.

    Raises, without running anything:
    - ``MigrationGap``: bundled versions are not unique and contiguous from 1, or the
      recorded versions are not contiguous from 1;
    - ``UnknownAppliedMigration``: a recorded version has no bundled file;
    - ``ChecksumMismatch``: a recorded checksum differs from the bundled file's.
    """
    versions = sorted(m.version for m in available)
    if versions != list(range(1, len(versions) + 1)):
        raise MigrationGap(versions)
    by_version = {m.version: m for m in available}
    for record in sorted(applied, key=lambda a: a.version):
        bundled = by_version.get(record.version)
        if bundled is None:
            raise UnknownAppliedMigration(record.version)
        if bundled.checksum != record.checksum:
            raise ChecksumMismatch(record.version)
    done = {a.version for a in applied}
    if done != set(range(1, len(done) + 1)):
        raise MigrationGap(sorted(done), source="recorded")
    return [by_version[v] for v in versions if v not in done]


def status(available: Sequence[Migration], applied: Sequence[AppliedMigration]) -> MigrationStatus:
    """Applied and pending migrations. Raises what ``plan`` raises. Pure."""
    pending = plan(available, applied)
    return MigrationStatus(
        applied=tuple(sorted(applied, key=lambda a: a.version)),
        pending=tuple(pending),
        head=head(available),
        current=max((a.version for a in applied), default=0),
    )


# --- database ------------------------------------------------------------------------------


def _reason(exc: psycopg.Error) -> str:
    """Error class, SQLSTATE and the server's primary message (no connection details)."""
    message = exc.diag.message_primary
    if not message:
        lines = str(exc).strip().splitlines()
        message = lines[0] if lines else ""
    state = f" ({exc.sqlstate})" if exc.sqlstate else ""
    return f"{type(exc).__name__}{state}: {message}" if message else f"{type(exc).__name__}{state}"


@contextmanager
def advisory_lock(conn: psycopg.Connection[Any]) -> Iterator[None]:
    """Hold the session advisory lock ``cairn_migrate``. A second holder waits."""
    conn.execute("SELECT pg_advisory_lock(hashtext(%s))", (LOCK_NAME,))
    try:
        yield
    finally:
        try:
            conn.execute("SELECT pg_advisory_unlock(hashtext(%s))", (LOCK_NAME,))
        except psycopg.Error:
            # A broken connection drops its session locks when it closes.
            log.warning("could not release the migration advisory lock explicitly")


def _ensure_bookkeeping(conn: psycopg.Connection[Any]) -> None:
    with conn.transaction():
        conn.execute(_ENSURE_BOOKKEEPING)


def read_applied(conn: psycopg.Connection[Any]) -> list[AppliedMigration]:
    """Rows of ``cairn_meta.schema_migrations`` by version; empty if the table is missing."""
    exists = conn.execute("SELECT to_regclass(%s) IS NOT NULL", (BOOKKEEPING_TABLE,)).fetchone()
    if exists is None or not exists[0]:
        return []
    rows = conn.execute(
        "SELECT version, name, checksum, applied_at FROM cairn_meta.schema_migrations "
        "ORDER BY version"
    ).fetchall()
    return [AppliedMigration(version=r[0], name=r[1], checksum=r[2], applied_at=r[3]) for r in rows]


def _record(conn: psycopg.Connection[Any], m: Migration) -> None:
    conn.execute(
        "INSERT INTO cairn_meta.schema_migrations (version, name, checksum) VALUES (%s, %s, %s)",
        (m.version, m.name, m.checksum),
    )


# Migration text comes from files shipped in the package, never from user input, so
# it is passed to psycopg as a plain query string. With no parameters psycopg does
# no placeholder processing, so a literal "%" in a file is safe.
def _apply(conn: psycopg.Connection[Any], m: Migration) -> None:
    try:
        if m.transactional:
            with conn.transaction():
                conn.execute(m.sql)
                _record(conn, m)
        else:
            conn.execute(m.sql)
            _record(conn, m)
    except psycopg.Error as exc:
        raise MigrationFailed(m.version, m.name, _reason(exc)) from exc


def migrate(conn: psycopg.Connection[Any], migrations: Sequence[Migration]) -> list[int]:
    """Apply every pending migration and return the versions applied, ascending.

    ``conn`` must be in autocommit mode and connected as the owner role. A second
    call returns ``[]``. Raises ``MigrationError`` subclasses; on any error before
    a file runs, nothing has been applied.
    """
    if not conn.autocommit:
        raise ValueError("migrate() needs a connection in autocommit mode")
    with advisory_lock(conn):
        _ensure_bookkeeping(conn)
        pending = plan(migrations, read_applied(conn))  # raises before anything runs
        done: list[int] = []
        for m in pending:
            # Invariant: recorded versions plus `done` are the prefix 1..k of all
            # versions, and every recorded checksum equals its file's checksum.
            _apply(conn, m)
            done.append(m.version)
        return done
