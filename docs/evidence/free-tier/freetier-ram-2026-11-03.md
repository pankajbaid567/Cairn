# Free-tier RAM check, 2026-11-03

**PASS: every budget rule holds.**

`ft-app` (the combined process, limit 512 MiB, 0.1 CPU) measured with `docker stats --no-stream` (cgroup usage minus the inactive file cache): an idle phase of 60 s and a load phase of 60 s, a sample every 2 s. Budgets are FT-18 (Proposed target).

## Rules

| Rule | Budget | Measured | Result |
|---|---:|---:|---|
| Idle peak (MiB) | 300 | 91.64 | ok |
| Load peak (MiB) | 384 | 92.02 | ok |
| Failed load requests | at most 2% | 0 of 252 (0.0%) | ok |
| OOM-killed | no | no | ok |
| Restart count | 0 | 0 | ok |

## Memory (MiB)

| Phase | Samples | Failed samples | Mean | Peak |
|---|---:|---:|---:|---:|
| idle | 30 | 0 | 79.01 | 91.64 |
| load | 30 | 0 | 80.04 | 92.02 |

Headroom to the 512 MiB limit at the load peak: 420.0 MiB.

## Load

2 clients, each pausing 0.2 s after a request, request timeout 60 s. Latencies are in milliseconds and cover requests that got an HTTP response; they are for information, not a rule.

| Path | Expected | Requests | Errors | p50 | p95 | Max |
|---|---:|---:|---:|---:|---:|---:|
| `/readyz` | 200 | 84 | 0 | 181.5 | 2,168.8 | 3,197.9 |
| `/healthz` | 200 | 84 | 0 | 9.5 | 699.1 | 1,200.9 |
| `/cairn-ram-check-unknown` | 404 | 84 | 0 | 12.3 | 1,394.4 | 5,582.2 |
| **All** | | **252** | **0** | 58.2 | 1,394.4 | 5,582.2 |

Status codes: 200 x 168, 404 x 84. Requests without a response: none.

## Container

- Name: cairn-ft-app-1; state at the end: running (exit code 0)
- Image: `cairn-combined:local` (`sha256:6c359524311c6d74f3d298325c413591d843b7d885a70c4312f8d5d7889576f7`)
- Limits: memory 512.0 MiB, CPU 0.1
- cgroup at the end (read once, not in the verdict): `memory.peak` since start 92.6 MiB (includes start-up), anon 82.1 MiB, file 0.0 MiB, shmem 0.0 MiB
- Docker VM memory: 3,919.3 MiB; server 28.5.1
- Started: 2026-11-03T14:21:42Z

## Samples: idle (MiB)

| t (s) | MiB |
|---:|---:|
| 0.0 | 76.05 |
| 2.0 | 76.07 |
| 4.0 | 76.37 |
| 6.0 | 76.67 |
| 8.0 | 76.43 |
| 10.0 | 76.46 |
| 12.0 | 76.46 |
| 14.0 | 76.48 |
| 16.0 | 76.50 |
| 18.0 | 76.52 |
| 20.0 | 76.52 |
| 22.0 | 76.52 |
| 24.0 | 76.53 |
| 26.0 | 76.52 |
| 28.1 | 78.00 |
| 30.1 | 85.04 |
| 32.1 | 87.57 |
| 34.2 | 88.45 |
| 36.2 | 88.48 |
| 38.2 | 91.64 |
| 40.3 | 91.52 |
| 42.3 | 76.59 |
| 44.4 | 76.60 |
| 46.4 | 76.60 |
| 48.4 | 76.60 |
| 50.4 | 76.61 |
| 52.5 | 76.61 |
| 54.6 | 76.60 |
| 56.6 | 76.60 |
| 58.7 | 76.61 |

## Samples: load (MiB)

| t (s) | MiB |
|---:|---:|
| 0.0 | 76.88 |
| 2.0 | 76.67 |
| 4.0 | 76.70 |
| 6.0 | 77.21 |
| 8.1 | 76.96 |
| 10.1 | 76.96 |
| 12.1 | 83.98 |
| 14.1 | 83.81 |
| 16.1 | 85.43 |
| 18.1 | 87.87 |
| 20.2 | 88.74 |
| 22.2 | 88.83 |
| 24.2 | 91.61 |
| 26.3 | 92.02 |
| 28.3 | 76.80 |
| 30.3 | 76.83 |
| 32.3 | 77.29 |
| 34.4 | 76.81 |
| 36.4 | 76.80 |
| 38.4 | 76.80 |
| 40.5 | 77.13 |
| 42.5 | 76.89 |
| 44.7 | 76.88 |
| 47.1 | 76.89 |
| 49.1 | 77.39 |
| 51.1 | 76.89 |
| 53.2 | 76.89 |
| 55.2 | 77.14 |
| 57.2 | 77.43 |
| 59.2 | 82.57 |
