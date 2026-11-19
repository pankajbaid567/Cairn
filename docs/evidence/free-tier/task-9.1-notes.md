# Task 9.1: Render Blueprint

Done 3 Nov 2026. Requirements 6.2, 17.1, 17.2, 17.11.

## Files

- `render.yaml` (new): one web service `cairn-api`, `runtime: docker`, `plan: free`, `region: singapore`, `dockerfilePath: ./infra/docker/python-app.Dockerfile`, `dockerContext: .`, `dockerCommand: cairn-combined`, `healthCheckPath: /healthz`, `autoDeployTrigger: "off"`.
- Environment: non-secret values `CAIRN_ENV=demo`, `CAIRN_LOG_LEVEL`, `CAIRN_DB_PORT`, `CAIRN_DB_NAME`, `CAIRN_DB_SSLMODE=verify-full`, `CAIRN_DB_SSLROOTCERT=/etc/ssl/certs/ca-certificates.crt` (FV11, not `system`; same value as `.env.neon.example`), `CAIRN_DB_TIMEOUT_S`, `CAIRN_HEARTBEAT_CRON=0 */5 * * * *`, `OTEL_TRACES_EXPORTER=none`, `CAIRN_WORKERS_AI_BUDGET_STORE=postgres`, `CAIRN_WORKERS_AI_DAILY_NEURONS=9000`. Entered by the user (`sync: false`): `CAIRN_DB_HOST`, `CAIRN_APP_DB_PASSWORD`, `CAIRN_DBOS_DB_PASSWORD`, `CAIRN_PROXY_TOKEN`, `CLOUDFLARE_ACCOUNT_ID`, `CLOUDFLARE_API_TOKEN`. No admin, owner or superuser key.
- `tests/scripts/test_render_blueprint.py` (new, 11 tests).
- `docs/evidence/free-tier/verify/FV6.md` (new) and the index rows in `README.md`.

## Deviations from design §14

- `autoDeployTrigger: "off"` instead of `autoDeploy: false` (the latter is deprecated per Render's Blueprint reference). Quoted because unquoted `off` is a YAML 1.1 boolean.
- Block-style `envVars` with comments instead of flow-style lines.
- `docs/deploy.md` (task 9.4) should say that a `sync: false` key added to the Blueprint after the first creation has to be typed into the dashboard by hand (FV6).

## Open for the user

FV6 could not settle whether Render's health probes count as inbound traffic for the 15-minute sleep. FU5 has a 20-minute idle check; the fallback if the service never sleeps is described in FV6.

## Verify

- `uv run pytest tests/scripts/test_render_blueprint.py`: passed.
- `make secrets`: no leaks found.
- `make fmt-check lint types`: see the final report.
- `CombinedSettings` accepts the Blueprint values (scratch process, dummy secrets, nothing printed).

No git, no volume `cairn_pgdata`, no profile started, no Neon/Render/Cloudflare call, `.env` untouched. Files owned by tasks 8.1 and 10.1 were not edited.
