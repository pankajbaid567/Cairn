# Task 6.1: the freetier services and the profile validation
Date: 2 Nov 2026. Requirements 6.3, 10.1 to 10.7; decisions FT-08 and FT-17; FV13 resolved in [verify/FV13.md](verify/FV13.md). No dependency was added; `.env` was not touched (it already had `CAIRN_FREETIER_*`, `CAIRN_MANAGED_ADMIN_PASSWORD` and `CAIRN_PROXY_TOKEN`). The core stack was stopped for the run and is up again.

## Files
| File | Change |
|---|---|
| `docker-compose.yml` | Services `ft-db`, `ft-admin-init`, `ft-db-init`, `ft-app` (profile `freetier`); the header comment lists the profile, its limits and the never-with-core rule |
| `mk/db.mk` | `compose-check` loops over `core obs experiments freetier`; `core` and `freetier` are rendered alone, the others together with `core` |
| `tests/scripts/test_compose_config.py` | 11 tests added (19 in the file) |
| `docs/evidence/free-tier/verify/FV13.md`, README rows | New |

## The services as built
| Service | Image | Notes |
|---|---|---|
| `ft-db` | `*pg-image` (same anchor as `db`) | tmpfs `/var/lib/postgresql:size=256m,mode=0700,uid=999,gid=999`, `mem_limit` 512m, `127.0.0.1:${CAIRN_FREETIER_DB_PORT:-55433}:5432`, TCP `pg_isready` healthcheck, `POSTGRES_PASSWORD` only; no volume, no restart policy |
| `ft-admin-init` | `cairn-python:local` (own `build`, `PACKAGE=cairn-core`) | `cairn-db emulate-managed-admin`, `CAIRN_ENV=ci`, env: host, port, `POSTGRES_PASSWORD`, `CAIRN_MANAGED_ADMIN_PASSWORD` and nothing else |
| `ft-db-init` | `cairn-python:local` (own `build`) | `cairn-db init`, no `--allow-plaintext-password`, `CAIRN_DB_ADMIN_USER=cairn_admin`, `CAIRN_DB_ADMIN_PASSWORD` from `CAIRN_MANAGED_ADMIN_PASSWORD`, `CAIRN_DB_ADMIN_MODE=managed`, the three role passwords, no `POSTGRES_PASSWORD`; log lines show "admin mode: managed" and SCRAM verifiers through libpq |
| `ft-app` | `cairn-combined:local` (`PACKAGE=cairn-combined`) | `mem_limit` and `memswap_limit` 512m, `cpus` 0.1, `PORT=10000`, read-only root, user 10001, `CAIRN_PROXY_TOKEN`, `cairn_app` and `cairn_dbos` passwords only, `CAIRN_HEARTBEAT_CRON=*/10 * * * * *`, `OTEL_TRACES_EXPORTER=none`, `127.0.0.1:${CAIRN_FREETIER_APP_PORT:-8010}:10000`, `stop_grace_period` 30s; no restart policy, so an OOM kill or crash stays visible to task 6.3 |

Deviations from the design §9 YAML:
1. **Healthcheck of `ft-app`: `interval 30s`, `timeout 60s`, `retries 6`, `start_period 180s`** (design: 10 s, 8 s, 12, 180 s). At 0.1 CPU one `python -m cairn_api.healthcheck` takes about 16 s because the exec'd process shares the CPU quota with the app; the design values failed every check. Details in FV13. `cairn_api.healthcheck` itself (5 s socket timeout) was not changed; it passed with these values.
2. `ft-admin-init` and `ft-db-init` each have their own `build` block for the tag `cairn-python:local`. Without it `up --build` on a clean checkout (CI) would find no image for them, because the tag is otherwise built only by core's `db-init`. Same Dockerfile and args as `db-init`, so the layers are cached.
3. `CAIRN_LOG_LEVEL`, `CAIRN_DB_NAME` and (for `ft-db-init`) `CAIRN_ENV` are passed with the same defaults as the core services; `ft-db-init` keeps the local `CAIRN_ENV` default (only `ft-app` is `demo` and only `ft-admin-init` is `ci`).
4. `mem_limit` 256m on the two one-shots (as `db-init`), which the design left unset.

## Verify results
```text
$ make compose-check
compose-check: profile core ok / obs ok / experiments ok / freetier ok
$ uv run pytest tests/scripts/test_compose_config.py
19 passed
$ make down && docker compose --profile freetier up --wait --build && docker compose --profile freetier down
ft-db healthy 11 s, ft-admin-init and ft-db-init exited 0, ft-app healthy 133 s; down removed all four, exit 0
$ make fmt-check lint types test-unit
All checks passed; mypy: no issues (30 strict + 114 base files); 1307 passed, 54 deselected, 1 xfailed (the canary xfail)
$ make up    (core back up: db, api, worker, web healthy; make db-status: head 1, pending 0)
```
`cairn_pgdata` and `cairn_pgdata-pgvector` were present before and after; no `--volumes` was used and `openwa-api` was not touched. The stack was `make down`-ed before the freetier run; the first `up` attempt of this session failed with `network not found` because I issued `down` and `up` at the same moment (see FV13).

## What the config tests cover
`ft-app`: memory and memory plus swap 512 MiB, `cpus` 0.1, `PORT` 10000, read-only, user 10001, image and build argument. Ports: `ft-db` and `ft-app` on 127.0.0.1 with the default host ports, the two init services publish nothing. `POSTGRES_PASSWORD` exactly on `ft-db` and `ft-admin-init`. `ft-app` environment has none of the four admin/owner/superuser password keys, and its password/token keys are exactly `CAIRN_APP_DB_PASSWORD`, `CAIRN_DBOS_DB_PASSWORD`, `CAIRN_PROXY_TOKEN`. `ft-db-init` command, user, mode, no flag, no superuser password; `ft-admin-init` command and `CAIRN_ENV=ci`; `ft-db` image equals the `db` image and its data is tmpfs; no freetier service mounts a volume; the profile has exactly the four services; `mk/db.mk` loop includes `freetier`. They render from `.env.example`, so they run in CI without `.env`.

## For later tasks
- 6.2: at 0.1 CPU everything inside `ft-app` is slow (FV13): `/healthz` took up to 14 s while health checks ran, `/readyz` about 1.4 s when idle. Use generous HTTP timeouts (60 s) in `tests/freetier`; the heartbeat cron is every 10 s, but under throttling the first SUCCESS row can come late, so poll rather than sleep three intervals. `docker compose --profile freetier stop ft-app` timing: uvicorn re-raises SIGTERM, so check elapsed time, not the exit status (task 5.1 notes). Environment keys for `docker inspect` checks are exactly those listed above.
- 6.2: `freetier-up` should be `docker compose --profile freetier up --wait --build`; it takes about 2.5 minutes on this host, mostly `ft-app` reaching healthy.
- 6.3: single samples showed `ft-app` 79 MiB and `ft-db` 129 MiB idle, CPU 6 to 10% of the 10% quota (the cgroup is throttled in about 89% of its periods).
- The Neon extension path (`CREATE EXTENSION` as `neondb_owner`) is not exercised here: `template1` already carries both extensions (task 4.3 notes).
