# Task 6.1 notes: DBOS worker

Date: 27 Oct 2026. Dependencies and uv.lock unchanged (`uv lock --check` passes, 118 packages; `uv sync --locked --all-packages` clean). No `[project.scripts]` entry: the container runs `python -m cairn_worker`, as in design §8.

## Files

| File | State |
|---|---|
| `apps/worker/src/cairn_worker/app.py` | New |
| `apps/worker/src/cairn_worker/healthcheck.py` | New, standard library only |
| `apps/worker/src/cairn_worker/__main__.py` | New: `raise SystemExit(main())` |
| `apps/worker/tests/unit/test_worker_app.py` | New, 15 tests |
| `apps/worker/tests/unit/test_worker_liveness.py` | New, 23 test cases (parametrized) |
| `docs/evidence/phase-0/verify/V2.md` | New: Verify V2 findings |

## Public interface

`cairn_worker.app`:

| Name | What it does |
|---|---|
| `SERVICE_NAME`, `SCHEDULE_NAME` (`cairn-heartbeat`), `SCHEDULE_CONTEXT` (`phase-0`), `LIVENESS_INTERVAL_S` (10), `SYS_DB_POOL_SIZE` (5) | constants |
| `dbos_url(settings) -> str` | `postgresql://cairn_dbos:<percent-encoded password>@host:port/<dbos_db_name>`; IPv6 hosts bracketed. Contains the password |
| `describe_target(settings) -> str` | `cairn_dbos@host:port/db`, safe for logs |
| `dbos_config(settings) -> DBOSConfig` | name `cairn-worker`, `system_database_url`, `sys_db_pool_size=5`, `run_migrations=True`, `enable_otlp=False`, `log_level` |
| `prepare_dbos_logging()` | NullHandler on the `dbos` logger and `propagate=True`, so DBOS records reach the root JSON handler (V2) |
| `heartbeat_record(scheduled_time, context) -> dict[str, str]` | Pure. Keys `schedule_name`, `scheduled_time` (UTC ISO), `schedule_context`; none collides with a `LogRecord` attribute |
| `heartbeat(scheduled_time, context) -> None` | `@DBOS.workflow()`; span `worker.heartbeat` with attributes, one `worker.heartbeat` INFO line |
| `schedule_spec(settings, workflow_fn=heartbeat) -> ScheduleInput` | The `apply_schedules` entry; `schedule` is `settings.heartbeat_cron` |
| `make_db_probe(settings) -> Callable[[], None]` | New short-lived psycopg connection as `cairn_dbos` (keyword arguments, no URL), `SELECT 1` |
| `LivenessThread(probe, path=ALIVE_FILE, every_s=10)` | Daemon thread; `tick()` probes then touches the file, returns whether it did; `run()` ticks immediately and then every `every_s`; `stop(timeout=5)` |
| `wait_for_signal(signals) -> int` | Blocks the main thread, returns the signal number, restores old handlers |
| `run(settings, stop_signals) -> int` | `prepare_dbos_logging`, `DBOS(config)`, `launch`, `apply_schedules`, liveness thread, wait, stop thread, `DBOS.destroy()`. Returns 0, or 1 after logging `worker.failed` (class and SQLSTATE plus traceback) |
| `main() -> int` | `load_settings(WorkerSettings)` (exit 2 on bad config), `configure_logging`, `configure_tracing`, `run(settings, (SIGTERM, SIGINT))` |

`cairn_worker.healthcheck`: `ALIVE_FILE` (`/tmp/cairn-worker/alive`), `MAX_AGE_S` (30.0), `is_fresh(now, stamp, max_age_s=30.0) -> bool`, `read_stamp(path) -> float | None`, `main(argv=None, *, path=ALIVE_FILE, clock=time.time) -> int` (0 healthy, 1 with one line on stderr). `python -m cairn_worker.healthcheck` works without importing DBOS.

## Behavior worth knowing

