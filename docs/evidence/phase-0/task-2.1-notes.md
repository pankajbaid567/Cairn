# Task 2.1 notes: `cairn_core.config`

Date: 27 Oct 2026. Dependencies and uv.lock unchanged (`uv lock --check` passes: 118 packages).

## Files

| File | State |
|---|---|
| `packages/core/src/cairn_core/config.py` | New: `CommonSettings`, `ApiSettings`, `WorkerSettings`, `AdminSettings`, `load_settings`, `invalid_keys`, `CONFIG_EXIT_STATUS` |
| `packages/core/tests/unit/test_config.py` | New: 24 tests |

## Settings and environment variables

| Class | Field | Variable | Default | Constraint |
|---|---|---|---|---|
| Common | `env` | `CAIRN_ENV` | `local` | `local`, `ci`, `demo` |
| Common | `log_level` | `CAIRN_LOG_LEVEL` | `INFO` | DEBUG, INFO, WARNING, ERROR |
| Common | `db_host` | `CAIRN_DB_HOST` | `127.0.0.1` | non-empty |
| Common | `db_port` | `CAIRN_DB_PORT` | `5432` | 1 to 65535 |
| Common | `db_name` | `CAIRN_DB_NAME` | `cairn` | non-empty |
| Common | `db_timeout_s` | `CAIRN_DB_TIMEOUT_S` | `2.0` | above 0, at most 30 |
| Common | `otel_traces_exporter` | `OTEL_TRACES_EXPORTER` | `none` | `none`, `otlp`, `console` |
| Common | `otel_endpoint` | `OTEL_EXPORTER_OTLP_ENDPOINT` | none | http or https URL |
| Api | `app_db_user` | `CAIRN_APP_DB_USER` | `cairn_app` | non-empty |
| Api | `app_db_password` | `CAIRN_APP_DB_PASSWORD` | required | `SecretStr`, non-empty |
| Worker | `dbos_db_name` | `CAIRN_DBOS_DB_NAME` | `cairn_dbos` | non-empty |
| Worker | `dbos_db_password` | `CAIRN_DBOS_DB_PASSWORD` | required | `SecretStr`, non-empty |
| Worker | `heartbeat_cron` | `CAIRN_HEARTBEAT_CRON` | `*/30 * * * * *` | 5 or 6 fields |
| Admin | `superuser_password` | `POSTGRES_PASSWORD` | none (optional) | `SecretStr` |
| Admin | `owner_db_password` | `CAIRN_OWNER_DB_PASSWORD` | required | `SecretStr`, non-empty |
| Admin | `app_db_password` | `CAIRN_APP_DB_PASSWORD` | required | `SecretStr`, non-empty |
| Admin | `dbos_db_password` | `CAIRN_DBOS_DB_PASSWORD` | required | `SecretStr`, non-empty |

A unit test renders `.env.example` through `scripts.dev_env.render` into a temporary `.env` and loads all three classes from it. It also asserts that every alias is a key in `.env.example`. The repo's own `.env` is never read.

## Behavior

- Sources: process environment first, then `.env` in the working directory. `extra="ignore"`, so `.env` keys that belong to other processes (for example `POSTGRES_PASSWORD` for the API) are ignored. Field names such as `db_port` are not environment keys; only the aliases are.
- `env_ignore_empty=True`: `FOO=` counts as unset. An empty required password is therefore reported as missing instead of becoming an empty credential.
- `frozen=True`, secrets are `SecretStr`: `repr`, `str` and `model_dump_json` show `**********`.
- `load_settings(cls)` on failure prints one line to stderr, `invalid configuration: CAIRN_DB_PORT (int_parsing), CAIRN_APP_DB_PASSWORD (missing)`, and raises `SystemExit(2)`. Every bad key is listed at once, sorted. Only the variable name and the pydantic error code are printed; the input value and pydantic's message are dropped, because some messages quote the input. An unreadable settings source (`SettingsError`) prints the class name only.
- `invalid_keys(error, cls)` is public so a later test or the CLI can reuse it.

