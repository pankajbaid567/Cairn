# Task 3.2 notes: idempotent bootstrap, `cairn-db bootstrap` and `init`

Date: 27 Oct 2026. Dependencies and uv.lock unchanged. V4 resolved in [verify/V4.md](verify/V4.md).

## Files

| File | State |
|---|---|
| `packages/core/src/cairn_core/bootstrap.py` | New: pure statement builders, SCRAM verifiers, database half |
| `packages/core/src/cairn_core/cli.py` | `bootstrap` and `init` added; `migrate` body moved to `_migrate` (same output); `_bootstrap`, `_connect_superuser` |
| `packages/core/tests/unit/test_bootstrap.py` | New: 50 tests |
| `docs/evidence/phase-0/verify/V4.md` | New |

## Public API of `cairn_core.bootstrap`

| Name | What it does |
|---|---|
| `OWNER_ROLE`, `APP_ROLE`, `DBOS_ROLE`, `MANAGED_ROLES`, `DBOS_DATABASE`, `REQUIRED_EXTENSIONS`, `PARADEDB_SCHEMA`, `ROLE_ATTRIBUTES` | Constants. `cli.OWNER_ROLE` is kept as is |
| `BootstrapError` | Refusal or failure. Messages name roles and databases, never passwords |
| `scram_sha256_verifier(password, *, salt=None, iterations=4096)` | Pure-Python verifier (ASCII only); `is_scram_verifier(value)` |
| `ScramEncryptor(conn)` | Callable `(password, role) -> verifier`. libpq first (`.method == "libpq"`), Python fallback (`"python"`) |
| `role_statement(name, verifier, *, exists)` | `CREATE`/`ALTER ROLE … WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS [NO]INHERIT PASSWORD '<verifier>'`. Raises if the value is not a SCRAM verifier |
| `role_statements(passwords, existing, encrypt)` | Pure. One statement per managed role in fixed order (owner, app, dbos). `passwords` maps role to plaintext, `encrypt` is any `(password, role) -> verifier` |
| `membership_violations(rows)` | Pure. `["cairn_app is a member of pg_monitor", …]` for `(member, granted)` pairs |
| `DatabaseSpec`, `content_spec(name="cairn", …)`, `dbos_spec()` | What a database needs: owner, CONNECT roles, extensions |
| `database_statements(spec, *, current_owner)` | Pure. `CREATE DATABASE` or `ALTER … OWNER TO`, `REVOKE CONNECT, TEMPORARY … FROM PUBLIC`, `REVOKE CONNECT` from managed roles that may not connect, `GRANT CONNECT` |
| `extension_statements(spec)`, `schema_grant_statements(spec, *, paradedb_schema_exists)` | Pure. In-database part: `CREATE EXTENSION IF NOT EXISTS`, `REVOKE CREATE ON SCHEMA public FROM PUBLIC`, `GRANT USAGE ON SCHEMA paradedb` |
| `check_memberships(admin_conn)` | Raises `BootstrapError` naming every membership of `cairn_app` or `cairn_dbos` |
| `bootstrap_database(admin_conn, name, *, spec=None, connect=None) -> DatabaseResult` | One database, default `content_spec(name)`: owner `cairn_owner`, CONNECT for owner and app, the three extensions. For test and spike databases. Roles must exist |
| `bootstrap(admin_conn, settings, *, connect=None, encrypt=None) -> BootstrapResult` | Everything; content database is `settings.db_name`, plus `cairn_dbos` |
| `connect_like(admin_conn, dbname)` | Default `connect`: autocommit connection with the admin connection's credentials |
| `describe(result)` | Stable output lines |
| `RoleResult`, `DatabaseResult`, `BootstrapResult` | Frozen result dataclasses (no secrets) |

Run order in `bootstrap`: autocommit check, membership check (read-only, so a refusal changes nothing), existing-role lookup, `CREATE`/`ALTER ROLE` for the three roles, then `bootstrap_database` for `cairn` and for `cairn_dbos`. `bootstrap_database` reads the owner from `pg_database`, runs the database statements on the admin connection, then opens a connection to the database itself for extensions and the paradedb grant (the schema exists only after `pg_search` is created, so it is checked after the extension statements).