- Freshness is strict: age 29.999 s is healthy, 30.0 s is not (Requirement 8.6, "younger than 30 seconds"). A stamp in the future (clock skew) counts as fresh. The file's mtime is the stamp.
- A failing probe or an unwritable path never touches the file; it logs `worker.liveness_probe_failed` or `worker.liveness_touch_failed` with `error_class` (and `sqlstate`), never the exception text, so the file goes stale and the healthcheck fails.
- The thread's first tick runs when it starts, which is after `launch` and `apply_schedules`, so a fresh file means DBOS is up and the schedule is registered.
- Everything under `run` after `DBOS(...)` is in a `finally` that stops the thread and calls `DBOS.destroy()`; if the constructor itself fails, destroy is skipped.
- The app never logs `dbos_config(...)`, `dbos_url(...)` or the password. A test asserts the password is absent from the JSON log stream while `DBOS` is constructed and destroyed.
- Environment for a real run: `CAIRN_DBOS_DB_PASSWORD` (required), `CAIRN_DB_HOST`, `CAIRN_DB_PORT`, `CAIRN_DBOS_DB_NAME`, `CAIRN_HEARTBEAT_CRON`, `CAIRN_LOG_LEVEL`, `OTEL_TRACES_EXPORTER`, `OTEL_EXPORTER_OTLP_ENDPOINT`. `python -m cairn_worker` with no password prints `invalid configuration: CAIRN_DBOS_DB_PASSWORD (missing)` and exits 2 (checked).

## Verify V2 summary (details in `verify/V2.md`)

- Schedule API: `DBOS.apply_schedules([...])` after `launch()`; create-or-replace in one transaction; workflow signature `(datetime, Any) -> None`.
- Cron: six fields accepted, **seconds first**; five fields are minute first.
- Runs of a schedule: `DBOS.list_workflows(schedule_name="cairn-heartbeat", status="SUCCESS")`, scoped to this app name by default.
- Privileges: no `CREATE EXTENSION` in DBOS migrations. `cairn_dbos` needs CONNECT and CREATE on its own database and ownership of schema `dbos`, which it has. With a URL, DBOS also connects to the `postgres` database once to check existence and only warns if it cannot, so do not revoke PUBLIC CONNECT on `postgres`.
- Logging: DBOS cuts its logger off from root unless it already has a handler; `prepare_dbos_logging` fixes that (unit-tested).
- Shutdown: DBOS installs no signal handlers; `destroy()` can in the worst case take several 10 s joins, so 8.7 needs measuring in 6.3.
- Enqueue on the caller's transaction: only with the SQL function `dbos.enqueue_workflow(...)` and only if the DBOS tables are in the same database as the content tables. The Python enqueue APIs use their own connection. With PD-07 as designed it is not available; options (outbox, or schema `dbos` inside `cairn`) are in V2.md for ADR-0002.

## Verification

```text
$ uv run pytest apps/worker/tests/unit
40 passed in 1.12s (2 earlier import tests + 15 + 23)
$ make fmt-check lint
70 files already formatted / All checks passed!
$ make types
mypy --strict packages/core: Success: no issues found in 18 source files
mypy base: Success: no issues found in 52 source files
$ uv lock --check
Resolved 118 packages
$ python -W error -m cairn_worker.healthcheck   (no file)
unhealthy: liveness file missing, exit 1
```

Not verified: DBOS launch, migrations as `cairn_dbos`, the schedule actually firing, `docker stop` timing and `list_workflows(schedule_name=...)` output. They need the compose database and belong to task 6.3.

## Tests

