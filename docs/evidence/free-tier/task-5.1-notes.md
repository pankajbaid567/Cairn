# Task 5.1: the `cairn-combined` workspace member and the jobs runner
Date: 2 Nov 2026. Requirements 6.1, 8.1 to 8.9 and 17.3; decisions FT-10, FT-11 and FT-12; FV5 resolved in [verify/FV5.md](verify/FV5.md). No dependency was added: `uv lock` only added the new member (119 packages, `uv lock --check` passes). The local `.env` was not touched. Only the core stack was used; it is still up.

## Files
| File | State |
|---|---|
| `apps/combined/pyproject.toml` | New: `cairn-api`, `cairn-worker`, `cairn-core` as workspace sources, `fastapi==0.162.0` and `uvicorn==0.54.0` (the pins `cairn-api` already has, because the code imports both), script `cairn-combined = cairn_combined.__main__:main` |
| `apps/combined/src/cairn_combined/{__init__,__main__,app}.py`, `py.typed` | New |
| `apps/combined/tests/unit/{test_import,test_app}.py` | New: 36 tests |
| `apps/combined/tests/integration/test_combined_integration.py` | New: 2 tests (real `python -m cairn_combined` subprocess, marker `integration`) |
| `pyproject.toml` | Workspace member `apps/combined`; `apps/combined/src` in `mypy_path` |
| `mk/python.mk` | `apps/combined` in `MYPY_BASE_DIRS` |
| `uv.lock` | Adds `cairn-combined` |
| `packages/core/src/cairn_core/config.py` | `CombinedSettings(ApiSettings, WorkerSettings)`: `PORT` (10000), `CAIRN_JOBS_RETRY_MAX_S` (60, 0 < x <= 600), `heartbeat_cron` default `0 */5 * * * *` (FT-12; the worker keeps `*/30 * * * * *`; the five/six-field validator still applies) |
| `packages/core/src/cairn_core/health.py` | `JobsStatus(launched, attempts)`, `JobsProbe`, `evaluate_readiness(probe, head, jobs=None)`: a `jobs` check (`jobs_not_launched`, detail `attempts`) exists exactly when `jobs` is given, also when the database is unreachable |
| `apps/api/src/cairn_api/main.py` | `build_app(..., jobs=None, lifespan=None)`; the split `api` passes neither |
| `apps/worker/src/cairn_worker/app.py` | `schedule_spec` adds `"automatic_backfill": False` |
| `infra/docker/python-app.Dockerfile` | Copies `apps/combined` manifest and source; `ARG PACKAGE=cairn-combined`; header comment |
| `CONTRIBUTING.md` | One row for `apps/combined` in the repo layout |
| `docs/evidence/free-tier/verify/FV5.md`, `FV5-probe.py`, `FV5-probe.out` | New |
| Tests changed | `packages/core/tests/unit/test_health.py` (4 jobs tests), `apps/worker/tests/unit/test_worker_app.py` (expected spec has `automatic_backfill`) |

## `cairn_combined.app`
| Name | What it does |
|---|---|
| `backoff_schedule(attempt, cap_s)` | `min(cap, 2 ** (attempt - 1))` as a float; attempt below 1 raises `ValueError`; the exponent is clamped at 32 so a huge attempt count cannot build a huge int |
| `launch_dbos(settings)` | `prepare_dbos_logging()`, `DBOS(config=dbos_config(settings))`, `DBOS.launch()`, `DBOS.apply_schedules([schedule_spec(settings)])` |
| `destroy_dbos()` | `DBOS.destroy()` |
| `JobsRunner(settings, *, launch, destroy, wait, max_backoff_s)` | Daemon thread. Loop: count the attempt, launch; on any exception log `jobs.launch_failed` (attempt, exception class, SQLSTATE through the `__cause__` chain, `retry_in_s`; no message, no traceback), call destroy, wait `backoff_schedule(attempt, cap)`; a `True` from `wait` ends the loop. Success sets `launched` and logs `jobs.launched`. `status()` is a lock-protected `JobsStatus`. `stop(timeout=20)` sets the event, joins, and destroys DBOS only if it had launched (so each failure is destroyed once, at the failure, and a launched instance once, at stop). A failing destroy is logged and ignored |
| `runner_lifespan(runner)` | Lifespan: `runner.start()` at startup; at shutdown `runner.stop(15)` in a worker thread so the event loop stays free |
| `create_app()` | `load_settings(CombinedSettings)` (exit 2), logging and tracing as service `cairn-combined`, psycopg instrumentation, `build_app(settings, tracer_provider, jobs=runner.status, lifespan=...)` |
| `main()` | Loads the settings first (so a bad configuration exits 2 before uvicorn is touched), then `uvicorn.run(create_app, factory=True, host="0.0.0.0", port=settings.port, log_config=None, access_log=False, server_header=False, timeout_graceful_shutdown=20)` |

