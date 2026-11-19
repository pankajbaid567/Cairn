# Task 4.7: managed-mode test fixtures and the managed role-test case
Done 2 Nov 2026. Requirements 6.4 to 6.6, 11.6; design §9 and §15; FV4 rows 8, 11 and 12.

## What changed
- `cairn_core.testing.AdminTarget`: new `mode` (`CAIRN_TEST_ADMIN_MODE`, `superuser` by default, or `managed`). `port`, `user` and `password` are now properties that pick the selected target:
  - superuser: `CAIRN_TEST_ADMIN_PORT`, `CAIRN_TEST_ADMIN_USER`, `POSTGRES_PASSWORD` (as before);
  - managed: `CAIRN_FREETIER_DB_PORT` (default 55433), fixed user `cairn_admin` (`CAIRN_TEST_ADMIN_USER` is ignored), `CAIRN_MANAGED_ADMIN_PASSWORD`.
  - `CAIRN_TEST_ADMIN_HOST` applies to both. Only the selected mode's password is required, so managed runs need no `POSTGRES_PASSWORD`. A missing one makes `load_config` raise `DatabaseUnavailable` naming the key (never a value), which the fixtures turn into a skip.
- `connect_admin` in managed mode reports an unreachable server as `cannot reach the managed-mode test database (ft-db) at host:port (...); start it with make freetier-up, or unset CAIRN_TEST_ADMIN_MODE to test the core db`. A reachable server that rejects the login still fails loudly.
- `FreshDb.connect_admin()` added; `connect_superuser` is an alias (`FreshDb.connect_superuser is FreshDb.connect_admin`). The `admin_dsn` field keeps its name.
- `drop_database(conn, name, *, managed=None)`: still refuses names without the `cairn_test_` prefix before sending any SQL. A superuser drops directly. In managed mode it reads the database owner (`pg_get_userbyid(datdba)`, so `cairn_owner` or `cairn_dbos`), runs `SET ROLE <owner>`, drops with `FORCE`, and always runs `RESET ROLE`; a missing database does nothing. `managed=None` (callers without a config, such as the API and worker integration tests) asks the server whether the admin is a superuser. `temporary_database` passes the mode explicitly.
- `pyproject.toml`: markers `managed` and `freetier`. `mk/python.mk` `test-unit` is now `not integration and not smoke and not obs and not freetier`.
- `test_roles.py`: every test runs in both modes except the two admin tests of step 5. `test_superuser_sees_all_rows` skips in managed mode; the new `test_admin_bypasses_rls_and_is_not_a_runtime_role` (marked `managed`, module marker `matrix`) skips in superuser mode with `needs CAIRN_TEST_ADMIN_MODE=managed and ft-db (make matrix-managed)`. It asserts the admin is not a superuser, has BYPASSRLS, is none of `cairn_app`, `cairn_owner`, `cairn_dbos`, and reads all 3 rows. In managed mode the owner grants the admin `USAGE` on schema `cairn` and `SELECT` on `cairn.rls_probe` first (FV4 row 11); the admin already has CONNECT from bootstrap (FV4 row 8).
- `test_bootstrap_integration.py::test_bootstrap_twice_leaves_the_catalog_unchanged` also carries `@pytest.mark.managed`.
- New unit tests, `packages/core/tests/unit/test_testing_target.py` (34 cases): target selection in both modes (ports, user, password, host, ignoring the other mode's keys), `load_config` from the environment, missing or invalid settings name the key and no value, `connect_admin` kwargs and message in managed mode, the DSN login, the alias, and `drop_database` (prefix refusal in every mode with no SQL sent, direct drop, `SET ROLE`/drop/`RESET ROLE`, reset after a failed drop, missing database, mode detection from the admin role). The recording connection only records statement text; no database is faked for results that matter.

## Verification
- `make test-unit`: 1203 passed, 1 xfailed (the known canary gap), 52 deselected.
- `make up-db && make matrix` (default mode, core `db`): 12 passed, 1 skipped (the managed-only test, with its message).
- `make fmt-check lint types test-integration`: clean; 42 passed, 1 skipped.
- Managed mode with `ft-db` absent: `CAIRN_TEST_ADMIN_MODE=managed pytest -m "matrix or managed"` gives 14 skipped, each with the `make freetier-up` message.
- Rehearsal of the managed path on a throwaway server, since `ft-db` does not exist until task 6.1: a container `cairn-fv47-rehearsal` from the pinned pgvector image on 127.0.0.1:55499 (fake passwords, `--rm`, now stopped), `cairn-db emulate-managed-admin` with `CAIRN_ENV=ci`, then `CAIRN_TEST_ADMIN_MODE=managed CAIRN_FREETIER_DB_PORT=55499 CAIRN_MANAGED_ADMIN_PASSWORD=<fake> pytest -m "matrix or managed"`: 13 passed, 1 skipped (`test_superuser_sees_all_rows`). That run covers the managed role test, the idempotency test, `drop_database` under `SET ROLE` (no `cairn_test_*` database left on either server) and `test_bootstrap_managed_path.py` (2 passed). It is a rehearsal, not the FV4 part 2 result: that is recorded on the real `ft-db` by task 6.2.
- The core stack was left up; `cairn_pgdata` and other projects were not touched.

## For later tasks
- 6.1: `make matrix-managed` is `CAIRN_TEST_ADMIN_MODE=managed pytest -m "matrix or managed"`; the target `freetier-up` named in the skip message must exist. The key `CAIRN_FREETIER_DB_PORT` (default 55433) and `CAIRN_MANAGED_ADMIN_PASSWORD` come from `.env`.
- 6.2: record the managed matrix result in `verify/FV4.md` part 2. Expect 13 passed and 1 skipped on `ft-db`.
- Other integration tests that call `connect_superuser` still work in managed mode through the alias, but those that read tables as the admin (for example `test_migrate_integration.py`, `test_schema_invariants.py`) would need grants the admin lacks; they are not marked `managed`, so `matrix-managed` does not select them.