- `heartbeat_record`: UTC, other zone converted, naive read as UTC, keys safe as `extra=`.
- `schedule_spec`: default and custom cron, the workflow object. The meaning of 5- and 6-field cron strings is checked with croniter (imported dynamically because it ships no types).
- `dbos_config` and `dbos_url`: role, database, port, percent-encoding of `/ : @ #`, IPv6, database name setting, `describe_target` free of the password.
- Logging: with `configure_logging` on a `StringIO`, `prepare_dbos_logging` plus `DBOS(config=...)` (no launch) leaves `propagate=True`, no `StreamHandler`, and a DBOS record comes out as JSON with `service=cairn-worker`; the password is absent. The test restores root and `dbos` logger state.
- `wait_for_signal`: a timer thread raises SIGUSR1; the call returns it and restores the handler.
- Healthcheck: `is_fresh` at ages -5, 0, 9.9, 29.999, 30, 30.001, 3600 and for a missing stamp; `main` on missing, 1 s, 29 s, 31 s, 600 s files.
- Liveness: `tick` creates the directory and refreshes an old mtime; a failing probe leaves the file missing or unchanged and logs only the class; an unwritable path returns False; the thread ticks at 10 ms intervals (semaphore, no sleeps) and stops; a failing probe never creates the file; the probe against a closed local port raises `OperationalError` without the password.

## Deviations from the design

- `heartbeat` returns `None`, not a dict. DBOS types scheduled workflows as `Callable[[datetime, Any], None]`, and mypy rejects a dict-returning function as `workflow_fn`. The record is logged and put on the span instead.
- Added `prepare_dbos_logging()` (not in the design): the design assumed DBOS logs propagate; they do not by default (V2).
- `wait_for_signal` polls `Event.wait(0.25)` instead of blocking: a bare wait hangs when the signal is delivered to another thread.
- `run` returns an exit status and `main` returns it; `__main__` wraps it in `SystemExit`. The design's `main()` returned `None`.
- Added `log_level` to the DBOS config so DBOS follows `CAIRN_LOG_LEVEL`.
- `describe_target`, `make_db_probe`, `run` and the `ALIVE_FILE`/`MAX_AGE_S` constants are additions; the healthcheck constants live in `healthcheck.py` so that module stays stdlib-only.

## Notes for later tasks

- 6.2 (property: worker health follows heartbeat freshness): generate `age_s` over, say, floats from -60 to 3600 plus `None`; assert `is_fresh(now, None if missing else now - age_s) == (not missing and age_s < 30.0)`. For the file-based version use `main(path=..., clock=lambda: now)` with `os.utime`, as in `test_worker_liveness.py`. Tag it with Property 8.
- 6.3 (compose service and integration test):
  - Command `python -m cairn_worker`; healthcheck `python -m cairn_worker.healthcheck`, interval 10 s, `read_only: true` with `tmpfs: /tmp` (the liveness file is `/tmp/cairn-worker/alive`, created by the thread, so the mount must be writable by uid 10001).
  - Environment: `CAIRN_DBOS_DB_PASSWORD` only for the password; `CAIRN_DB_HOST=db`, `CAIRN_HEARTBEAT_CRON`, `CAIRN_LOG_LEVEL`, `OTEL_*`. The worker needs no other password.
  - Integration test: start `cairn_worker.app.run` is awkward in-process (signals need the main thread); easier is `DBOS(config=dbos_config(settings))`, `DBOS.launch()`, `DBOS.apply_schedules([schedule_spec(settings)])`, wait up to three intervals, then `DBOS.list_workflows(schedule_name=SCHEDULE_NAME, status="SUCCESS")`, then `DBOS.destroy()`. Use `CAIRN_HEARTBEAT_CRON="*/5 * * * * *"` and `WorkerSettings`-compatible environment for a test database named `cairn_dbos_test_<hex>` created by bootstrap code (the role needs it owned by `cairn_dbos`; `bootstrap_database`/`DBOS_DATABASE` spec in `cairn_core.bootstrap`). DBOS global state is per process: destroy it at the end, and do not run two such tests in one session without `DBOS.destroy(destroy_registry=False)`.
  - New schedules are picked up within about 1 s for the first minute after launch, then every 30 s.
  - Measure `docker compose stop worker` time for Requirement 8.7 (limit 10 s); if it is longer, pass `stop_grace_period` and look at which DBOS background thread joins last.
  - First launch logs a DBOS warning if `cairn_dbos` cannot connect to the `postgres` database; it can by default.
- 5.x (api): same `configure_logging`/`configure_tracing` calls; no DBOS import in the API.
- ADR-0002 (task 12.x): use the "Enqueue on a caller's transaction" row of V2.md for the pending X7 item.
