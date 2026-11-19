# Task 10.1 notes: obs profile services and `make up-obs`
Date: 30 Oct 2026 (host clock). Task resumed after Docker Desktop quit during the first run. No git command run against this repo. No volume deleted, `cairn-db-1` never stopped, removed or recreated (see "Safety checks").
## Files
| File | State |
|---|---|
| `docker-compose.yml` | `otel-collector`, `prometheus`, `grafana` (profile `obs`); `OTEL_TRACES_EXPORTER` and `OTEL_EXPORTER_OTLP_ENDPOINT` on `api` and `worker` |
| `infra/otel/Dockerfile`, `infra/otel/collector.yaml` | Collector image with a static busybox for the healthcheck; pipeline config |
| `infra/prometheus/prometheus.yml` | Scrapes itself and `otel-collector:8889` |
| `infra/grafana/provisioning/{datasources,dashboards}/` | Prometheus data source (uid `cairn-prometheus`, read-only, default) and the dashboard "Cairn RED (span metrics)" |
| `mk/obs.mk` | `up-obs`, `down-obs`, `logs-obs` |
| `scripts/licenses.py` | `IMAGE_LICENSES` rows: `grafana/grafana`, `grafana/grafana-oss`, `otel/opentelemetry-collector-contrib`, `prom/prometheus`, `busybox` |
| `CONTRIBUTING.md` | Target row for `up-obs`/`down-obs`/`logs-obs`; new Troubleshooting section "Observability profile" (added in this resumed run; the row already referred to it) |
Most of this existed from the interrupted run. This run inspected each piece, verified it against the live stack, and added the Troubleshooting section and this note.
## Images (tag and digest)
| Service | Image | Digest resolves in registry |
|---|---|---|
| otel-collector | `otel/opentelemetry-collector-contrib:0.164.0@sha256:340c9ca6...fddc522b`, plus `busybox:1.37.0-musl@sha256:5cec3fc1...5a0092` (derived image `cairn-otel-collector:local`) | yes (`docker buildx imagetools inspect`) |
| prometheus | `prom/prometheus:v3.15.1@sha256:fa330227...f191a2` | yes |
| grafana | `grafana/grafana:13.2.5@sha256:9f7aa1e1...268a84` | yes |
Local `docker image ls --digests` shows the same digests for the pulled images.
Deviations from design §4:
- Grafana image is `grafana/grafana`, not `grafana/grafana-oss` (same OSS edition; `-oss` stopped at 13.0.2). Both have a license row.
- The collector gets a derived image, because the upstream image has no shell or wget and Requirement 4.4 wants a healthcheck. The healthcheck runs `/busybox wget` against the `health_check` extension on 127.0.0.1:13133.
- Prometheus and Grafana use the image's `wget`, as designed.
## Settings checked
| Item | Value |
|---|---|
| Host ports (`docker ps`) | `127.0.0.1:4318`, `127.0.0.1:9090`, `127.0.0.1:3000`; nothing else published by obs |
| Memory limits | collector 256m, prometheus 384m, grafana 256m (sum 896m; core 1856m) |
| Grafana | anonymous access off (`/api/frontend/settings` reports `anonymousEnabled: false`; unauthenticated `/api/datasources` returns 401); admin password from `GRAFANA_ADMIN_PASSWORD` (login worked; value not printed); sign-up off; plugin preinstall off |
| Containers | `read_only`, `cap_drop: ALL`, `no-new-privileges`; json-file logs rotated 2 x 5 MB |
| Default core behavior | `OTEL_TRACES_EXPORTER` defaults to `none` in compose; only `make up-obs` sets `otlp` |
## Functional check (live stack, after `make up-obs`)
- `make up-obs` exit 0 in 31 s on an already-running stack; all seven services healthy. Only api, worker, web and the collector were recreated; db kept its start time (`2026-10-30T17:45:13Z`).
- `GET /readyz` returned 200. The collector log shows the span (`http.target: /readyz`, `http.status_code: 200`).
- Prometheus targets: `prometheus` up, `otel-collector` up.
- Prometheus `traces_span_metrics_calls_total` had series for `service_name` `cairn-api` (spans `GET /readyz`, `SELECT`, `GET /readyz http send`) and `cairn-worker` (`worker.heartbeat`).
- Grafana `/api/ds/query` through the provisioned data source returned `sum by (service_name) (traces_span_metrics_calls_total)` = 156 for `cairn-api` and 179 for `cairn-worker`. `/api/search` lists the folder "Cairn" and the dashboard "Cairn RED (span metrics)".
- Not done: task 10.2 (the integration check) is a separate task; no test file added here.
## Measured memory (docker stats, `--no-stream`, idle stack, host with Docker Desktop)
Working set as `docker stats` reports it (usage minus inactive file cache), then the cgroup split from `memory.stat`.
| Container | Limit | `docker stats` | anon | file | `memory.peak` | OOM killed | limit hits (`memory.events max`) |
|---|---|---|---|---|---|---|---|
| otel-collector | 256m | 105 to 253 MiB (varies with how much of the binary is mapped); 52 MiB right after a restart | 47 MiB | 5 to 205 MiB | 54 MiB after restart; 256 MiB before it | no | 0 |
| prometheus | 384m | 127 to 153 MiB | 54 MiB | 72 MiB | 159 MiB | no | 0 |
| grafana | 256m | 177 to 243 MiB | 137 to 141 MiB | 106 to 109 MiB | 256 MiB | no | 1415 |
| Sum of anon | 896m | | about 240 MiB | | | | |
For comparison, core at the same time: db 115 MiB, api 54 MiB, worker 69 MiB, web 3 MiB (about 240 MiB; their combined limits are 1856m).
Reading:
- No obs container was OOM-killed or restarted (`RestartCount` 0, `OOMKilled` false) across the whole observation, including several Grafana queries.
- The collector's `docker stats` figure reached 252 MiB of 256 MiB for minutes, but only 47 MiB of it was anonymous memory; the rest was the memory-mapped contrib binary (about 200 MB). Those pages are reclaimable, `memory.events` shows 0 limit hits, and after the container was restarted it sat at 52 MiB. So the 256m limit is adequate, with the `memory_limiter` (192 MiB) acting on the Go heap well below it.
- Grafana's file cache and anon memory together touch its 256m limit: the kernel reclaimed pages 1415 times at the limit and nothing was killed. Anon alone is about 140 MiB. The earlier run measured about 220 MB at startup with all bundled data-source plugins enabled, which is why `GF_PLUGINS_DISABLE_PLUGINS` lists the unused ones. Leave 256m, and raise it first if Grafana ever restarts under load.
- Total working set of obs is below the 0.9 GB design budget; with core the stack is far inside the 6 GB ceiling.
- Source of the Grafana startup times quoted in the compose comment (about 25 s idle, over 2 minutes loaded, first start on an empty volume): the interrupted first run. They were not re-measured, because that needs an empty `grafana-data` volume and no volume may be deleted.
## Safety checks
- Before every `up`: `docker compose --profile core config --hash db` equaled the `com.docker.compose.config-hash` label of `cairn-db-1` (`6e9b0dfb...afb6d`). Also true for all seven services with `OTEL_TRACES_EXPORTER=otlp` before the run.
- `cairn-db-1`, `taskflow-*` and `openwa-api` were not stopped, removed or recreated. No `down --volumes`, no volume deleted (`cairn_pgdata`, `cairn_prometheus-data`, `cairn_grafana-data` still exist).
- `make gates` not run. `ci.yml` and `mk/gates.mk` not touched.
## Final checks
`make fmt-check lint types test-unit compose-check licenses docs-check` ran in one call, exit 0:
- `test-unit`: 862 passed, 1 xfailed (the known canary gap from 12.2), 46 deselected.
- `compose-check`: profiles core, obs and experiments ok.
- `licenses`: ok, 6 transitive web packages listed for review (MIT-0, CC-BY-4.0, BlueOak-1.0.0, CC0-1.0); none is a direct dependency. `docs/licenses.md` was regenerated.
- `docs-check`: ok.
## End state
`make down-obs` removed the three obs containers (volumes kept). `make up` then recreated api and worker with `OTEL_TRACES_EXPORTER=none` (confirmed in their environment), so core is back to its default. Running: cairn-db-1 (untouched, up since before this run), api, worker, web. Not running: otel-collector, prometheus, grafana.
