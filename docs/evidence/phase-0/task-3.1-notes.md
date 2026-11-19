# Task 3.1 notes: migration runner, baseline migration, `cairn-db migrate` and `status`

Date: 27 Oct 2026. Dependencies and uv.lock unchanged (`uv lock --check` passes: 118 packages). The only `pyproject.toml` change is the `[project.scripts]` entry in `packages/core`.

## Files

| File | State |
|---|---|
| `packages/core/src/cairn_core/migrate.py` | New: runner library (pure discovery and planning, plus the database half) |
| `packages/core/src/cairn_core/migrations/sql/0001_baseline.sql` | New: baseline migration, shipped as package data |
| `packages/core/src/cairn_core/cli.py` | New: `cairn-db` click group with `migrate` and `status`, and `connect_as` |
| `packages/core/pyproject.toml` | `[project.scripts] cairn-db = "cairn_core.cli:main"` |
| `packages/core/tests/unit/test_migrate.py` | New: 41 tests |

## Public API of `cairn_core.migrate`

| Name | What it does |
|---|---|
| `Migration(version, name, sql, checksum, transactional)` | Frozen dataclass; `.filename` gives `NNNN_name.sql` |
| `AppliedMigration(version, name, checksum, applied_at)` | One bookkeeping row |
| `MigrationStatus(applied, pending, head, current)` | Result of `status` |
| `MigrationError` and subclasses `InvalidMigrationFile`, `ChecksumMismatch`, `UnknownAppliedMigration`, `MigrationGap`, `MigrationFailed` | Messages name the version, never a credential. `ChecksumMismatch` and `UnknownAppliedMigration` have `.version`; `MigrationGap` has `.versions` and `.source` (`bundled` or `recorded`); `MigrationFailed` has `.version`, `.migration_name`, `.reason` |
| `parse_migration(filename, data: bytes)` | Pure. Name pattern `^\d{4}_[a-z][a-z0-9_]*\.sql$`, version at least 1, UTF-8, not empty; sha256 of the file bytes; `transactional` is false only if the first line is `-- cairn:no-transaction` |
| `discover(directory=None)` | Reads `*.sql` from a `Traversable` (default: `cairn_core/migrations/sql` through `importlib.resources`), sorted by version. Other files are ignored; a badly named `.sql` file raises |
| `bundled_migrations()` | `discover()` with the default directory |
| `head(migrations)` | Highest version, 0 if none |
| `plan(available, applied)` | Pure. Pending suffix in ascending order, or raises `MigrationGap` (bundled or recorded), `UnknownAppliedMigration`, `ChecksumMismatch`. Task 3.3's property fits it directly |
| `status(available, applied)` | Pure. Wraps `plan` and adds counts |
| `read_applied(conn)` | Rows of `cairn_meta.schema_migrations`; `[]` if the table does not exist (checked with `to_regclass`) |
| `advisory_lock(conn)` | Context manager for the session lock `pg_advisory_lock(hashtext('cairn_migrate'))` |
| `migrate(conn, migrations)` | Applies pending files, returns the applied versions. `conn` must be autocommit (`ValueError` otherwise) |

Run order in `migrate`: take the lock, create `cairn_meta` and `schema_migrations` if missing (own transaction), read applied rows, `plan` (raises before any file runs), then for each pending file one transaction containing the file and its `INSERT` into the bookkeeping table. The lock is released in a `finally`; if the unlock itself fails (broken connection) it only logs a warning, since closing the session releases the lock.

## Behavior worth knowing

