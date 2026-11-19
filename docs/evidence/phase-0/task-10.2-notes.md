# Task 10.2 notes: obs integration check
Requirements 9.8, 9.9. No git command run. `cairn-db-1` was not touched (start time `2026-10-30T17:45:13Z` before and after; `config --hash db` equaled its label before `up-obs`).

## Files
| File | Change |
|---|---|
| `tests/obs/test_obs_profile.py` | New. Marker `obs`. Skips when the collector container is not running; fails when it runs but the api has no `OTEL_TRACES_EXPORTER=otlp` |
| `pyproject.toml` | Registered marker `obs` |
| `mk/python.mk` | `test-unit` now `-m "not integration and not smoke and not obs"` |
| `mk/obs.mk` | `obs-check: up-obs` runs `pytest -m obs tests/obs` |
| `CONTRIBUTING.md` | `obs-check` in the target table and reference row; `obs` in the marker list |

## What it checks
1. One `/readyz?probe=<uuid>` call; the collector log (`docker logs`) contains the uuid within 90 s.
2. Prometheus `traces_span_metrics_calls_total{service_name="cairn-api"}` has a series with `span_name` containing `/readyz` and a value above 0 within 90 s.
3. Grafana `/api/datasources` (admin, password from env or `.env`, never printed) lists the `prometheus` data source `cairn-prometheus`. Skips when no password is available.

## Result
`make obs-check` on the live stack: 3 passed (34 s). A first run failed because the test asserted every `cairn-api` series was above 0 at the first poll; the check now selects the `/readyz` series first. Afterwards `make down-obs` and `make up` restored the default (`OTEL_TRACES_EXPORTER=none` on api; obs containers gone; volumes kept).
`make fmt-check lint types test-unit docs-check`: exit 0, 923 passed, 49 deselected, 1 xfailed (known canary gap).
