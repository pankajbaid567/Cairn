# Free-tier retarget evidence

Evidence for the spec "Cairn free-tier retarget" (`.kiro/specs/cairn-free-tier-retarget/`). Task notes are named `task-<id>-notes.md`, Verify results `verify/FV<n>.md` (FV0 is `verify/FV0-platform-facts.md`), and RAM reports `freetier-ram-<date>.md`. Add a row here whenever a file is added.

## Task notes

| File | Task | Content |
|---|---|---|
| [task-1.1-notes.md](task-1.1-notes.md) | 1.1 | Docker container and volume names before the retarget (for task 14.1), `cairn_pgdata` check |
| [task-1.2-notes.md](task-1.2-notes.md) | 1.2 | New template keys, `make env-update`, appended key names |
| [task-2.1-notes.md](task-2.1-notes.md) | 2.1 | `db` on `pgvector/pgvector:0.8.6-pg18` and the volume `cairn_pgdata-pgvector`, `db-reset-guard`, the live switch |
| [task-2.2-notes.md](task-2.2-notes.md) | 2.2 | `pg_search` removed from bootstrap, readiness (`extensions_outdated`), the 0001 baseline (old and new checksums) and the tests; the scan `tests/scripts/test_no_paradedb.py`; `make db-init` on the new volume |
| [task-2.3-notes.md](task-2.3-notes.md) | 2.3 | Integration, matrix, migrations-check and smoke on the pgvector image; the S2 harness test as a strict xfail until task 3.1; image digest, extension versions, `/readyz` body; the core RAM report `core-ram-2026-11-02` |
| [task-3.1-notes.md](task-3.1-notes.md) | 3.1 | S2 harness on native full-text search (`body_tsv`, `chunks_fts_idx`, `fts` variants, ACL locations `index`/`filter`/`none`), the spike host guard (`--allow-remote`), the 14 recorded FTS plan fixtures, the finding that `@@` is not leakproof so the FTS index is not used under FORCE RLS at 2,000 rows |
| [task-3.5-notes.md](task-3.5-notes.md) | 3.5 | S2 at 20,000 rows on native full-text search (`docs/spikes/S2/2026-11-02-*`): FTS latencies and index use under FORCE RLS with and without the explicit predicate (index unused in 48 of 48 plans), the superuser baseline that does use it, the 27 Oct BM25 numbers labelled as a different engine, planner choice at 1% and 5%, HNSW recall by iterative-scan setting, and what the numbers show for ADR-0006 (input only) |
| [task-4.1-notes.md](task-4.1-notes.md) | 4.1 | Admin and TLS settings (`db_sslmode`, `db_sslrootcert`, `libpq_tls`, admin user, password fallback, database and mode), where each connection takes them, type fixes, FV11 follow-up in the spec text |
| [task-4.3-notes.md](task-4.3-notes.md) | 4.3 | Managed-mode bootstrap (`resolve_mode`, mode-aware builders, `password_form` and `--allow-plaintext-password`), `cairn-db emulate-managed-admin` and its guard, the FV4 probe, tests, Verify results |
| [task-4.7-notes.md](task-4.7-notes.md) | 4.7 | Managed-mode test fixtures (`CAIRN_TEST_ADMIN_MODE`, `connect_admin`, `drop_database` under `SET ROLE`), markers `managed` and `freetier`, the managed role-test case, rehearsal on a throwaway server, Verify results |
| [task-5.1-notes.md](task-5.1-notes.md) | 5.1 | `apps/combined` (`cairn-combined`): `CombinedSettings`, `JobsRunner`, `launch_dbos`/`destroy_dbos`, `backoff_schedule`, `create_app`, `main`; `build_app(jobs, lifespan)`; the `jobs` readiness check; `automatic_backfill` off; Dockerfile default `PACKAGE=cairn-combined`; why Requirement 8.4's give-up path is not implemented; Verify results |
| [task-5.2-notes.md](task-5.2-notes.md) | 5.2 | `cairn_api.proxy_token` (`allowed`, middleware, 404 `not_found` with `no-store`, `hmac.compare_digest`, `/healthz` exempt), `ApiSettings.proxy_token` (at least 32 characters, exit 2), the healthcheck header, installation in `build_app` (the combined app too), and why tests now pass `CAIRN_PROXY_TOKEN: None` explicitly; Verify results |
| [task-6.1-notes.md](task-6.1-notes.md) | 6.1 | The freetier services (`ft-db`, `ft-admin-init`, `ft-db-init`, `ft-app`) in `docker-compose.yml`, `compose-check` for `freetier`, the compose config tests, the ft-app healthcheck values chosen after FV13, Verify results |
| [task-6.2-notes.md](task-6.2-notes.md) | 6.2 | `tests/freetier/test_freetier_profile.py` (12 checks: proxy header, readiness, heartbeat as `cairn_dbos`, no `pg_search`, admin attributes, ft-app limits and environment names, timed `stop ft-app`), `mk/freetier.mk` targets, the managed matrix on `ft-db` (13 passed, 1 skipped), Verify results |
| [task-6.3-notes.md](task-6.3-notes.md) | 6.3 | `scripts/freetier_ram.py` (`RamBudget`, `Phase`, `verdict`, sampler, two-client load, writers, exit codes), `make freetier-ram`, the 52 tests, the first real run (pass: idle peak 94.6 MiB, load peak 95.0 MiB, 0 of 288 requests failed), Verify results |
| [task-6.5-notes.md](task-6.5-notes.md) | 6.5 | `freetier-check` in `GATES_ALL` after the core `down`, the CI steps (`make down`, `make freetier-check`, free-tier logs on failure), `make freetier-logs`, `phase0_exit.py` (free-tier RAM evidence, core db image check, EG5, FU1 to FU9), the tests, and the first full `freetier-check` run (PASS) |
| [task-8.3-notes.md](task-8.3-notes.md) | 8.3 | Neuron budget (`NEURON_RATES`, estimators, `InMemoryUsageStore`, `PostgresUsageStore`, `NeuronBudget`), migration `0002_provider_usage.sql`, tests in both admin modes, the bge-small rate fix (1,841) and the managed-mode test fix, Verify results |
| [task-8.7-notes.md](task-8.7-notes.md) | 8.7 | Workers AI client (`client.py`), parsers (`parse.py`), contract fixtures and tests, FV8 re-check, Verify results |
| [task-8.13-notes.md](task-8.13-notes.md) | 8.13 | Provider protocols, Workers AI adapters, fakes and builders (`cairn_retrieval.models`, `cairn_retrieval.llm`), the FV9 empty-answer rule (`finish_reason` `length`), the `live` tests and the user-run `make workers-ai-live-check` in `mk/providers.mk` (in no gate, not in CI), `tests/scripts/test_live_check.py`, Verify results |
| [task-9.1-notes.md](task-9.1-notes.md) | 9.1 | `render.yaml`, `tests/scripts/test_render_blueprint.py`, deviations from design §14, Verify results |
| [task-9.2-notes.md](task-9.2-notes.md) | 9.2 | `apps/web/wrangler.toml`, `public/_routes.json`, `public/_headers`, the `/api` Function and `functions/_lib/proxy.ts` with its 60 vitest cases, `mk/deploy.mk` `web-dist`, `tests/scripts/test_pages_config.py`, deviations from design §14, Verify results |
| [task-9.3-notes.md](task-9.3-notes.md) | 9.3 | The user-run targets `env-neon`, `remote-init` and `remote-status` in `mk/deploy.mk` (refusals before any docker call, `--allow-plaintext-password`, image build when absent), `tests/scripts/test_deploy_targets.py` with a stubbed `docker`, Verify results |
| [task-9.4-notes.md](task-9.4-notes.md) | 9.4 | `docs/deploy.md` (topology, limits with sources, FU1 to FU9 each "Needs user", operations, the Neon CU-hours consequence of DBOS polling, neuron budget, password rotation, teardown, removing `cairn_pgdata`, troubleshooting), `tests/scripts/test_deploy_doc.py`, decisions, Verify results |
| [task-12.1-notes.md](task-12.1-notes.md) | 12.1 | ADR-0013 to ADR-0015, ADR-0001 superseded, in-place corrections to ADR-0004 and ADR-0005, the ADR index and reserved numbers, the checker (`ADR_NUMBERS`, expected status, keyword table) and its tests, the History-section test in `test_no_paradedb.py`, Verify results |
| [task-12.2-notes.md](task-12.2-notes.md) | 12.2 | Threat model update (TM-11, TM-13, new TM-15 and TM-16, diagram, controls), `THREAT_IDS` to TM-16, checker tests, Verify results |
| [task-13.2-notes.md](task-13.2-notes.md) | 13.2 | Source-document edits |

