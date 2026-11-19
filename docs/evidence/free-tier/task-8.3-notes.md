# Task 8.3 notes: neuron budget, usage stores and migration 0002
Date: 3 Nov 2026 (host clock). Requirements 15.1 to 15.6; design §4 and §12; decision FT-22. No git command was run. `.env` was not touched. `apps/web` was not touched. No Cloudflare, Neon or Render call was made.

**Result: PASS.** The previous run had already written the migration, `budget.py` and both test files. They were inspected, kept, and corrected in three places (below).

## What exists
- `packages/core/src/cairn_core/migrations/sql/0002_provider_usage.sql`: identical to design §4 plus a header comment. Table `cairn_meta.provider_usage` (`day`, `provider` with the `^[a-z][a-z0-9_]{0,31}$` check, `neurons_reserved >= 0`, `updated_at`), `REVOKE ALL ... FROM PUBLIC`, `GRANT SELECT, INSERT, UPDATE ... TO cairn_app` (no DELETE).
- `packages/retrieval/src/cairn_retrieval/cloudflare/budget.py`: `NeuronRate`, `NEURON_RATES`, `estimate_tokens`, `estimate_input_tokens`, `estimate_neurons` (exact `Fraction` arithmetic, at least 1), `UnpricedModel`, `UsageStore`, `InMemoryUsageStore` (asyncio lock, same provider rule as the table), `PostgresUsageStore` (one short-lived autocommit connection per call as `cairn_app`, `libpq_tls`, statement timeout, `repr` without credentials; one conditional upsert), `BudgetExhausted`, `NeuronBudget` (`reserve`, `used`, `cap`, UTC day). A Postgres failure propagates and the caller makes no call (fail closed).
- `packages/retrieval/pyproject.toml` declares `psycopg[binary]==3.3.6`; `uv lock --check` passes.
- Tests: `packages/retrieval/tests/unit/test_budget.py` (the estimator, the in-memory store and the guard) and `tests/integration/test_budget_integration.py` (7 cases, markers `integration` and `managed`): migration recorded and owner `cairn_owner`; exactly SELECT, INSERT, UPDATE for `cairn_app`, none for PUBLIC; `cairn_app` cannot DELETE; CHECK constraints; totals persist across store instances; an oversize first reservation inserts nothing; 20 concurrent reservations of 7 under a cap of 100 accept exactly 14 and total 98; Postgres and in-memory stores agree on a fixed sequence.

## Changes made in this run
1. `NEURON_RATES["@cf/baai/bge-small-en-v1.5"]` 1,836 to **1,841** (pricing page on 3 Nov 2026; FV9) and the two test expectations that used it. All other rates match the page.
2. The two integration tests that read `cairn_meta` through `connect_admin()` now use `connect_owner()`. In managed mode (`cairn_admin`, a non-superuser) the admin has no privilege on schema `cairn_meta`, so those tests failed on `ft-db` with `permission denied for schema cairn_meta`. They passed on the core `db` only because the admin is a superuser there.
3. `verify/FV9.md` written (new); `verify/FV8.md` written at the orchestrator's request (task 8.7 owns the client; no client code was written here). README rows added.

## FV9 in one paragraph
Rates for the five models match the table except bge-small (1,841). `gemma-4-26b-a4b-it` and `glm-4.7-flash` are not on the pricing page's paid-only list, so they are free-plan models. Both are reasoning models whose usage object carries `completion_tokens_details.reasoning_tokens`; reasoning counts as output, so `max_tokens_out` must cover reasoning plus the answer. Design §12 figures change by at most one neuron (corpus embed 220.9, so 221), so the design text is left as written. Open: confirm on the first live call that `completion_tokens` includes reasoning tokens.

## FV8 in one paragraph
Resolved from the docs for task 8.7: endpoint and envelope, shapes of the four default models (the text models answer in the OpenAI `choices` shape), embedding batch cap of 100 per call in the schema (the design's 50 stays), `DAILY_LIMIT_CODES = {3036}` (also 3040 is a retryable capacity 429), and that `Retry-After` is documented for Cloudflare rate limits but not stated for Workers AI. See `verify/FV8.md`.

## Verified
- `uv lock --check`: ok.
- `uv run pytest packages/retrieval/tests/unit`: 36 passed (includes `test_import.py`).
- `make fmt-check lint types`: clean (mypy strict over `packages/core`, base config over `packages/retrieval`).
- `make test-unit`: 1465 passed, 1 xfailed (the `canary_token` strict xfail is unchanged), 73 deselected.
- `make up-db && make test-integration matrix migrations-check` on the core `db` (superuser mode): 51 passed, 1 skipped; matrix 12 passed, 1 skipped; `migrations-check: ok (version 2; the second run applied nothing)`.
- Managed mode, which the `managed` marker promises: `make down`, `make freetier-up`, `CAIRN_TEST_ADMIN_MODE=managed pytest -m "matrix or managed"` on `ft-db`: first run 2 failed (change 2 above), after the fix 20 passed, 1 skipped (the superuser-only baseline). Then `make freetier-down` and `make up`; the profiles never ran together.
- After `make up`: `make db-status` shows head 2, pending 0 (`0001 baseline`, `0002 provider_usage`, applied by `db-init`); `/readyz` is `ready` with schema `version 2, expected 2`, `vector 0.8.6`, PostgreSQL 18.6.
- `docker volume inspect cairn_pgdata` succeeds and no container mounts it.

## Notes for later tasks
- The core stack's `cairn_pgdata-pgvector` volume now holds migration 0002. A database created before this task is migrated by `make db-init` (run by `make up`); the checksum of 0002 is `6bf23a1cec22…`.
- Properties 19 to 21 (tasks 8.4 to 8.6) are not written here; the in-memory concurrency test and the fixed-sequence parity test are unit/integration examples only.
- The client (task 8.7) takes `estimate_neurons(...)` from this module and reserves before each attempt through `NeuronBudget.reserve`.
