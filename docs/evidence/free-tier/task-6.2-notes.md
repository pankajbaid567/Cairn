# Task 6.2: the freetier smoke tests, the make targets and the managed matrix
Date: 2 Nov 2026. Requirements 5.10, 5.11, 6.3, 6.6, 7.1, 8.7, 10.8, 11.1 to 11.6; design §9. No dependency was added; `.env` was not touched. The core stack was stopped (`make down`) for the freetier runs and is up again (`make up`: db, api, worker, web healthy; `make db-status`: head 1, pending 0).

## Files
| File | Change |
|---|---|
| `tests/freetier/test_freetier_profile.py` | New, marker `freetier`, 12 tests |
| `mk/freetier.mk` | New: `freetier-up`, `freetier-smoke`, `matrix-managed`, `freetier-ram` (placeholder), `freetier-check`, `freetier-down`. The Makefile includes `mk/*.mk` by wildcard, so no include was added |
| `tests/scripts/test_no_paradedb.py` | `SELF_ALLOWLIST` gains the new test module (it asserts `pg_search` is absent and must spell the name) |
| `CONTRIBUTING.md` | One row for the six targets; the `compose-check` row now names the `freetier` profile. `make docs-check` is green |
| `docs/evidence/free-tier/verify/FV4.md` | Part 2 filled in (13 passed, 1 skipped on `ft-db`) |
| `docs/evidence/free-tier/README.md` | Rows for this note and FV4 |

## The tests
All of them take their ports and secrets from the environment or `.env` through a small pydantic `FreetierEnv` (`SecretStr` fields; a missing secret is a skip that names the key). They skip with ``the freetier profile is not running (start it with `make freetier-up`)`` when `ft-app` is not running; with the core profile up they skip all 12. Once `ft-app` runs, a failure is a failure.

| Test | Checks |
|---|---|
| `test_healthz_is_200_without_the_proxy_header` | Requirement 11.2 |
| `test_readyz_is_404_without_the_header_and_for_a_wrong_one` | 404 `{"error": "not_found"}` with `Cache-Control: no-store`, for no header and for a same-length wrong token |
| `test_readyz_is_200_with_the_header_and_every_check_passes` | Polls up to 120 s, then: status `ready`, checks exactly `database postgres extensions role schema jobs`, all ok, `extensions` exactly `btree_gist` and `vector` |
| `test_heartbeat_has_a_success_run_in_the_dbos_database` | Requirement 11.3, below |
| `test_pg_search_is_neither_installed_nor_available` | As `cairn_admin`: 0 rows in `pg_available_extensions` and 0 in `pg_extension` of `postgres` and `cairn` |
| `test_cairn_admin_is_not_a_superuser_and_bypasses_rls` | `pg_roles`: NOSUPERUSER, BYPASSRLS, CREATEROLE, CREATEDB, NOREPLICATION |
| `test_ft_app_has_the_render_free_limits` | `docker inspect`: Memory and MemorySwap 512 MiB, NanoCpus 100,000,000, read-only root, user 10001, not OOM-killed |
| `test_ft_app_environment_names_only_the_allowed_secrets` | Secret-like names (`PASSWORD`, `TOKEN`, `SECRET`, `CREDENTIAL`, `API_KEY`, `PRIVATE`) on `ft-app` are exactly `CAIRN_APP_DB_PASSWORD`, `CAIRN_DBOS_DB_PASSWORD`, `CAIRN_PROXY_TOKEN`; none of the four admin, owner or superuser keys is present |
| `test_only_ft_db_and_ft_admin_init_hold_the_superuser_password` | `POSTGRES_PASSWORD` is in the environment of exactly those two, not in `ft-db-init` |
| `test_the_one_shot_init_containers_exited_cleanly` | `ft-admin-init` and `ft-db-init` exited with 0 |
| `test_only_loopback_ports_are_published` | Port bindings of `ft-db` and `ft-app` are on 127.0.0.1 and exist |
| `test_zz_stopping_ft_app_takes_under_20_seconds` | Last in the module; times `docker compose --profile freetier stop ft-app`, asserts the container is no longer running and the elapsed time is under 20 s. The exit status is not asserted (uvicorn re-raises SIGTERM, task 5.1) |