## `cairn-db`

| Command | Output (stable lines) | Exit |
|---|---|---|
| `cairn-db bootstrap` | `role cairn_owner: created\|updated` ×3, `database cairn: created\|exists\|owner changed`, `extensions cairn: btree_gist, pg_search, vector`, `database cairn_dbos: …`, `bootstrap complete (password encryption: libpq\|python)` | 0 |
| `cairn-db init` | the `bootstrap` lines, then the `migrate` lines | 0 |
| `BootstrapError` | stderr `bootstrap failed: …` (`init` stops before migrating) | 1 |
| `psycopg.Error` | stderr `database error: <Class> (<SQLSTATE>)` | 1 |
| `POSTGRES_PASSWORD` unset | stderr `invalid configuration: POSTGRES_PASSWORD (missing)` | 2 |
| other bad configuration | from `load_settings` | 2 |

`bootstrap` connects as `postgres` to database `postgres` with `POSTGRES_PASSWORD`; `migrate` and the migrate half of `init` connect as `cairn_owner` to `CAIRN_DB_NAME`. The superuser password check happens before any connection attempt.

## Behavior worth knowing

- Idempotence: roles are `ALTER`ed on every run, which re-asserts attributes. The password verifier is re-sent each time with a new random salt, so `rolpassword` changes; attributes, memberships, owners, privileges and extensions do not (Requirement 5.5 lists none of the password).
- Passwords never reach statements, logs, results or errors as plaintext: the builder rejects any value that is not a SCRAM verifier, logs carry only role and database names (`bootstrap.role`, `bootstrap.database`; fields `role`, `database`, `was_created`, `owner_changed`), and the CLI prints names only. A test runs the whole flow on both password paths and searches statements, `repr(result)`, output and captured log records for all four passwords.
- Verifiers do appear in the `ALTER ROLE` statement text. A verifier is not a password (it cannot be replayed to log in, but it allows offline guessing) and would show in server logs under `log_statement = all|ddl`. The ParadeDB image default is `none`. Keep it that way for the db service (task 4.1).
- Log extra named `created` collides with a `LogRecord` attribute (`KeyError`), hence `was_created`.
- Extra REVOKE: besides `REVOKE … FROM PUBLIC`, bootstrap revokes CONNECT on each database from the managed roles that should not have it (for example `cairn_dbos` on `cairn`). This makes "CONNECT to the intended roles only" hold even if someone granted more earlier.
- `REVOKE CREATE ON SCHEMA public FROM PUBLIC` runs in every bootstrapped database, including `cairn_dbos` (a no-op on PG15+ defaults, harmless).
- A database that exists with another owner is re-owned with `ALTER DATABASE … OWNER TO` (reported as `owner changed`). Objects inside it keep their owners.

## Verification

```text
$ uv run pytest packages/core/tests/unit/test_bootstrap.py
50 passed in 0.23s
$ make types
mypy --strict packages/core: Success: no issues found in 15 source files
mypy base:                   Success: no issues found in 40 source files
$ make fmt-check lint
55 files already formatted / All checks passed!
$ make test-unit
561 passed, 1 xfailed (the xfail is a known gap in packages/eval, not mine)
```

## Tests

- Verifiers: format, fresh salt, non-ASCII refused without echo, `is_scram_verifier` rejects plaintext and md5, and Python output equals libpq output when given libpq's salt and iterations (offline, using a failed `PGconn`).
- Encryptor: libpq called with `(password, role, b"scram-sha-256")`; falls back when `pgconn` is missing, `None`, or raises.
- Role statements: exact `CREATE`/`ALTER` text, only `cairn_app` NOINHERIT, no dangerous attribute without `NO`, plaintext verifier refused without echo, identifier injection refused, fixed order, missing and empty passwords.
- Database statements: exact statement lists for the content database and `cairn_dbos`, existing database not recreated, wrong owner fixed, name validation, extension and schema-grant statements.
- `bootstrap()` against a fake cluster: step order on an empty cluster, second run changes nothing and only `ALTER`s roles, wrong-owner database fixed, membership refusal (both roles) with zero writes, non-autocommit rejected, Python fallback path, no plaintext in statements/logs/results/output on both paths, a failing statement's error carries no password, `bootstrap_database` for a test database (twice) and with a custom spec.
- CLI with `connect_as`, `run_bootstrap` and `migrate` replaced: users and databases connected to, order `postgres → bootstrap → cairn_owner → migrate`, `init` stops on failure, exit 2 naming only `POSTGRES_PASSWORD`, exit 1 on a connection error without host or password.