## Verify items

| File | Item | Status |
|---|---|---|
| [verify/FV0-platform-facts.md](verify/FV0-platform-facts.md) | FV0: platform facts of 30 Oct 2026 with source URLs | Recorded (task 1.1) |
| [verify/FV1.md](verify/FV1.md) | FV1: pgvector image tag, digest and the Neon pgvector version for PostgreSQL 18 | Resolved (task 1.1) |
| [verify/FV2.md](verify/FV2.md) | FV2: PostgreSQL 18 and `aws-ap-southeast-1` on Neon free | Resolved from docs (task 1.1); user confirms in FU1 and FU2 |
| [verify/FV3.md](verify/FV3.md) | FV3: what `neondb_owner` can do | From docs (task 1.1): Neon refuses hashed passwords; OQ-1 decided 2 Nov 2026 (plaintext over `verify-full` behind `--allow-plaintext-password`); item 7 (`vector` not trusted) resolved in task 4.3; rest confirmed by the user in FU2 |
| [verify/FV4.md](verify/FV4.md) | FV4: what a CREATEROLE / CREATEDB / BYPASSRLS non-superuser can do on plain PostgreSQL 18 (roles, SET grants, databases, extensions); raw probe in [FV4-probe.sql](verify/FV4-probe.sql) and [FV4-probe.out](verify/FV4-probe.out) | Part 1 resolved (task 4.3); part 2 (managed matrix on `ft-db`: 13 passed, 1 skipped) resolved (task 6.2) |
| [verify/FV5.md](verify/FV5.md) | FV5: DBOS 3.3.0 in one long-lived process: relaunch after `destroy()` works, launch from a non-main thread under uvicorn works, `automatic_backfill` defaults to off, about 9.5 idle transactions per second (Neon stays awake while the process is awake), TLS parameters in the URL work; probe [FV5-probe.py](verify/FV5-probe.py) and output [FV5-probe.out](verify/FV5-probe.out) | Resolved (task 5.1) |
| [verify/FV6.md](verify/FV6.md) | FV6: Render free web service from the docs: health checks, sleep and wake, SIGTERM grace, env vars as build arguments, `dockerCommand`, `PORT`, `sync: false`, `singapore`; open for FU5: whether health probes keep the service awake | Resolved from docs (task 9.1); FU5 confirms the idle behavior |
| [verify/FV7.md](verify/FV7.md) | FV7: Cloudflare Pages Functions routing, `_routes.json`, `_headers`, `wrangler.toml` vars, Direct Upload with Functions, Pages secrets (set before the deploy), wrangler 4.156.2 pin, origin timeout, free-plan limits; open for FU6: first deploy behavior | Resolved from docs (task 9.2); FU6 confirms the live behavior |
| [verify/FV8.md](verify/FV8.md) | FV8: Workers AI REST endpoint and envelope, request and response shapes of the four default models, limits, error codes (`DAILY_LIMIT_CODES = {3036}`), `Retry-After` | Resolved from docs (task 8.3, for task 8.7); live behavior of 429 headers open |
| [verify/FV9.md](verify/FV9.md) | FV9: neuron rates (bge-small is 1,841), gemma and glm on the free plan, reasoning tokens counted as output; design §12 figures unchanged within one neuron | Resolved from docs (task 8.3); first live call confirms the reasoning-token count |
| [verify/FV11.md](verify/FV11.md) | FV11: `sslrootcert=system` loads no roots in `python:3.12.16-slim`; Neon and Render use `verify-full` with `CAIRN_DB_SSLROOTCERT=/etc/ssl/certs/ca-certificates.crt` | Resolved (task 4.1) |
| [verify/FV13.md](verify/FV13.md) | FV13: ports 55433 and 8010 free, `cpus` 0.1 and `memswap_limit` honored (cgroup values), tmpfs ownership (uid 999) for the Postgres data directory, `--profile freetier down` leaves core containers; finding: the ft-app healthcheck needs `interval 30s`, `timeout 60s` at 0.1 CPU | Resolved (task 6.1) |
| [verify/FV12.md](verify/FV12.md) | FV12: S1 topic tokens under `simple` (`t3_5` becomes the phrase `'t3' <-> '5'`, matching stays exact) | Resolved (task 3.1) |

## RAM reports

| File | Content |
|---|---|
| [freetier-ram-2026-11-03.md](freetier-ram-2026-11-03.md), [.json](freetier-ram-2026-11-03.json) | `make freetier-ram` (task 6.3, rewritten by the `make freetier-check` run of task 6.5 on the same date): PASS, idle peak 94.34 MiB of 300, load peak 94.52 MiB of 384, 0 of 298 requests failed, no OOM kill, no restart |

`make freetier-ram` writes `freetier-ram-<date>.md` and `.json` here; core profile reports from `make ram-report` stay in `docs/evidence/phase-0/`.
