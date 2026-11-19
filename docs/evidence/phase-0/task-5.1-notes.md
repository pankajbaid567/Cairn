# Task 5.1 notes: `cairn_core.health`, the FastAPI app, `cairn_api.healthcheck`

Date: 27 Oct 2026. Dependencies and uv.lock unchanged (`uv lock --check` passes, 118 packages). The only `pyproject.toml` change is the `[project.scripts]` entry `cairn-api` in `apps/api`. V12 resolved in [verify/V12.md](verify/V12.md).

## Files

| File | State |
|---|---|
| `packages/core/src/cairn_core/health.py` | New: `DbProbe`, `DbUnavailable`, `CheckResult`, `ReadyReport`, `evaluate_readiness`, `probe_database` |
| `packages/core/tests/unit/test_health.py` | New: 35 test cases |
| `apps/api/src/cairn_api/main.py` | New: `build_app`, `create_app` |
| `apps/api/src/cairn_api/healthcheck.py` | New: `check`, `main` (stdlib only) |
| `apps/api/src/cairn_api/__main__.py` | New: `python -m cairn_api` and the `cairn-api` script (runs uvicorn) |
| `apps/api/pyproject.toml` | `[project.scripts] cairn-api` |
| `apps/api/tests/unit/test_main.py`, `test_healthcheck.py`, `test_serve.py` | New: 38 test cases (the directory runs 40 with the existing `test_import.py`) |
| `docs/evidence/phase-0/verify/V12.md` | New |

## Public API of `cairn_core.health`

| Name | What it does |
|---|---|
| `DbProbe(server_version_num, extensions, role_name, role_is_superuser, role_bypasses_rls, schema_version, latency_ms)` | Frozen dataclass: what one successful probe saw. `extensions` holds only the required ones that are installed; `schema_version` is `None` when the bookkeeping table is missing or empty |
| `DbUnavailable(code)` | Frozen dataclass with one field, `code`: `db_unreachable`, `db_timeout` or `probe_failed`. No free-text field, so no exception text can reach a body |
| `ProbeResult = DbProbe \| DbUnavailable`, `ProbeFn = Callable[[], Awaitable[ProbeResult]]` | The injection point: `build_app(settings, probe=...)` |
| `CheckResult(ok, error, detail)`, `ReadyReport(status, checks, extensions)` | Pydantic models. `.to_body()` gives the JSON dict (`error` and `detail` only when set) |
| `evaluate_readiness(probe, expected_head) -> (status_code, ReadyReport)` | Pure. 200 if and only if every check passes |
| `postgres_version_text(180001) -> "18.1"` | Pure helper |
| `probe_database(settings: ApiSettings) -> ProbeResult` | The only code that touches Postgres. Never raises (except task cancellation) |
| `PROBE_SQL`, `SCHEMA_VERSION_SQL`, `EXPECTED_POSTGRES_MAJOR`, `SCHEMA_TABLE` | Constants. `REQUIRED_EXTENSIONS` is imported from `cairn_core.bootstrap` |

## Readiness JSON and error codes

```json
{"status": "ready",
 "checks": {"database": {"ok": true, "detail": {"latency_ms": 3.1}},
            "postgres": {"ok": true, "detail": {"version": "18.1"}},
            "extensions": {"ok": true},
            "role": {"ok": true, "detail": {"name": "cairn_app"}},
            "schema": {"ok": true, "detail": {"version": 1, "expected": 1}}},
 "extensions": {"btree_gist": "1.8", "pg_search": "0.26.3", "vector": "0.8.6"}}
```

Shape: keys are always `status`, `checks`, `extensions`. A failing check adds `"error": "<code>"`. When the database cannot be probed, `checks` holds only `database` (`{"ok": false, "error": "<code>"}`) and `extensions` is `{}`.