Not covered here (task 4.2, real database): roles and attributes in `pg_roles`, the second run leaving the catalog unchanged, `CREATE DATABASE` and privilege behavior, `pg_auth_members` query shape, paradedb schema grant on the real image, and `info.password`-based `connect_like`.

## Deviations from the design

- Fallback for missing libpq encryption is a pure-Python SCRAM verifier, not a plaintext `Literal` (see V4). The libpq call uses an explicit `scram-sha-256` algorithm.
- `paradedb` USAGE is granted to `cairn_owner` as well as `cairn_app` (the roles with CONNECT on the content database), since the owner will create BM25 indexes in Phase 1. Design §5 names the app role only.
- `REQUIRED_EXTENSIONS` lives in `cairn_core.bootstrap`; the design puts it in `health.py` (task 5.1). `health.py` should import it from here.
- `bootstrap_database` takes an optional `spec` and `connect`, so one function serves the content, DBOS, test and spike databases.
- The DBOS database name is the constant `DBOS_DATABASE` (`AdminSettings` has no field for it; `WorkerSettings.dbos_db_name` defaults to the same value).

## Notes for later tasks

- 3.4 (property test "bootstrap never exposes plaintext passwords"): drive `role_statements(passwords, existing, encrypt)` with generated passwords and either `fake_encrypt`-style callables or `ScramEncryptor(None)` (pure Python, ASCII only; restrict the strategy to printable ASCII, or expect `BootstrapError` for non-ASCII). Render with `statement.as_string()` (works without a connection). Assert no password substring in any statement, that each contains `PASSWORD 'SCRAM-SHA-256$`, and that `BootstrapError` messages do not contain the password. Very short passwords (one character) can appear inside fixed text such as `ALTER`; use a minimum length (8+) or check for the password as a whole token with random characters.
- 4.1 (compose): `db-init` command is `cairn-db init`. It needs `POSTGRES_PASSWORD` and the three role passwords, and reaches the database through `CAIRN_DB_HOST` and `CAIRN_DB_PORT`. Keep `log_statement` at its default (`none`) on the db service. The superuser connection goes to database `postgres`.
- 4.2 (fixtures and integration): `admin_conn` must be an autocommit `postgres` connection. Call `bootstrap(admin_conn, settings)` once per session for roles and the two fixed databases, then `bootstrap_database(admin_conn, f"cairn_test_{hex}")` per test database, then `migrate` as `cairn_owner`. Drop with `DROP DATABASE … WITH (FORCE)`. Integration cases to add: second `bootstrap` leaves `pg_roles` (minus `rolpassword`), `pg_auth_members`, `pg_database` owners and ACLs, and `pg_extension` unchanged; `cairn_app` or `cairn_dbos` granted a role makes `bootstrap` raise `BootstrapError` naming it and change nothing; `has_database_privilege('cairn_dbos','cairn','CONNECT')` is false and `PUBLIC` has no CONNECT or TEMPORARY; the three extensions exist in `cairn`; `has_schema_privilege('cairn_app','paradedb','USAGE')`; passwords work by logging in as each role (SCRAM); `cairn_app` and `cairn_dbos` have `rolsuper`, `rolbypassrls` and the other flags false and `rolinherit` false for `cairn_app`. `connect_like` reads `admin_conn.info.password`, so it only works when the connection was opened with a password; if the fixture uses `connect_like`, check this first.
- 5.1 (readiness): import `REQUIRED_EXTENSIONS` from `cairn_core.bootstrap`.