- Failure: a `psycopg.Error` inside a file becomes `MigrationFailed`, for example `migration 0001_baseline failed: RaiseException (P0001): missing extensions: pg_search, vector (run cairn-db bootstrap)`. The text is the error class, SQLSTATE and the server's primary message; no connection details. The baseline sorts the missing names, so the message names every missing extension (Requirement 6.9).
- Bookkeeping DDL is in the runner, not in `0001`. The baseline grants on `cairn_meta`, so the runner creates the schema and table first, as the connecting role (`cairn_owner`). Schema `cairn_meta` is therefore owned by the owner role.
- `-- cairn:no-transaction` files: executed on the autocommit connection with no `BEGIN`, then the row is inserted. Such a file must hold one statement (Postgres rejects `CREATE INDEX CONCURRENTLY` in a multi-statement batch) and must be safe to run again, because a crash between the file and its row leaves it pending. No bundled file uses this yet.
- Migration SQL is passed to `conn.execute` with no parameters, so psycopg does no placeholder processing and a literal `%` in a file (the baseline's `RAISE EXCEPTION '…%…'`) is fine.
- The checksum is over the raw bytes. A line-ending change counts as an edit. Keep migrations LF (an `.gitattributes` with `*.sql text eol=lf` would enforce it; I did not add root files).
- Wheel: hatch's `packages = ["src/cairn_core"]` already ships non-Python files. Verified with `uv build --wheel`: the wheel holds `cairn_core/migrations/sql/0001_baseline.sql` and `entry_points.txt` with `cairn-db = cairn_core.cli:main`.

## `cairn-db`

| Command | Output (stable lines) | Exit |
|---|---|---|
| `cairn-db migrate` | `applied 0001_baseline.sql` per file, then `applied N migration(s); now at version H`, or `up to date at version H; nothing applied` | 0 |
| `cairn-db status` | `head: H  current: C  pending: P`, then `applied  0001  baseline  <12 hex>  <timestamp>` and `pending  0002  name  <12 hex>` lines | 0 |
| either, on `MigrationError` | stderr `migration failed: …` or `migration status: …` | 1 |
| either, on any `psycopg.Error` | stderr `database error: <ErrorClass> (<SQLSTATE>)`, nothing else | 1 |
| either, bad configuration | stderr `invalid configuration: KEY (code), …` from `load_settings` | 2 |

Both commands load `AdminSettings` (so they need the three role passwords; `POSTGRES_PASSWORD` is optional), call `configure_logging("cairn-db", level)`, and connect as `cairn_owner` with `CAIRN_OWNER_DB_PASSWORD` to `CAIRN_DB_NAME`. `connect_as(settings, *, user, password, dbname=None)` builds the connection from keyword arguments, so no connection string with a password exists anywhere. `status` is read-only and takes no lock. With the repo's `.env` and no database running, `cairn-db migrate` prints `database error: OperationalError` and exits 1.

## Verification

```text
$ uv run pytest packages/core/tests/unit/test_migrate.py
41 passed in 0.14s
$ make types
mypy --strict packages/core: Success: no issues found in 9 source files
mypy base:                   Success: no issues found in 30 source files
$ make fmt-check lint
39 files already formatted / All checks passed!
$ make test-unit
281 passed in 0.76s
$ uv sync --locked --all-packages && uv run cairn-db --help
Commands: migrate, status
$ uv lock --check
Resolved 118 packages
```

## Tests

- Discovery: checksum is sha256 of the bytes and changes with any byte; eight bad names; empty and non-UTF-8 files; the directive counts only on the first line (CRLF and trailing spaces allowed); sorted discovery ignoring README and subdirectories; the bundled baseline exists, is transactional, contains the extension check, the schema, and the three grants, and plans cleanly on an empty database.
- Planning: empty database, pending suffix, nothing pending, inputs unchanged, checksum mismatch names the version, database ahead, bundled gaps (missing, not starting at 1, duplicates), recorded gap, `status` counts.
- `migrate()` against a recording connection (not a database): order of events under the lock; second run applies nothing; a failing file rolls back, records nothing, stops later files and still unlocks; checksum mismatch, database ahead and bundled gap run no file; no-transaction files run outside `BEGIN`/`COMMIT`; non-autocommit connection rejected; `read_applied` with a missing table.
- CLI with the connection replaced: `migrate` uses the owner role and the owner password and reports up to date on the second run; `status` for applied and fresh databases; exit 1 and the version named on drift; exit 1 on `OperationalError` with the password and host from the exception text absent from the output; exit 2 on a missing password; `--help` lists the commands.

What is not covered here and lands in task 4.2 with a real database: the SQL text itself running on PG18, the bookkeeping DDL, the advisory lock waiting for a concurrent run (Requirement 6.7), rollback of real DDL (6.6), the baseline's grants and ownership (6.8), and the missing-extension message from a real `RAISE` (6.9).

## Deviations from the design

- One module `cairn_core/migrate.py` instead of a `migrate/` package with `model.py`; the task text names `migrate.py`. The import path `cairn_core.migrate` is the same.
- Added beyond design §6: `InvalidMigrationFile`, `MigrationFailed`, `MigrationStatus`, `status`, `parse_migration`, `bundled_migrations`, `read_applied`, `advisory_lock`, and the autocommit check in `migrate`. `discover()` takes an optional directory.
- Baseline: the missing-extension list is sorted and rendered with `array_to_string` (`pg_search, vector`) instead of the raw array (`{pg_search,vector}`), and it carries a header comment. Statements are otherwise as in design §6.
- `cairn-db migrate` prints plain lines (the stable ones above) instead of JSON logs; `configure_logging` is still set up for warnings.
- Mypy treats `LiteralString` as `str`, so no cast is needed when passing migration text to `conn.execute`.

## Notes for later tasks

- 3.2 (bootstrap, `init`): add commands to the `cairn_core.cli.main` group; reuse `connect_as(settings, user="postgres", password=superuser_password, dbname=…)`. `OWNER_ROLE = "cairn_owner"` is in `cli.py`. `connect_as` returns an autocommit connection, which bootstrap needs for `CREATE DATABASE`. `init` should call `migrate(conn, bundled_migrations())` after bootstrap on a `cairn_owner` connection and may reuse the output lines of `migrate_command`. Bootstrap must create the roles, `cairn` and the three extensions before the baseline runs: the baseline refers to `cairn_owner`, `cairn_app` and the extensions, and `CREATE SCHEMA` needs CREATE on the database, which `cairn_owner` has as its owner. `bootstrap` must also work on test databases `cairn_test_<hex>` owned by `cairn_owner`.
- 3.2: `REVOKE ALL ON SCHEMA cairn_meta`/grants are not needed in bootstrap; the baseline grants `cairn_app` USAGE on `cairn` and `cairn_meta` and SELECT on `schema_migrations`. Bootstrap still owns the `paradedb` schema USAGE grant (design §5 step 4).
- 4.1 (compose): `db-init` runs `cairn-db init` (bootstrap, then migrate) with `POSTGRES_PASSWORD` and the three role passwords; the image needs the `cairn-db` console script on PATH (`uv sync --locked --no-dev`-style install of `cairn-core` puts it in the venv's `bin`). `cairn-db migrate` alone needs only the owner and app/dbos passwords because `AdminSettings` requires all three role passwords.
- 4.2 (fixtures and tests): `migrate(conn, bundled_migrations())` needs an autocommit connection as `cairn_owner` to the fresh `cairn_test_<hex>` database. Suggested integration tests: apply twice and expect `[]` the second time (also `migrations-check`: `cairn-db migrate` twice, the second prints `nothing applied`); edit a checksum row and expect `ChecksumMismatch` with no change; insert a version 99 row and expect `UnknownAppliedMigration`; a failing file via a temporary directory of migrations (`discover(tmp_path)`) leaves no bookkeeping row; two threads calling `migrate` on separate connections (the second waits, then returns `[]`); drop an extension in a scratch database and expect `MigrationFailed` naming it; after the baseline, `cairn.` is owned by `cairn_owner`, `cairn_app` has USAGE on `cairn` and `cairn_meta` and SELECT on `schema_migrations`.
- 5.1 (api readiness): the expected head is `head(bundled_migrations())`; the recorded version is `max(version)` from `cairn_meta.schema_migrations`, readable by `cairn_app` thanks to the baseline grants.
- Later migrations: new files are `0002_<name>.sql` in `packages/core/src/cairn_core/migrations/sql/`; never edit an applied file. Every table in schema `cairn` needs `ENABLE` and `FORCE ROW LEVEL SECURITY` and explicit grants (no default privileges).