## Verification

```text
$ uv run pytest packages/core/tests/unit/test_config.py
24 passed in 0.21s
$ make types
mypy --strict packages/core: Success: no issues found in 5 source files
mypy base:                   Success: no issues found in 17 source files
$ uv run ruff check / ruff format --check (my two files)
All checks passed! / 2 files left unchanged
$ make fmt-check
23 files already formatted
$ make test-unit
110 passed in 0.60s
$ uv lock --check
Resolved 118 packages
```

`make lint` currently fails with 3 findings, none in my files, all in task 2.2's work in progress: `packages/core/src/cairn_core/telemetry.py:38`, `packages/core/tests/unit/test_telemetry.py:159` and `:264` (PT018, the last one an unsafe fix). Not touched.

## Deviations from the design

- `load_settings` uses PEP 695 syntax, `def load_settings[S: CommonSettings](settings_cls: type[S]) -> S`, instead of a module-level `TypeVar`. Same typing; ruff's UP047 asks for it on Python 3.12. The parameter is named `settings_cls`.
- Additions beyond design §7, all small: `min_length=1` on names and secrets; `env_ignore_empty=True`; a `heartbeat_cron` validator that requires 5 or 6 fields (a bad cron would otherwise surface only when DBOS launches); `invalid_keys` and `CONFIG_EXIT_STATUS` as public names.
- `otel_endpoint` is not required when the exporter is `otlp`. The design's `AnyHttpUrl | None` default is kept. Task 2.2 decides what an `otlp` exporter without an endpoint does (the OpenTelemetry exporter has its own default of `http://localhost:4318`).
- `AdminSettings` has no field for the test admin connection (`CAIRN_TEST_ADMIN_HOST`, `_PORT`, `_USER`). Per task 1.3 those belong to the test fixtures in 4.2, and the password is `POSTGRES_PASSWORD`.

## Notes for later tasks

- Which task reads which settings:
  - 2.2 (telemetry): `env`, `log_level`, `otel_traces_exporter`, `otel_endpoint`, available on every class through `CommonSettings`.
  - 3.x (`cairn-db`): `AdminSettings` for `bootstrap`, `migrate`, `init`; `db_host`, `db_port`, `db_name`, `db_timeout_s`, and the three role passwords. `superuser_password` is `None` when `POSTGRES_PASSWORD` is unset, so `bootstrap` must check it itself and fail with a message naming the key, not a value.
  - 5.x (api): `ApiSettings`: `app_db_user`, `app_db_password`, `db_host`, `db_port`, `db_name`, `db_timeout_s`. The API never needs the superuser, owner or DBOS passwords.
  - 6.x (worker): `WorkerSettings`: `dbos_db_name`, `dbos_db_password`, `heartbeat_cron`, `db_host`, `db_port`. It connects to `dbos_db_name`, not `db_name`. The role name `cairn_dbos` is fixed (no setting), per PD-06.
  - 4.2 (test fixtures): construct `AdminSettings`-style values themselves or read `POSTGRES_PASSWORD` directly, and skip when the db is down.
- Services call `load_settings(ApiSettings)` once at startup, before binding a port or opening a connection. It exits with 2 on bad config (Requirement 7.11).
- Unit tests that touch settings must clear the Cairn variables and `chdir` to a temporary directory, as `test_config.py` does in its autouse fixture; the repo root has a real `.env` after `make env`, and `env_file=".env"` is relative to the working directory.
- Building a DSN: use `get_secret_value()` at the last moment, quote the password with `urllib.parse.quote`, and never log the DSN (the telemetry redaction pattern covers keys named `dsn`).
- The mypy pydantic plugin accepts `ApiSettings()` calls inside `load_settings` through the generic `type[S]`. Calling a settings class directly with no arguments in tests works for classes with no required fields (`CommonSettings()`); for the others use `load_settings`.