## Requirement 8.4 (give-up path)
Not implemented, on purpose. The requirement reads "IF DBOS cannot be constructed again in the same process, THEN end the process with status 1 after five failed launch attempts". FV5 shows DBOS 3.3.0 can be constructed again after `DBOS.destroy()` (three failures and then a success in one process), so the condition never holds and the runner retries until it works or is stopped (design Property 9). No five-attempt rule was added, because the requirement text makes it conditional. If a later DBOS version breaks this, the integration test `test_a_failing_launch_keeps_healthz_up...` plus the `retry` logs would show the stuck state, and the rule can be added then.

## Behavior worth knowing
- uvicorn 0.54 re-raises SIGTERM after a clean shutdown, so the process exit status is -15 (a container reports 143), not 0. The integration tests accept 0 or -SIGTERM. Task 6.2's `ft-app` stop test should read the time, not the status.
- A launch that blocks inside libpq (for example a black-holed host) is not interrupted. `stop()` waits 15 s at most from the lifespan; the thread is a daemon, so the process still exits. DBOS has no connect timeout in the URL. Not changed (it would touch `dbos_url`, task 4.1's Property 8).
- DBOS polls its database about 9.5 times a second while launched (FV5). Neon is awake while the combined process is awake. The consequence for the CU-hour estimate is in FV5.md.
- `/readyz` in combined mode is 503 with `jobs_not_launched` until the runner has launched; Render's health check uses `/healthz`, so a database outage does not restart the service (FT-11). The compose healthcheck of `ft-app` (task 6.1) uses `/readyz`, so the container turns healthy only once DBOS is up.
- The heartbeat cron comes from `CAIRN_HEARTBEAT_CRON` when set. The repo's `.env` sets `*/30 * * * * *` for the worker, so a combined process started from the repo root with that `.env` ticks every 30 s; the container gets the `0 */5 * * * *` default unless task 6.1 sets the variable.
- The `cairn-combined` entry in the process uses DBOS app name `cairn-worker` (from `dbos_config`). Do not run it and the core `worker` against the same `cairn_dbos` database at once; the freetier profile has its own database.

## Verify results
```text
$ uv lock --check && uv run pytest apps/combined apps/api/tests/unit apps/worker/tests/unit packages/core/tests/unit/test_health.py
Resolved 119 packages / 195 passed
$ uv run pytest -m integration apps/combined
2 passed in 10.3 s
$ make types
mypy --strict packages/core: no issues in 29 source files / base: no issues in 109 source files
$ docker build -f infra/docker/python-app.Dockerfile -t cairn-combined:local .
built; `docker run --rm cairn-combined:local cairn-combined` prints "invalid configuration: CAIRN_APP_DB_PASSWORD (missing), CAIRN_DBOS_DB_PASSWORD (missing)" and exits 2; user 10001:10001
$ docker build --build-arg PACKAGE=cairn-core|cairn-api|cairn-worker ...
all three still build (scratch tags removed)
$ make fmt-check lint test-unit
see the task report for the final numbers
```
