# Task 4.2 notes: database test fixtures and integration tests
## Files
| File | State |
|---|---|
| `packages/core/src/cairn_core/testing.py` | New: `admin_config`, `admin_conn`, `fresh_db` fixtures; `temporary_database`, `connect_admin`, `load_config`, `tables_lacking_forced_rls`, `FreshDb` |
| `conftest.py` | Re-exports the three fixtures |
| `packages/core/tests/integration/test_bootstrap_integration.py` | New: 7 tests (Requirements 5.1 to 5.6) |
| `packages/core/tests/integration/test_migrate_integration.py` | New: 9 tests (6.1 to 6.9) |
| `packages/core/tests/integration/test_schema_invariants.py` | New: 2 tests (17.5) |
| `scripts/migrations_check.py` | New: the gate behind `make migrations-check` (10.7) |
| `mk/db-tests.mk` | New: `test-integration`, `matrix`, `migrations-check` (picked up by the Makefile's `mk/*.mk` wildcard) |
## Behavior worth knowing
- Settings: `CAIRN_TEST_ADMIN_HOST`, `_PORT`, `_USER` and `POSTGRES_PASSWORD` plus the three role passwords, from the environment or `.env` in the working directory. Nothing is printed; `FreshDb` hides its connection strings from `repr`.
- Skips: unreachable server or incomplete settings skip with a message; a server that rejects the login (SQLSTATE 28xxx) fails. `migrations-check` exits 2 when the db is unreachable.
- `admin_conn` runs `bootstrap()` once per session. On a bootstrapped cluster it only re-asserts roles (new SCRAM salt, same attributes).
- Tests that edit bookkeeping rows, fail a file or drop extensions use their own `temporary_database`; `fresh_db` and the cluster's `cairn` database stay untouched. The membership refusal test seeds a role and grant inside a transaction that always rolls back.
- `make matrix` exits 5 (no tests collected) until task 4.3 adds the `matrix` tests.
## Verification
```text
$ make fmt-check lint types test-integration migrations-check
18 passed; migrations-check: ok (version 1; the second run applied nothing)
$ make test-unit
740 passed, 1 xfailed (known gap in packages/eval)
$ CAIRN_TEST_ADMIN_PORT=1 pytest -m integration   # db unreachable
18 skipped
```
`make up-db` was not re-run: the shared `cairn-db-1` container was already healthy and `up` could recreate it.
