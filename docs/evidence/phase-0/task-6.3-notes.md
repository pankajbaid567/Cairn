# Task 6.3 notes: worker compose service and heartbeat integration test

Date: 27 Oct 2026. No dependency or `uv.lock` change. `docs/adr/0002` not edited (the task does not ask for it).

## Files

| File | State |
|---|---|
| `docker-compose.yml` | `worker` service added; header comments updated |
| `apps/worker/tests/integration/test_heartbeat_integration.py` | New: 2 tests, marked `integration` |

## The worker service

| Item | Value |
|---|---|
| Image / build | `cairn-worker:local`, `infra/docker/python-app.Dockerfile`, `PACKAGE: cairn-worker` |
| Base | `x-python-app` anchor (user 10001:10001, read-only root, tmpfs `/tmp`, `cap_drop: ALL`, `no-new-privileges`) |
| Command | `python -m cairn_worker` |
| Environment | `CAIRN_ENV`, `CAIRN_LOG_LEVEL`, `CAIRN_DB_HOST=db`, `CAIRN_DB_PORT=5432`, `CAIRN_DB_TIMEOUT_S`, `CAIRN_HEARTBEAT_CRON` (default `*/30 * * * * *`), `OTEL_TRACES_EXPORTER`, and the one secret `CAIRN_DBOS_DB_PASSWORD`. No superuser, owner or app password. `CAIRN_DBOS_DB_NAME` is not passed: bootstrap fixes the database name to `cairn_dbos`, which is also the setting's default |
| Depends on | `db-init: service_completed_successfully` |
| Healthcheck | `python -m cairn_worker.healthcheck`, interval 10 s, timeout 6 s, 6 retries, **start period 30 s** (the liveness file appears after DBOS migrations and the schedule; on an empty database that is a few seconds, and 30 s leaves margin) |
| Limit / port | `mem_limit: 384m` (inspect: 402653184); no `ports` (inspect: `map[]`) |
| Stop | `stop_grace_period: 20s` |

`docker compose --profile core config --hash db` is unchanged by this edit (`6e9b0dfb...`, equal to the label on the running `cairn-db-1`), so `up worker` does not recreate the database. `up` only started `db-init` (idempotent bootstrap and migrate, exit 0) and `worker`.

## Requirement 8.7: stop time (measured)

`docker compose --profile core stop worker`, wall clock including compose's own start-up, three runs on the live stack:

| Run | `stop` wall time | Container exit | In-process `worker.stopped` `shutdown_s` |
|---|---|---|---|
| 1 | 1.38 s | 0 | 0.05 s |
| 2 | 0.74 s | 0 | 0.03 s |
| 3 | 0.61 s | 0 | 0.02 s |

All exits were 0, not 137, so Docker never had to SIGKILL. The margin to the 10 s limit is about 7x on the slowest run. `stop_grace_period` is 20 s so that an overrun would show up as a late but clean exit in the log instead of a SIGKILL at exactly the limit; if it ever exceeds 10 s, the `shutdown_s` field says whether DBOS's `destroy()` is responsible.

## Integration tests

The database is the live compose `db`, but each test creates `cairn_test_dbos_<hex>` (bootstrapped with `dbos_spec`: owned by and open to `cairn_dbos` only) and drops it afterwards. The live `cairn_dbos` database and the `worker` container are never touched by the tests.