Choices:
- **Heartbeat check reads the DBOS database as `cairn_dbos`** (`dbos.workflow_status`, `schedule_name = 'cairn-heartbeat'`, `status = 'SUCCESS'`) over the published ft-db port, with the password from `.env`. Chosen over reading logs because it tests the runtime role and the stored result, and the log format is not a contract. It polls every 5 s for up to 240 s and passes on the first row. Requirement 11.3 says three 10 s intervals; at 0.1 CPU the first run can come later (FV13), and the deadline is the only thing that is looser than the requirement text. On the runs below the row was already there when the test ran (the test took well under a second), because `up --wait` returns only after `/readyz` is ok, which needs DBOS launched.
- **Secrets never reach the output.** Container environments are reduced to names the moment they are read (`_env_names`), assertion messages list names only, the proxy token and passwords stay in `SecretStr` until they are passed to httpx or psycopg, and there are no value comparisons on environment entries.
- `SHOW shared_preload_libraries` was in the first draft and removed: `cairn_admin` is not allowed to read that setting (`permission denied to examine "shared_preload_libraries"`, needs `pg_read_all_settings`). The `pg_available_extensions` and `pg_extension` checks cover the requirement.
- The module does not restart `ft-app`. After a smoke run `ft-app` is stopped; `ft-db` stays up, which is all `matrix-managed` needs.

## Make targets
`freetier-up` is `docker compose --profile freetier up --wait --build`, with the same failure report as `make up` (container state and the last 50 log lines). `freetier-smoke` is `pytest -m freetier tests/freetier`. `matrix-managed` is `CAIRN_TEST_ADMIN_MODE=managed pytest -m "matrix or managed"`. `freetier-ram` prints "not implemented yet; scripts/freetier_ram.py arrives with task 6.3" and exits 1. `freetier-check` runs `freetier-up freetier-smoke matrix-managed freetier-ram freetier-down` from a shell loop with `set -e` (so also under `make -j`), stops at the first failure and leaves the profile up for debugging, like `make gates`. `freetier-down` is `docker compose --profile freetier down`: no `--volumes`, no `--remove-orphans`. `make -n freetier-check` lists the five steps. I did not run `freetier-check` end to end, as instructed; it fails at `freetier-ram`. `test-unit` already excludes the `freetier` marker (task 4.7): 1307 passed, 66 deselected, 1 xfailed.

## Verify results
```text
$ make down && make freetier-up freetier-smoke matrix-managed && make freetier-down && docker volume inspect cairn_pgdata-pgvector cairn_pgdata >/dev/null
freetier-up: ft-db healthy, ft-admin-init and ft-db-init exited 0, ft-app healthy (cold start at the start of this session: about 2.5 minutes)
freetier-smoke: 12 passed in 10.4 s; slowest: the ft-app stop at 9.53 s (budget 20 s), /readyz 0.21 s
matrix-managed: 13 passed, 1 skipped (test_superuser_sees_all_rows, superuser mode only) in 2.2 s
freetier-down: four containers and the network removed (no core containers were running); both volumes still present
$ make fmt-check lint types test-unit docs-check compose-check
all pass: 1307 passed, 66 deselected, 1 xfailed (the canary xfail); docs-check ok; compose-check ok for core, obs, experiments, freetier
$ make up && make db-status
core healthy again; head 1, pending 0
```
Notes on the runs:
- The first smoke run failed one test (the `shared_preload_libraries` query above) and, because the module ends by stopping `ft-app`, the second run needed a fresh `make freetier-up` (images were cached; I did not time it). That was a test defect, fixed in the test; no code under test changed.
- Before the final Verify sequence the freetier profile from the earlier runs was still up; `make down` only touches the core profile, so `freetier-up` re-ran the init containers against the same `ft-db` (they are idempotent) and recreated `ft-app`. The cold start was the first run of the session.
- `cairn_pgdata` and `cairn_pgdata-pgvector` were present before and after. No `--volumes`; `openwa-api` and the `taskflow-*` containers were not touched.

## For later tasks
- **6.3:** `freetier-check` runs the RAM check after the smoke module, and the smoke module leaves `ft-app` stopped. `scripts/freetier_ram.py` must bring the profile up itself (`docker compose --profile freetier up --wait`, as `measure_ram.py` does for core), which also restarts a stopped `ft-app`; plan for the 2.5 minute bring-up and for the 60 s HTTP timeouts at 0.1 CPU. The proxy token comes from `.env` and must not be printed.
- **6.5:** CI needs `make down` before `freetier-check`; the loop in `freetier-check` is the thing to call.
- **12.3:** the new test module is in `SELF_ALLOWLIST` of `tests/scripts/test_no_paradedb.py`; keep it there when the scan widens.
