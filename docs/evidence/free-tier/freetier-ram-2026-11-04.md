# Free-tier RAM check, 2026-11-04

**PASS: every budget rule holds.**

`ft-app` (the combined process, limit 512 MiB, 0.1 CPU) measured with `docker stats --no-stream` (cgroup usage minus the inactive file cache): an idle phase of 60 s and a load phase of 60 s, a sample every 2 s. Budgets are FT-18 (Proposed target).

## Rules

| Rule | Budget | Measured | Result |
|---|---:|---:|---|
| Idle peak (MiB) | 300 | 96.50 | ok |
| Load peak (MiB) | 384 | 101.30 | ok |
| Failed load requests | at most 2% | 2 of 163 (1.2%) | ok |
| OOM-killed | no | no | ok |
| Restart count | 0 | 0 | ok |

## Memory (MiB)

| Phase | Samples | Failed samples | Mean | Peak |
|---|---:|---:|---:|---:|
| idle | 30 | 0 | 86.46 | 96.50 |
| load | 30 | 0 | 90.29 | 101.30 |

Headroom to the 512 MiB limit at the load peak: 410.7 MiB.

## Load

2 clients, each pausing 0.2 s after a request, request timeout 60 s. Latencies are in milliseconds and cover requests that got an HTTP response; they are for information, not a rule.

| Path | Expected | Requests | Errors | p50 | p95 | Max |
|---|---:|---:|---:|---:|---:|---:|
| `/readyz` | 200 | 55 | 2 | 496.1 | 5,687.6 | 10,078.6 |
| `/healthz` | 200 | 54 | 0 | 86.6 | 1,197.7 | 1,297.5 |
| `/cairn-ram-check-unknown` | 404 | 54 | 0 | 89.9 | 2,293.9 | 4,795.0 |
| **All** | | **163** | **2** | 164.2 | 2,293.9 | 10,078.6 |

Status codes: 200 x 107, 404 x 54, 503 x 2. Requests without a response: none.

## Container

- Name: cairn-ft-app-1; state at the end: running (exit code 0)
- Image: `cairn-combined:local` (`sha256:97c1f877786d5663e358af5aab5e639ee788b85bc76adb8f2c9064933ed75ae5`)
- Limits: memory 512.0 MiB, CPU 0.1
- cgroup at the end (read once, not in the verdict): `memory.peak` since start 102.9 MiB (includes start-up), anon 93.2 MiB, file 0.0 MiB, shmem 0.0 MiB
- Docker VM memory: 3,919.3 MiB; server 28.5.1
- Started: 2026-11-04T16:58:12Z

## Samples: idle (MiB)

| t (s) | MiB |
|---:|---:|
| 0.0 | 85.71 |
| 2.0 | 85.80 |
| 4.6 | 85.52 |
| 6.6 | 85.51 |
| 8.7 | 85.54 |
| 10.7 | 85.51 |
| 12.7 | 85.53 |
| 14.8 | 85.55 |
| 16.8 | 85.54 |
| 18.8 | 85.54 |
| 20.9 | 85.54 |
| 22.9 | 85.55 |
| 24.9 | 85.54 |
| 27.0 | 85.55 |
| 29.0 | 92.54 |
| 31.0 | 94.31 |
| 33.0 | 96.50 |
| 35.1 | 85.57 |
| 37.1 | 85.57 |
| 39.2 | 85.57 |
| 41.2 | 85.57 |
| 43.2 | 85.57 |
| 45.2 | 85.57 |
| 47.3 | 85.56 |
| 49.3 | 85.57 |
| 51.3 | 85.57 |
| 53.4 | 85.57 |
| 55.4 | 85.57 |
| 57.5 | 85.82 |
| 59.5 | 85.58 |

## Samples: load (MiB)

| t (s) | MiB |
|---:|---:|
| 0.0 | 85.69 |
| 2.0 | 85.72 |
| 4.1 | 87.02 |
| 6.1 | 92.65 |
| 8.1 | 95.13 |
| 10.1 | 95.25 |
| 12.2 | 96.98 |
| 14.2 | 96.76 |
| 16.3 | 97.28 |
| 18.3 | 97.75 |
| 20.3 | 101.00 |
| 22.3 | 100.80 |
| 24.4 | 101.30 |
| 26.4 | 86.58 |
| 28.4 | 85.87 |
| 30.5 | 86.42 |
| 32.6 | 85.94 |
| 34.6 | 85.94 |
| 36.7 | 86.19 |
| 38.7 | 86.20 |
| 40.7 | 85.94 |
| 42.7 | 86.42 |
| 44.8 | 86.68 |
| 46.8 | 86.18 |
| 48.8 | 86.42 |
| 50.8 | 85.95 |
| 52.9 | 85.94 |
| 54.9 | 86.71 |
| 56.9 | 88.64 |
| 59.0 | 93.29 |
