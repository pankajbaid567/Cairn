# Task 6.3: the free-tier RAM check and its first run
Date: 3 Nov 2026 (host clock; the run started 2 Nov 21:18 UTC). Requirements 12.1 to 12.5; decision FT-18; design §10. No dependency was added; `.env` was not touched. The core stack was stopped (`make down`) for the freetier run and is up again (`make up`: db, api, worker, web healthy; `make db-status`: head 1, pending 0).

**Result: PASS.** Idle peak 94.56 MiB (budget 300), load peak 95.00 MiB (budget 384), 0 of 288 load requests failed (budget 2%), not OOM-killed, restart count 0. Evidence: [freetier-ram-2026-11-03.md](freetier-ram-2026-11-03.md) and [freetier-ram-2026-11-03.json](freetier-ram-2026-11-03.json). No budget was changed.

## Files
| File | Change |
|---|---|
| `scripts/freetier_ram.py` | New, standard library only. Reuses `parse_stats_output`, `parse_memory_stat`, `parse_compose_ps`, `MIB`, `RamReportError` and `_run` from `scripts/measure_ram.py` |
| `tests/scripts/test_freetier_ram.py` | New, 52 tests |
| `mk/freetier.mk` | `freetier-ram` runs the script (`FT_RAM_ARGS` passes options); header comment rewritten |
| `CONTRIBUTING.md` | The `freetier-*` row describes `freetier-ram` |
| `docs/evidence/free-tier/freetier-ram-2026-11-03.{md,json}`, README rows | New |

## Names for task 6.4 (property test)
The design §10 names are used as written and import from `scripts.freetier_ram`:
```python
RamBudget(limit_mib=512.0, idle_peak_mib=300.0, load_peak_mib=384.0, max_error_ratio=0.02)   # frozen dataclass
Phase(name: Literal["idle", "load"], samples_mib: tuple[float, ...], requests: int = 0, errors: int = 0, latencies_ms: tuple[float, ...] = ())
verdict(budget, idle, load, oom_killed, restarts) -> tuple[bool, list[str]]
```
`verdict` is exactly the rule set of the design and nothing more: pass iff both sample tuples are non-empty, `max(idle) <= idle_peak_mib`, `max(load) <= load_peak_mib`, `errors <= max_error_ratio * requests` (this expression, so 0 requests with 0 errors passes), `not oom_killed` and `restarts == 0`. The reasons are one string per failed rule, in the order idle samples or peak, load samples or peak, errors, OOM, restarts. Two further run-level failures (ft-app not running at the end, no load request sent) are added by `evaluate()` in `main`, not by `verdict`, so an oracle that models only the design's rules matches `verdict` exactly. Also importable: `percentile` (nearest rank).

## How the script works
| Step | What it does |
|---|---|
| Settings | Token from the environment, then `.env` (`CAIRN_PROXY_TOKEN`); port from `CAIRN_FREETIER_APP_PORT` (default 8010). A missing token exits 2 before Docker is touched, naming the key only |
| Docker | `docker info` (exit 2 if it fails); `docker ps` for project `cairn`; any running container whose service does not start with `ft-` makes it exit 2 with "Core and freetier never run together ... run `make down` first" and nothing is started |
| Bring-up | `docker compose --profile freetier up --wait` (30-minute cap; `--build` only with `--build`; skipped with `--no-up`). This also restarts an `ft-app` that the smoke module stopped. Then polls `docker inspect` every 5 s, up to 600 s, until `Health.Status` is `healthy`; `unhealthy`, not running or timeout exit 2 |
| Idle | 30 samples, one per 2 s, each `docker stats --no-stream` of `ft-app` (usage in MiB). Each sample is due at `begin + i*2 s`; a sample that fails is counted and skipped |
| Load | Two daemon threads start at the same time as the load-phase sampler. Each cycles `/readyz` (header, expects 200), `/healthz` (no header, 200) and `/cairn-ram-check-unknown` (header, 404), offset by one between the clients, pausing 0.2 s after each request; 60 s request timeout. A request is a failure when the status differs from the expected one or there was no response (the exception class name is kept, never the message) |
| End | `docker inspect` (`OOMKilled`, `RestartCount`, state), then once `memory.peak` and `memory.stat` through `docker exec` for the report (not in the verdict) |
| Output | `freetier-ram-<date>.md` and `.json`, also when a budget is missed. Exit 0 pass, 1 budget missed, 2 unusable. `--date`, `--out-dir`, `--env-file`, `--idle-s`, `--load-s`, `--interval-s`, `--pause-s`, `--clients`, `--request-timeout-s`, `--health-timeout-s`, `--no-up`, `--build` |

