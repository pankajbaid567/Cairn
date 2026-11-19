# Task 2.1: the db service on the pgvector image and the new volume

Date: 2 Nov 2026. Requirements 1.1 to 1.6; plan items 0.4, D5.

## Changes

- `docker-compose.yml`: top-level anchor `x-pg-image: &pg-image pgvector/pgvector:0.8.6-pg18@sha256:2ba9ca5f2e7daa0f0e7723cba1ee9167bab54efd3640516a44ac1a928dd67e7a` (the FV1 pin); `db` uses `image: *pg-image` and mounts `pgdata-pgvector:/var/lib/postgresql`; the volume list declares `pgdata-pgvector`, `prometheus-data`, `grafana-data` and no longer `pgdata`. The `db` comments now describe the plain image (no init script in `template1`, no auto-tuning, stock defaults) and say that `cairn_pgdata` is not mounted, reused or deleted (FU7).
- `mk/db.mk`: `DB_VOLUME ?= cairn_pgdata-pgvector`; new `db-reset-guard`, which only compares the name (no docker or compose call) and exits 1 for anything else; `db-reset` depends on it, so a wrong name stops before `compose down` or any deletion. `mk/stack.mk`: the comment names `cairn_pgdata-pgvector`.
- `tests/scripts/test_compose_config.py` (new): the rendered `db` image matches `pgvector/pgvector:<ver>-pg18@sha256:<64 hex>` with version at least 0.8.0; `db` mounts only `pgdata-pgvector` at `/var/lib/postgresql` (Docker name `cairn_pgdata-pgvector`); no service in `core` or `obs` declares or mounts `pgdata`; `make db-reset-guard` refuses `cairn_pgdata` and other names and accepts the new name, with a `docker` stub on `PATH` that proves no docker call happens; `db-reset` lists `db-reset-guard` as a prerequisite. `db-reset` itself is never run.
- `scripts/licenses.py` (outside the task's file list, needed to keep `make licenses` and `tests/scripts/test_licenses.py` passing): image references written as a compose anchor (`x-…: &name <ref>`) are read and `image: *alias` lines are skipped; `IMAGE_LICENSES` gains a `pgvector/pgvector` row (PostgreSQL License). The ParadeDB row and the rest of the report stay for task 10.1. One unit test added for the anchor case.

## Live switch

1. `make down` (no `--volumes`): removed `cairn-web-1`, `cairn-worker-1`, `cairn-api-1`, `cairn-db-init-1`, `cairn-db-1` and the network `cairn_default`. No other project was touched.
2. `make up-db`: compose created `cairn_pgdata-pgvector`; `cairn-db-1` became healthy on `pgvector/pgvector:0.8.6-pg18@sha256:2ba9ca5f…7e7a`, mount `cairn_pgdata-pgvector -> /var/lib/postgresql`.
3. Server facts: `PostgreSQL 18.6 (Debian 18.6-1.pgdg12+2) on aarch64-unknown-linux-gnu`; available extensions `vector` 0.8.6 and `btree_gist` 1.8, no `pg_search`; `shared_buffers` 128MB.
4. `cairn_pgdata` still exists and no container mounts it (`docker ps -q --filter volume=cairn_pgdata` is empty).

The api, worker and web stay down. `make db-init` is not run: until task 2.2 removes `pg_search` from bootstrap, it fails on the plain image by design.

## Not changed here (later tasks)

- `CONTRIBUTING.md` still says `db-reset` deletes `cairn_pgdata` (task 12.3 rewrites the make reference and troubleshooting).
- The S2 harness `image_reference()` still matches `paradedb` only (task 3.1).
- `docs/licenses.md` is not regenerated (task 10.1 runs `make licenses`).

## OQ-1 and FV3 item 7

The user's OQ-1 decision (option 1) and the plan for FV3 item 7 are recorded in [task-1.1-notes.md](task-1.1-notes.md) and [verify/FV3.md](verify/FV3.md), and carried into the retarget design (§2, OQ-1), Requirements 5.8, 5.9 and 5.11, and the bullets of tasks 4.3, 6.1 and 9.3.

## Verify

`make compose-check && uv run pytest tests/scripts/test_compose_config.py && ! make db-reset-guard DB_VOLUME=cairn_pgdata && make up-db && docker volume inspect cairn_pgdata-pgvector cairn_pgdata >/dev/null`

Result (2 Nov 2026): passed. compose-check ok for core, obs and experiments; 9 tests passed; the guard printed `db-reset-guard: refusing DB_VOLUME=cairn_pgdata; db-reset deletes only cairn_pgdata-pgvector` and exited 1; `db` healthy; both volumes exist. Also passed: `make fmt-check lint types`, `uv run pytest tests/scripts` (249 tests) and `scripts/check_docs.py --only links`.