| Test | Checks |
|---|---|
| `test_heartbeat_schedule_succeeds_and_dbos_tables_are_owned_by_cairn_dbos` | `DBOS(config=dbos_config(settings))`, `launch()`, `apply_schedules([schedule_spec(settings)])` with `*/5 * * * * *`; polls `DBOS.list_workflows(schedule_name="cairn-heartbeat", status="SUCCESS")` for up to 15 s (three intervals) and finds a run whose id starts with `sched-cairn-heartbeat-`; a second `apply_schedules` leaves exactly one schedule; after `destroy()`, as superuser: schema `dbos` and all its tables are owned by `cairn_dbos`, `workflow_status` and `operation_outputs` exist, and `workflow_status` holds a SUCCESS row for the schedule |
| `test_worker_process_starts_stays_alive_and_stops_within_ten_seconds` | Runs the real `cairn_worker.app.main()` as a subprocess (only the liveness file path is moved to `tmp_path`, so the host's `/tmp` is not written). Waits for `worker.started`, the liveness file, `worker.heartbeat` in the log, then `healthcheck.main` returns 0; sends SIGTERM and requires exit 0 in under 10 s, `worker.stopped` logged, and the DBOS password absent from all output |

Last run: 2 passed in 10.3 s. In the subprocess run the stop took 0.18 s.

The task text puts `docker compose stop worker` in the integration test. A pytest test that stops the shared compose `worker` would fight a developer's running stack, so the pytest test uses the same SIGTERM path in a subprocess and the container measurement is the table above.

## Findings from the first real launch

The worker had never run against a database. Nothing needed fixing; what was learned:

- DBOS's migrations (about 50 of them, up to number 123) ran as `cairn_dbos` in an empty database without any extension, as V2 predicted; the `postgres`-database existence check passed.
- The DBOS logger, with `prepare_dbos_logging()`, emits JSON through Cairn's formatter with `logger = "dbos"` and `service = "cairn-worker"`. Its URL line shows the password as `***`. `worker.heartbeat` carries `trace_id` and `span_id`.
- Noise: DBOS logs one INFO line per applied migration on a first launch (about 50 lines) and `To view and manage workflows, connect to DBOS Conductor at https://console.dbos.dev/...` on every launch. That line is a log message only; no Conductor key is set and no connection is made. Not suppressed.
- Pickup latency: the schedule's first run started about 1 to 2 s after its scheduled second (`scheduled_time` 16:33:30, logged 16:33:32; 16:31:55 vs 16:31:56 in the test run), consistent with the 1 s polling in V2.
- In the container, `/tmp/cairn-worker/alive` is created by uid 10001 on the tmpfs and the container reports healthy about 10 s after start.
- The live `cairn_dbos` now holds the `dbos` schema owned by `cairn_dbos` with 2 SUCCESS `cairn-heartbeat` rows from these runs. That is expected state, not something to clean.
- Teardown in the test is clean under `filterwarnings = ["error"]`: no thread, resource or deprecation warning from DBOS launch or destroy in-process.

## V2: same-transaction enqueue (for ADR-0002's pending item)

Confirmed by reading source in 6.1 and not contradicted by the runs here: DBOS 3.3.0 can enqueue inside a caller's transaction only through its SQL function `dbos.enqueue_workflow(...)`, which must be called on a connection to the database that holds schema `dbos`. With the separate `cairn_dbos` database (PD-07), a `cairn` connection cannot reach it, and the Python enqueue APIs use their own connection. So "the job exists if and only if the content write commits" is not available as designed. Options, none taken: (a) transactional outbox table in `cairn` drained by the worker into DBOS; (b) schema `dbos` inside database `cairn` with a narrow `EXECUTE` grant on `dbos.enqueue_workflow` for `cairn_app`, which weakens PD-07's separation. Details are in `verify/V2.md` ("Enqueue on a caller's transaction"). The ADR-0002 owner (task 12.x / the pending X7 item) should take it from there.

## Verification

```text
$ uv run pytest -m integration apps/worker
2 passed, 42 deselected in 10.32s
$ docker compose --profile core up -d --build --wait worker   # cairn-worker-1 Healthy; db untouched (same StartedAt)
$ docker compose --profile core stop worker                   # 1.38 s, 0.74 s, 0.61 s; exit 0
$ docker compose --profile core rm -f worker db-init          # only what this task started
```

Final `make fmt-check lint types test-unit test-integration compose-check` output is in the task report.