The measured figure is the `docker stats` `MemUsage` (cgroup usage minus the inactive file cache), the same figure `measure_ram.py` uses. Unlike `measure_ram.py` it does not call `docker exec` per sample: at 0.1 CPU an exec shares the quota with the app (FV13: 16 s for the healthcheck).

## The tests
52 tests, about 5 s. Verdict: just below, on and just above 300 and 384 MiB, 0 to 3 errors in 100 requests and 1 or 2 in 50, OOM, restarts 1 and 3, empty idle and empty load samples, all five reasons at once. Helpers: `percentile`, the `.env` reader, `docker inspect` parsing (and four unreadable inputs), core-container refusal. Sampler with a fake clock (slow sample, failure skipped, at least one sample). Load with a recording fetch: two clients, three paths, header only where needed, errors counted by class name. `http_get` against a real loopback `http.server` (200, 404, header) and its refusal of non-loopback URLs. `main` with a fake docker runner and fake fetch: pass (files, fields, port from `.env`, `compose up` called), `--no-up`, idle peak 301 gives 1 with evidence written, OOM 1, restart 1, 500 responses 1, container gone at the end 1, Docker down 2 (no files), failing `compose up` 2, no `ft-app` 2, unhealthy 2, core containers 2 with no `compose` or `stats` call, missing token 2 with no Docker call. Token: a fetch that raises `OSError` with the headers in its message, then the captured stdout and stderr and both evidence files contain neither the token nor its first 12 characters; a token from the environment is sent but not printed.

## The first real run
Taken on the 8 GB host with the core stack stopped, after `make freetier-up` (ft-app healthy 127.6 s after the start) and then `make freetier-ram`, which found everything up and went straight to sampling.

| | Budget | Measured |
|---|---:|---:|
| Idle peak (30 samples, 0 failed) | 300 MiB | 94.56 MiB (mean 83.01) |
| Load peak (30 samples, 0 failed) | 384 MiB | 95.00 MiB (mean 82.68) |
| Failed load requests | at most 2% | 0 of 288 (192 x 200, 96 x 404) |
| OOM-killed / restarts | no / 0 | no / 0 |

Headroom at the load peak: 417 MiB of the 512 MiB limit. Latencies (information only): `/readyz` p50 168 ms, p95 2,097 ms, max 4,200 ms; `/healthz` p50 10 ms, p95 595 ms; unknown path p50 10 ms. cgroup at the end: `memory.peak` since start 102.8 MiB (includes start-up), anon 76.3 MiB, file 7.3 MiB, shmem 0.

Observations:
- **The load did not move memory.** Baseline is 79.5 MiB; the load phase peaked at 95.0 MiB, the same as the idle peak. The only excursions (to 94.6 MiB idle, 95.0 MiB under load) are 8 to 12 s climbs and drops that recur about every 30 s, at 4 to 12 s and 44 to 52 s of the idle phase and 24 to 38 s of the load phase. That matches the 30 s compose healthcheck, which starts a second Python process in the container's cgroup (about 15 MiB; it takes about 16 s at 0.1 CPU). I did not isolate it further, so this is consistent with the data, not proven. The peaks therefore include a health check, as they would on Render.
- **Only the 60 s windows are measured.** Memory over hours (leaks, DBOS history) is not covered; Phase 1 adds code, which is what the 25% headroom in FT-18 is for.
- The 30 + 30 samples are in the `.md` and `.json`.

## Verify results
```text
$ uv run pytest tests/scripts/test_freetier_ram.py
52 passed
$ make down && make freetier-up && make freetier-ram && make freetier-down
freetier-up: ft-db healthy 14.6 s, both init containers exited 0, ft-app healthy 127.6 s
freetier-ram: exit 0, PASS (numbers above); freetier-down removed the four containers, both volumes still present
$ make fmt-check lint types test-unit docs-check
all pass: 1359 passed, 66 deselected, 1 xfailed (the canary xfail); docs-check ok
$ make up && make db-status
core healthy again; head 1, pending 0
```
Not run: `make freetier-check` end to end (task 6.5 wires it into the gates). In that sequence the smoke module stops `ft-app` first, so `freetier-ram` will spend about 2.5 minutes on `up --wait` before sampling.

`cairn_pgdata` and `cairn_pgdata-pgvector` were present before and after; no `--volumes`; `openwa-api` was not touched; no git command was run.

## For later tasks
- **6.4:** see "Names for task 6.4".
- **6.5:** `phase0_exit.py` can read `freetier-ram-<date>.json`: `result` is `pass` or `fail`, `reasons` is a list, `budget` and `idle.peak_mib`, `load.peak_mib` are numbers. The newest file by date is `freetier-ram-2026-11-03.json` (the date is the host's local date).
- The script has no per-test marker; `tests/scripts/test_freetier_ram.py` needs no Docker and runs in `test-unit`.