| Check | Code | Detail on failure |
|---|---|---|
| database | `db_unreachable` (refused, DNS, auth, dropped), `db_timeout` (asyncio budget, libpq connect timeout, `statement_timeout`), `probe_failed` (connected but a query failed, or the probe function crashed) | none |
| postgres | `postgres_major_mismatch` | `version` |
| extensions | `extensions_missing` | `missing`: comma-separated names |
| role | `role_unsafe` (superuser or BYPASSRLS) | `name` |
| schema | `schema_behind` (version below head, `None` counts as 0), `schema_ahead` | `version`, `expected` |

Status is 200 or 503. Responses carry `Cache-Control: no-store`. `/healthz` returns `{"status": "ok"}` and never calls the probe.

## Behavior worth knowing

- **Probe**: one `psycopg.AsyncConnection` per call, autocommit, inside `conn.transaction()`. Options `statement_timeout=<db_timeout_s in ms>` and `default_transaction_read_only=on`; `application_name=cairn-api-readyz`; keyword arguments, so no DSN with a password exists. Statement 1 returns server version, a JSON of the required extensions, the role's flags (`pg_roles` for `current_user`) and `to_regclass('cairn_meta.schema_migrations') IS NOT NULL`. Statement 2 (`max(version)`) runs only if that is true. The connection is closed on exit, so recovery after an outage needs nothing (Requirement 7.10).
- **Budget**: the whole probe (connect and both statements) sits under `asyncio.timeout(db_timeout_s)`, default 2 s. A refused connection returns at once. Against the live compose database the first probe took 0.4 to 0.9 s and later ones 0.08 to 0.2 s, so 2 s is enough locally but not by a wide margin on a cold Docker Desktop.
- **Failure mapping** (`_failure`): `TimeoutError` and `QueryCanceled` are `db_timeout`; `OperationalError` or `OSError` are `db_unreachable`, except an `OperationalError` with no SQLSTATE whose text contains "timeout" (libpq's own connect timeout), which is `db_timeout`; anything else is `probe_failed`. The text is only inspected, never stored. The log line `readyz.probe_failed` carries `error_class`, `sqlstate` and `reason` and nothing else (a test checks that host, port and password are absent).
- **App**: `build_app(settings, *, probe=None, expected_head=None, tracer_provider=None)` does everything with injected parts and touches no global state except the FastAPI instrumentation of that one app. `create_app()` is the uvicorn factory: `load_settings(ApiSettings)` (exit 2, keys only), `configure_logging("cairn-api", level)`, `configure_tracing(...)` as in the 2.2 notes, `PsycopgInstrumentor().instrument(tracer_provider=provider)`, then `build_app`. No connection is opened at startup.
- **Access log**: logger `cairn.api.access`, message `http.request`, fields `method`, `path` (no query string), `status`, `duration_ms`; `trace_id` and `span_id` come from the JSON formatter because the FastAPI instrumentation wraps the whole stack, so the span is current in the middleware. `/healthz` and `/readyz` log at DEBUG, everything else at INFO. An unhandled error logs status 500 and is re-raised. Do not pass `trace_id` as an extra: the formatter would rename it to `extra_trace_id`.
- **No docs routes**: `/docs`, `/redoc` and `/openapi.json` are disabled (404). Nothing in Phase 0 is authenticated, so the only routes are the two probes.
- **Healthcheck**: `python -m cairn_api.healthcheck [url]` does a GET with `urllib.request`, exit 0 on 200, exit 1 on anything else (HTTP error, refused, timeout, non-http scheme). The URL defaults to `http://127.0.0.1:8000/readyz` and `CAIRN_API_HEALTHCHECK_URL` overrides it. Timeout 5 s.
- **Not authenticated**: `/healthz` and `/readyz` have no access control (design §Security). `/readyz` reveals extension versions and the role name.

## Verification

```text
$ uv run pytest apps/api/tests/unit packages/core/tests/unit/test_health.py
75 passed in 4.18s
$ make types
mypy --strict packages/core: Success: no issues found in 18 source files
mypy base:                   Success: no issues found in 54 source files
$ make fmt-check lint
72 files already formatted / All checks passed!
$ uv lock --check
Resolved 118 packages
$ make test-unit
1 failed, 730 passed, 1 xfailed
  FAILED packages/core/tests/unit/test_telemetry_redaction_properties.py::test_redact_fields_matches_expected_and_leaves_its_input_alone
```

The one failure is not in my files: the property test (task 2.4 or 2.5) generated the key `XPass` and expects it to be redacted, while `is_secret_key` appears to treat `pass` as a whole word only (my reading, not checked). Either the test's generator or the rule needs a decision; I did not touch either.

Live check against the compose database (read-only, as `cairn_app` from `.env`, nothing written): `probe_database` returned a `DbProbe`, `evaluate_readiness` gave 200 with PostgreSQL 18.6, pg_search 0.26.3, vector 0.8.6, btree_gist 1.8, schema version 1. `python -m cairn_api --port 18000` served `/healthz` (200), `/readyz` (200) and logged JSON lines for uvicorn and for the 404 request with `trace_id` and `span_id`; `python -m cairn_api.healthcheck` exited 0. These live checks are not tests; task 5.4 writes the integration tests.

## Tests

- `evaluate_readiness`: the exact documented body for the ready case; each of the eight codes in isolation; several failures at once; `None` schema version as behind; minor versions of 18 pass; extra extensions not reported; missing extensions named and present ones still listed; `DbUnavailable` has no text field and bodies hold no host or secret.
- Probe (no database): connection refused gives `db_unreachable` in under 3 s and logs only class, SQLSTATE and reason; a listener that accepts and stays silent gives `db_timeout`; two calls in a row are independent; nine exception types map to fixed codes without leaking a note that carries a password and a host.
- API (fake probes, ASGI test client): `/healthz` 200 with a closed database port and without calling the probe; startup opens no connection; `/readyz` 200 with versions, eight 503 cases with their codes, missing names, 503 within 3 s on a dead database with `/healthz` still 200 and no host or secret in the body, recovery on the next request, a crashing probe gives `probe_failed` without exception text, docs routes 404.
- Logging and tracing: DEBUG for probes and INFO for other paths, silent at INFO, query string never logged, 500 logged on an unhandled error, each request gets a SERVER span and the JSON line carries its `trace_id`.
- `create_app`: exit 2 naming `CAIRN_APP_DB_PASSWORD`; exit 2 on a bad port without printing the value or the password; the order logging, tracing, psycopg instrumentation with the provider (stubs for the global calls).
- `healthcheck` against a local one-route HTTP server (200, 503, nothing listening, non-http schemes, exit codes, env override); `__main__` argument defaults and the uvicorn call.

## Deviations from the design

- **`--log-config none` does not exist.** In uvicorn 0.54 the option takes a file path (`Path 'none' does not exist`), so the design §7 command fails. The 2.2 notes repeat it. I added `python -m cairn_api` (script `cairn-api`), which calls `uvicorn.run("cairn_api.main:create_app", factory=True, log_config=None, access_log=False, server_header=False)`: uvicorn installs no logging config, its records propagate to the root JSON handler, and the access log stays off. Task 4.1 must use `cairn-api --host 0.0.0.0` (or `CAIRN_API_HOST=0.0.0.0`) instead of the uvicorn command line.
- **Default bind is 127.0.0.1** for `cairn-api`, not 0.0.0.0 as in design §7, because nothing is authenticated and a bare host run should not listen on the LAN. The container opts in to all interfaces, and compose publishes only on 127.0.0.1.
- New code `probe_failed` (connected, but a query failed or the probe crashed). The design lists no code for that case; mapping it to `db_unreachable` would mislead.
- `ReadyReport.extensions` is a dict of the required extensions that are present, as in the design; `CheckResult.detail` values may be `None` in the type, as in the design, but `to_body` omits empty detail.
- `build_app`/`create_app` split instead of one `create_app(settings)`: `create_app()` stays the argument-free uvicorn factory, and tests use `build_app`.
- `DbProbe.extensions` is filtered in SQL to the required names (`extname = ANY(...)`), so the probe does not read the whole extension list.
- Docs routes are disabled (see above); the design is silent.
- mypy treats the readiness body as `dict[str, Any]` via `to_body()`; the FastAPI app returns it as a plain `JSONResponse` instead of a `response_model`, so the 200 and 503 bodies share one shape.

## Notes for later tasks

- **5.2 (property: readiness is the conjunction of checks)**: generate `DbProbe` values (`server_version_num` in `170000..190000`, a subset of `REQUIRED_EXTENSIONS` as the `extensions` mapping, two flags, `schema_version` in `None | 0..5`, head `1..5`) and compare `evaluate_readiness(probe, head)` against a hand-written oracle. `status == 200` iff the four conditions hold; error codes per check are `postgres_major_mismatch`, `extensions_missing`, `role_unsafe`, `schema_behind`/`schema_ahead`; `report.extensions` is exactly the present required ones. `DbUnavailable` always gives 503 with a single `database` check.
- **5.3 (property: no error detail in bodies)**: `DbUnavailable` carries only a code, so the property has to go through `probe_database`'s failure path or `_failure(exc)` (private, imported in `test_health.py`) with exceptions whose text and notes hold arbitrary strings, then serialize `evaluate_readiness(...)[1].to_body()` and assert none of the strings appear. Also drive the app with a probe that raises `RuntimeError(text)`: the `/readyz` body must not contain `text`. Keep generated strings at least 8 characters, or they will match fixed body text like `ok`. For `CheckResult.detail` the role name comes from the database, so the property excludes `role.detail.name` from the "no arbitrary strings" claim.
- **5.4 (integration)**: `probe_database(settings)` takes an `ApiSettings`; build it with `ApiSettings.model_validate({"CAIRN_DB_HOST": ..., "CAIRN_DB_PORT": ..., "CAIRN_DB_NAME": test_db, "CAIRN_APP_DB_PASSWORD": ...})`, which bypasses the environment and `.env`. Cases: ready against a fresh migrated database (200, three extension versions, schema 1); `schema_behind` on a bootstrapped but unmigrated database (the table does not exist, `to_regclass` guard gives `schema_version=None`); `schema_ahead` after inserting version 2 as owner; `extensions_missing` after `DROP EXTENSION vector` in a scratch database; `role_unsafe` by pointing `app_db_user` at the superuser (`app_db_user` is a setting); `db_unreachable` on a closed port; recovery by probing the same settings before and after the target comes up (a TCP proxy or a second database); `db_timeout` with a silent listener (as in `test_health.py`). Each `/readyz` call opens a new connection, so no pool state needs resetting.
- **4.1 (compose)**: api service command is `cairn-api --host 0.0.0.0` (workdir and entry point from the venv; `uv sync` installs the script) or `python -m cairn_api` with `CAIRN_API_HOST=0.0.0.0`; healthcheck is `python -m cairn_api.healthcheck` (stdlib only, port 8000). The api container needs `CAIRN_APP_DB_PASSWORD`, `CAIRN_DB_HOST=db`, and optionally `CAIRN_DB_TIMEOUT_S` (raise it to 3 if the first cold readiness check flaps). `read_only: true` is fine: nothing writes to disk.
- **Web (7.x)**: `/readyz` returns 503 with a JSON body when not ready; the widget should treat any non-200 as "Not ready" and not parse the body.
- **6.1 (worker)**: nothing here is shared except `cairn_core.telemetry`; the worker has its own healthcheck.
- **Smoke test (4.x)**: `GET /readyz` JSON keys are as above; `extensions` is a name to version dict.
