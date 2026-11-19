# Task 5.4 notes: api compose service and readiness integration tests
Date: 27 Oct 2026. No dependency or `uv.lock` change.
## Files
| File | State |
|---|---|
| `docker-compose.yml` | `api` service added; header comments updated |
| `apps/api/tests/integration/test_readyz_integration.py` | New: 6 tests, marked `integration` |
## The api service
| Item | Value |
|---|---|
| Image / build | `cairn-api:local`, `infra/docker/python-app.Dockerfile` with `PACKAGE: cairn-api` |
| Base | `x-python-app` anchor: user 10001:10001, read-only root, tmpfs `/tmp`, `cap_drop: ALL`, `no-new-privileges` |
| Command | `cairn-api --host 0.0.0.0` |
| Environment | `CAIRN_ENV`, `CAIRN_LOG_LEVEL`, `CAIRN_DB_HOST=db`, `CAIRN_DB_PORT="5432"`, `CAIRN_DB_NAME`, `CAIRN_DB_TIMEOUT_S` (default 3), `OTEL_TRACES_EXPORTER` (default none), and the one secret `CAIRN_APP_DB_PASSWORD`. No superuser, owner or DBOS secret |
| Depends on | `db-init: service_completed_successfully` (db is reached through it) |
| Healthcheck | `python -m cairn_api.healthcheck`, 10 s interval, 6 s timeout, 6 retries, 10 s start period |
| Limit / port | `mem_limit: 384m` (inspect: 402653184); `127.0.0.1:8000:8000` |
### Deviation from design §7
The design's serving command ends in `--log-config none`. uvicorn 0.54 reads that flag as a file path, so it fails. The `cairn-api` entry point (task 5.1) passes `log_config=None` and `access_log=False` to `uvicorn.run`, which has the same effect: uvicorn installs no logging config, its records reach the root JSON handler. The compose comment records this.
### Environment note
`CAIRN_DB_TIMEOUT_S` shows 2.0 in the running container because the local `.env` sets it and compose substitutes it; 3 applies only when `.env` leaves it unset. `OTEL_EXPORTER_OTLP_ENDPOINT` is not passed: the host-side value in `.env` (127.0.0.1) would be wrong inside a container, and task 10.1 (`make up-obs`) owns that wiring.
## Integration tests
`build_app` with the real probe, called through `httpx.ASGITransport`; settings built with `ApiSettings.model_validate` and the env aliases.
| Test | Checks |
|---|---|
| ready against `fresh_db` | 200; five checks ok; role `cairn_app`; Postgres 18.x; schema version equals the bundled head; the three required extensions with versions; body hygiene |
| healthz with a dead database | 200 `{"status": "ok"}` |
| closed port | 503, exact body `database: db_unreachable`, under 3 s |
| schema behind, then migrated | 503 `schema_behind` with `version 0`/`expected` only; after migrating as owner, the same app answers 200 |
| database appears after the first call | 503 `db_unreachable` (database missing), then 200 once created, bootstrapped and migrated; the database is dropped in `finally` |
| spans | one server span, two client spans (the probe's two statements), same trace, parent is the server span, `db.system=postgresql`, no password in any attribute. psycopg instrumentation is undone after the test |
Body hygiene (every 503 and the 200 case where applicable): no password, no database port, no `psycopg` or `Traceback`, keys exactly `status/checks/extensions`, `Cache-Control: no-store`.
## Live verification
```text
$ docker compose --profile core config --hash db   # equals the label on the running cairn-db-1
```
Same hash, so `up api` leaves `cairn-db-1` alone (it showed "Up 2 hours" before and after). Brought up only `api` (plus its one-shot dependency `db-init`, which ran the idempotent bootstrap and migrate and exited 0):
```text
$ docker compose --profile core up -d --wait api      # cairn-api-1 Healthy
$ curl http://127.0.0.1:8000/readyz                   # 200, ready, pg 18.6, pg_search 0.26.3, vector 0.8.6, btree_gist 1.8, schema 1/1
$ docker port cairn-api-1                             # 8000/tcp -> 127.0.0.1:8000
```
The container log is JSON lines from uvicorn with the `service` field. Then `docker compose --profile core stop api` and `rm -f api db-init`. No volume was touched, `cairn-db-1` stayed healthy, and no `cairn_test_*` database was left.
## Not done / for later
- `CONTRIBUTING.md` was not edited: no make target changed status in this task.
