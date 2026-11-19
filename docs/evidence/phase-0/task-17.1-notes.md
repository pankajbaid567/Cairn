# Task 17.1 notes: core profile RAM report (`make ram-report`)

Date: 30 Oct 2026 (host clock). No dependency, lockfile, compose file, Dockerfile or image change; `IMAGE_LICENSES` needed no row. No git command run. `make gates` and `make phase0-exit` not run. `cairn-db-1` was not stopped, removed or recreated (start time `2026-10-30T17:45:13Z` before and after both runs; `docker compose --profile core config --hash db` equaled its `com.docker.compose.config-hash` label before the first run). Other projects' containers (`openwa-api`) were not touched.

## Files

| File | State |
|---|---|
| `scripts/measure_ram.py` | New. Standard library only. Pure parsing, summarizing and rendering functions; `main` and the `docker` helpers shell out |
| `mk/ram.mk` | New: `ram-report` (`RAM_ARGS` passes options; `--build` unless `UP_BUILD=` is empty) |
| `tests/scripts/test_measure_ram.py` | New: 44 tests (recorded `docker stats` and `memory.stat` lines, summary maths, the 6 GiB boundary, rendering, exit codes) |
| `docs/evidence/phase-0/core-ram-2026-10-30.md`, `.json` | The report from the live run |
| `CONTRIBUTING.md` | `ram-report` added to the target list and the target reference |

## What the tool does

1. Reads Docker's VM memory (`docker info --format '{{.MemTotal}}'`) and host memory (`sysctl -n hw.memsize`).
2. `docker compose --profile core up --wait [--build]`. On an up-to-date stack this changes nothing.
3. Finds the running db, api, worker and web containers by compose labels (project `cairn`). Fails with exit 2 when one is missing. Other running containers are listed in the report and not counted.
4. Warm-up of 60 s: 20 calls to `http://127.0.0.1:8000/readyz` over the first 20 s, then waiting; counts worker log lines that mention a heartbeat.
5. 12 samples, 5 s apart (60 s). Each sample is one `docker stats --no-stream --format '{{json .}}'` plus `docker exec <c> cat /sys/fs/cgroup/memory.stat` per container.
6. Writes the Markdown and JSON reports. Exit 0 when the total peak is at most 6 GiB, 1 when it is above, 2 when Docker or the stack is not usable.

Definitions:

- **Total peak** is the highest per-sample sum of the `docker stats` figures, not the sum of per-container peaks.
- **anon + shmem** is reported next to it. Postgres shared buffers are `shmem`, which cgroup v2 counts inside `file`, so `shmem` is added to `anon`. This is the memory the kernel cannot drop, as the 10.1 notes found for the obs containers.
- Thresholds use GiB (1024^3), the unit Docker prints: ceiling 6 GiB, estimate 4 GiB. "Exceeds" is strictly above.

## Result (run at 18:03 UTC, core profile idle, exporter `none`)

| | Value |
|---|---|
| Host memory | 8.00 GiB (8,589,934,592 B) |
| Docker VM memory | 3.83 GiB (4,109,672,448 B, about 4.1 GB), 48% of the host |
| Sum of container limits | 1,856 MiB (1.81 GiB) |
| Total peak, `docker stats` | 229.7 MiB (0.22 GiB), 5% of the 4 GiB estimate, 4% of the 6 GiB ceiling |
| Total mean | 229.4 MiB |
| Total peak, anon + shmem | 167.6 MiB |
| Verdict | PASS, exit 0 |

| Container | Limit | Mean | Peak | anon | file | shmem |
|---|---:|---:|---:|---:|---:|---:|
| cairn-db-1 | 1,024 MiB | 115.0 | 115.1 | 21.7 | 105.4 | 36.9 |
| cairn-api-1 | 384 MiB | 50.1 | 50.3 | 47.8 | 0.0 | 0.0 |
| cairn-worker-1 | 384 MiB | 61.7 | 61.9 | 59.2 | 0.0 | 0.0 |
| cairn-web-1 | 64 MiB | 2.5 | 2.6 | 1.9 | 0.0 | 0.0 |

All 20 `/readyz` calls returned 200, and the worker logged 2 heartbeats during the warm-up. The first run (same stack, a minute earlier, api just rebuilt) gave a total peak of 248 MiB; the report on disk is the second run.

Reading:

- The Phase 0 core profile has no workload, so it sits far below the "about 4 GB" estimate, which covers later phases. The result shows headroom for Phase 1, not a prediction of it. Rerun after the first ingestion and retrieval load.
- The Docker VM (3.83 GiB) is smaller than the 6 GiB ceiling, so the VM would run out before the ceiling does. The report says so. The VM also holds its own overhead, which the container figures exclude.
- `openwa-api` (about 220 MiB) shares the Docker VM and is not part of the Cairn total.
- `memory.peak` since container start is in the report for each container (db 144 MiB, from start-up and migrations).

## Verification

| Check | Result |
|---|---|
| `uv run pytest tests/scripts/test_measure_ram.py` | 44 passed |
| `make ram-report` | exit 0, twice; both runs on the live stack |
| `make fmt-check lint types test-unit docs-check` | pass: 906 passed, 1 xfailed (the known canary gap from 12.2), 46 deselected; `docs-check: ok` |

## For later tasks

- 17.2 (`phase0-exit`): cite `make ram-report` and `docs/evidence/phase-0/core-ram-<date>.md` as the proof for the RAM line of the exit gate. It is not part of `make gates`, because it adds about 2.5 minutes of waiting.
