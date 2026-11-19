# Task 2.3: database and stack suites on the pgvector image, refreshed core RAM report

Date: 2 Nov 2026. Requirements 1.6, 2.1, 2.4, 2.5; plan items 0.16, 0.24.

Starting state (tasks 2.1 and 2.2): `cairn-db-1` on `pgvector/pgvector:0.8.6-pg18`, volume `cairn_pgdata-pgvector`, bootstrapped and at migration version 1; api, worker and web down.

## Change: S2 harness integration test marked as a strict xfail

The first `make test-integration` run failed one test, `packages/eval/tests/integration/test_s2_harness.py::test_harness_runs_the_matrix_and_default_deny_holds`, with `psycopg.errors.UndefinedObject: access method "bm25" does not exist` at `CREATE INDEX chunks_bm25_idx ON s2.chunks USING bm25 (id, body) …` (the `(id, body)` fallback of `create_bm25_index`). The cause is the image swap: the BM25 harness needs `pg_search`, which the plain pgvector image does not have. The spike database `cairn_spike_s2_<hex>` was still dropped (the server afterwards held only `cairn`, `cairn_dbos`, `postgres`, `template0`, `template1`).

Making the harness work on the new image is task 3.1 (full-text search rework). The minimal change here, in that test file only:

```python
@pytest.mark.xfail(
    raises=psycopg.errors.UndefinedObject,
    strict=True,
    reason="BM25 harness on the pgvector image: no bm25 access method until task 3.1",
)
```

with a comment marked TEMPORARY that names task 2.3 and task 3.1. `raises=` limits the expected failure to the missing access method, so any other error still fails the run; `strict=True` fails the run once the test passes again, so task 3.1 must remove the marker when it rewrites the test. No source file changed. The file already has a TEMPORARY entry for task 3.1 in `tests/scripts/test_no_paradedb.py`; the scan still passes (8 tests). No gate or script counts xfails.

No other test failed: nothing else assumed `pg_search`, the `paradedb` schema or ParadeDB auto-tuning.

## Runs

| Command | Result |
|---|---|
| `make up-db test-integration` | 39 passed, 1 xfailed (the S2 harness test above), 986 deselected |
| `make matrix` | 12 passed (`packages/core/tests/integration/test_roles.py`) |
| `make migrations-check` | first and second run exit 0; version 1, the second run applied nothing |
| `make smoke` | `make up` started api, worker and web (db-init exited 0); 6 passed in `tests/smoke/test_core_profile.py` |
| `make ram-report` | PASS, total peak 0.23 GiB of the 6.00 GiB ceiling; wrote `docs/evidence/phase-0/core-ram-2026-11-02.md` and `.json` |

## Server facts

- Image: `pgvector/pgvector:0.8.6-pg18@sha256:2ba9ca5f2e7daa0f0e7723cba1ee9167bab54efd3640516a44ac1a928dd67e7a` (container image id is the same digest).
- Mount: `cairn_pgdata-pgvector -> /var/lib/postgresql`.
- `PostgreSQL 18.6 (Debian 18.6-1.pgdg12+2) on aarch64-unknown-linux-gnu`; `shared_buffers` 128MB.
- Extensions in database `cairn`: `vector` 0.8.6, `btree_gist` 1.8, `plpgsql` 1.0.

`/readyz` (HTTP 200) on `127.0.0.1:8000`:

```json
{"status":"ready","checks":{"database":{"ok":true,"detail":{"latency_ms":29.5}},"postgres":{"ok":true,"detail":{"version":"18.6"}},"extensions":{"ok":true},"role":{"ok":true,"detail":{"name":"cairn_app"}},"schema":{"ok":true,"detail":{"version":1,"expected":1}}},"extensions":{"vector":"0.8.6","btree_gist":"1.8"}}
```

`/healthz` returned `{"status":"ok"}` (HTTP 200).

## Core RAM report

`docs/evidence/phase-0/core-ram-2026-11-02.md` is now the newest core report; its Images table names `pgvector/pgvector:0.8.6-pg18@sha256:2ba9ca5f…7e7a` for `cairn-db-1`. The Verify command below ran `make ram-report` a second time on the same day, which rewrote the same two files; the figures below are from that final run (started 2026-11-02T10:28:48Z, 20 of 20 `/readyz` calls returned 200 during warm-up; `openwa-api` was running and is listed, not counted).

| | 30 Oct 2026 (ParadeDB image) | 2 Nov 2026 (pgvector image) |
|---|---:|---:|
| Core total peak | 229.7 MiB (0.22 GiB) | 236.3 MiB (0.23 GiB) |
| `cairn-db-1` peak | 115.1 MiB | 121.7 MiB |
| `cairn-db-1` anon + shmem (peak) | 58.7 MiB | 75.0 MiB |
| `cairn-db-1` cgroup peak | 144.0 MiB | 230.9 MiB |

The `cairn-db-1` cgroup peak includes start-up and the integration, matrix, migrations-check and smoke runs against the same container since it started, so it is not comparable with the 30 Oct value, which came from a fresher container. The 30 Oct report stays unchanged.

## cairn_pgdata

`docker volume inspect cairn_pgdata` succeeds (created 2026-10-27T14:43:33Z); `docker ps -aq --filter volume=cairn_pgdata` is empty, so no container, running or stopped, mounts it.

## End state

`cairn-db-1`, `cairn-api-1`, `cairn-worker-1` and `cairn-web-1` are up and healthy. `openwa-api` was not touched.

## Verify

`make up-db test-integration matrix migrations-check && make smoke && make ram-report && docker volume inspect cairn_pgdata >/dev/null && [ -z "$(docker ps -q --filter volume=cairn_pgdata)" ]`

Result (2 Nov 2026): passed (exit 0). 39 passed and 1 xfailed; 12 passed; migrations-check ok at version 1; 6 smoke tests passed; ram-report PASS at 0.23 GiB; `cairn_pgdata` exists and is not mounted. Also passed: `make fmt-check lint` and `uv run pytest tests/scripts/test_no_paradedb.py`.
