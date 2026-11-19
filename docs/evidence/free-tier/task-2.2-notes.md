# Task 2.2: pg_search removed from bootstrap, readiness, the baseline migration and the tests

Date: 2 Nov 2026. Requirements 2.1 to 2.7; decisions FT-01, FT-03, FT-04; plan items 0.5, 0.16, D5, D6.

## Changes

- `cairn_core.bootstrap`: `REQUIRED_EXTENSIONS = ("vector", "btree_gist")`; new `MIN_EXTENSION_VERSIONS = {"vector": (0, 8, 0)}`. Removed `PARADEDB_SCHEMA`, `schema_grant_statements`, `_paradedb_schema_exists` and the schema probe in `bootstrap_database`, which now runs only the extension statements and the `public` revoke inside each database. Module and function docstrings updated.
- `cairn_core.health`: `parse_extension_version` (`"0.8.1"` -> `(0, 8, 1)`, `None` unless dotted digits), `meets_minimum` (missing parts read as 0, so `0.8` meets `0.8.0`), `outdated_extensions`. The `extensions` check reports `extensions_missing` when a required extension is absent, otherwise `extensions_outdated` when one is below its floor; the detail carries `missing` and/or `outdated` (extension names only). A version that cannot be parsed counts as outdated. The probe SQL is unchanged and asks for the two names only, so the body's `extensions` lists `btree_gist` and `vector`.
- `0001_baseline.sql`, the one-time in-place edit of design §4 (FT-04, deviation FX5): the extension check array is now `ARRAY['btree_gist', 'vector']`; nothing else changed.
  - Previous checksum: `5f77c7547931c0c4a563e51937905cbe37962baee0e43858f228f0c4670e3b1e` (the one Phase 0 task 4.1 notes show as `5f77c7547931` on the retired volume).
  - New checksum: `d718e6ff8a360be6e5a0a7df627b0b41a5fa474659c24b078dfda308cfe6efb0`.
  - `packages/core/tests/unit/test_migrate.py` records the previous checksum as `PREVIOUS_BASELINE_CHECKSUM`; a new test checks that `plan()` raises `ChecksumMismatch` (version 1) for an applied row carrying it and that `migrate()` runs no file (Requirement 2.7). The baseline content test asserts the two-name array.
- Tests updated (no reference left): core `test_bootstrap.py` (extension statements, the fake cluster without a schema probe, no schema grants, `describe` output, a test for the constants), `test_bootstrap_properties.py` (fake cluster), `test_health.py` (fixtures, missing cases, new cases for parsing, the floor, outdated, missing winning over outdated), `apps/api/tests/unit/test_main.py` (fixtures, an `extensions_outdated` case, the missing detail), `apps/api/tests/unit/test_readiness_properties.py` (the existing Phase 0 Property 2 oracle now knows the `vector` floor, since its version generator can draw values below 0.8.0; the retarget's Property 1 is still task 5.3), integration `test_migrate_integration.py` (dropped-extension cases), `test_bootstrap_integration.py` (no `paradedb` schema; the content database has exactly `vector` and `btree_gist` besides `plpgsql`, Requirement 2.1), `apps/api/tests/integration/test_readyz_integration.py` (comment), `tests/smoke/test_core_profile.py` (the two names, and `/readyz` lists exactly `btree_gist` and `vector`, Requirement 2.5), `apps/web/src/ReadinessStatus.test.tsx` (the hidden body example uses `btree_gist`).
- Comments without the vendor name: `docker-compose.yml`, `mk/db.mk`, `mk/stack.mk` and the `tests/scripts/test_compose_config.py` docstring now say "the old volume `cairn_pgdata`".
- New `tests/scripts/test_no_paradedb.py`: scans `apps`, `packages`, `scripts`, `tests`, `infra`, `mk`, `.github`, `docker-compose.yml` and `.env.example` byte-wise for `pg_search` or `paradedb`, case-insensitive, skipping gitignored directories. Allowlists:
  - history (Requirement 2.6): ADR-0001, ADR-0013, the 27 Oct 2026 S2 report and results, `docs/evidence/phase-0/`, `docs/evidence/free-tier/`, `var/`, links to ADR-0001's file name, and ADR-0001's row in `docs/adr/README.md`. Most lie outside today's roots; task 12.3 adds `docs/` and `CONTRIBUTING.md` (and must decide how to cover the source-document lines design §16 keeps).
  - self: the scan module and `tests/scripts/test_dev_env.py` (asserts `.env.example` has no vendor name).
  - TEMPORARY, each entry naming its task: task 3.1 removes the S2 harness files (`analyze.py`, `harness.py`, `queries.py`, `report.py`), `test_s2_analyze.py`, `test_s2_plan_analysis_properties.py`, `test_s2_harness.py` and the four plan fixtures in `packages/eval/tests/unit/data/s2_plans/`; task 10.1 removes `scripts/licenses.py` and `tests/scripts/test_licenses.py`. `test_temporary_allowlist_entries_still_need_it` fails once an entry no longer matches, so those tasks must delete their entries.
  - Unit tests of the scanner: case-insensitive matching with line numbers, the ADR-0001 link rule, the index-row rule, skipped directories and the allowlists on a temporary tree.

Not in the scan and not changed: the local `.env` (gitignored, one old comment line; never rewritten, Requirement 22.3) and the documents (task 12.3).

## Verify

`make fmt-check lint types test-unit`

Result (2 Nov 2026): passed. 117 files formatted, ruff clean, mypy strict (core, 23 files) and base (94 files) clean; 976 passed, 49 deselected, 1 xfailed (the known `canary_token` strict xfail). Also passed: `make web-check` (8 vitest tests, build).

Side note: running `packages/core/tests/unit` and `apps/api/tests/unit` together in one ad hoc pytest call fails `test_probe_endpoints_log_at_debug_and_other_paths_at_info` (an httpx request log line lands in `caplog`). It passes in `make test-unit` and on its own; the ordering effect is not related to this task.

## make db-init on the new volume

Run once against `cairn-db-1` (`pgvector/pgvector:0.8.6-pg18`, volume `cairn_pgdata-pgvector`, empty before). api, worker and web stayed down; `make up`, smoke and test-integration were not run (task 2.3).

Output:

```
role cairn_owner: created
role cairn_app: created
role cairn_dbos: created
database cairn: created
extensions cairn: btree_gist, vector
database cairn_dbos: created
bootstrap complete (password encryption: libpq)
applied 0001_baseline.sql
applied 1 migration(s); now at version 1
```

Read back as `postgres`: database `cairn` has `btree_gist 1.8`, `plpgsql 1.0`, `vector 0.8.6` and the schemas `cairn`, `cairn_meta`, `public`; `cairn_dbos` has `plpgsql` only; `cairn_meta.schema_migrations` holds `1 | baseline | d718e6ff…efb0`. `cairn-db status`: `head: 1  current: 1  pending: 0`. `cairn_pgdata` and `cairn_pgdata-pgvector` both exist and no container mounts `cairn_pgdata`.
