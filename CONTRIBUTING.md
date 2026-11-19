# Contributing to Cairn

Cairn is built in seven phases between 27 Oct and 20 Nov 2026. This guide covers how to set up the repo, what "done" means, which checks must pass, how to work with an AI coding assistant, how decisions are recorded, what data is allowed, and the steps only the project owner takes.

Sources: [development_plan.md](development_plan.md) (phases, numbered items, definition of done, quality gates) and [brainstorm.md](brainstorm.md) (decisions D1 to D20, architecture, risks). Neither file is edited by a task or script on its own: they change only through an approved spec, with the hash record in `docs/evidence/phase-0/source-docs.sha256` updated alongside (the free-tier retarget spec did this, see `docs/evidence/free-tier/source-doc-edits.md`). Other documents: [docs/prd.md](docs/prd.md), [docs/threat-model.md](docs/threat-model.md), [docs/acceptance-tests.md](docs/acceptance-tests.md), [docs/adr/README.md](docs/adr/README.md), [docs/licenses.md](docs/licenses.md).

Labels follow the brainstorm: Proposed target is an acceptance target, Estimate is a planning number, and Verify is a fact still to confirm.

## Setup and quick start

### Prerequisites

| Tool | Version | Notes |
|---|---|---|
| Make | GNU Make 3.81 or newer | Ships with macOS. Every check is a `make` target, and CI calls the same targets. |
| uv | 0.11.25 | The root `pyproject.toml` requires this exact version. uv installs and manages CPython 3.12. |
| Docker Desktop | current | Give it at least 4 GB of memory before any database task (user step U1). Needed for the web gates, the secret scan and, once they exist, the compose services. |
| Node | 24 LTS (optional on the host) | `make web-check` runs inside a pinned Node 24 container by default. Install Node 24 only if you want `pnpm dev` or `WEB_RUNNER=host` (user step U7). The host's Node 20 is past end of life. |

### First run

```bash
uv sync --locked --all-packages   # create .venv from uv.lock; fails if the lock is out of date
make env                          # write .env with random local secrets; refuses to overwrite
make env-update                   # later: append keys that a newer .env.example added (never edits a line)
make help                         # list every target
make fmt-check lint types test-unit
make web-check
make docs-check
```

- `.env` holds local secrets and is never committed. It is listed in `.gitignore`, and `.env.example` keeps placeholder values only. If `.env` already exists, `make env` leaves it alone and exits non-zero. After a pull that adds keys to `.env.example`, run `make env-update`: it appends the missing keys with fresh secrets and a dated comment, and never changes an existing line (so a local `CAIRN_DB_PORT` stays). Keys that only you can supply are appended with an empty value.
- `make fmt` rewrites Python files in place (import order and formatting). `make web-fmt` does the same for the web app.
- Property tests read `HYPOTHESIS_PROFILE`: `dev` runs 100 examples, `ci` runs 200. Example: `make test-unit HYPOTHESIS_PROFILE=ci`.
- Run one docs group while others are unfinished: `make docs-check DOCS_ONLY=links`. Groups: `adr`, `threat-model`, `acceptance`, `prd`, `contributing`, `links`.

### Targets that exist today

| Area | Targets |
|---|---|
| Python | `fmt`, `fmt-check`, `lint`, `types`, `test-unit` |
| Environment | `env`, `env-update` |
| Web | `web-check`, `web-fmt`, `web-lock`, `web-clean`, `web-dist` (static build for Cloudflare Pages) |
| Compose and database | `compose-check`, `up-db`, `db-init`, `db-status`, `db-reset` |
| Core stack | `up`, `down`, `logs`, `smoke` |
| Free-tier emulation | `freetier-up`, `freetier-smoke`, `matrix-managed`, `freetier-ram`, `freetier-check`, `freetier-down`, `freetier-logs` |
| Observability (optional) | `up-obs`, `down-obs`, `logs-obs`, `obs-check` |
| Memory | `ram-report` (core profile RAM report; needs Docker) |
| Database tests | `test-integration`, `migrations-check`, `matrix` (need `up-db`) |
| Scenario | `scenario-validate` (validates `scenarios/kestrel_pay/scenario.yaml`; exits 0 when the file is valid) |
| Security | `audit`, `secrets`, `secrets-history`, `licenses` |
| Docs | `docs-check` |
| CI config | `actionlint` (lints `.github/workflows/*.yml`) |
| Aggregate | `gates` (every quality gate in order) |
| User-run (never in `gates`, never run by agents) | `env-neon`, `remote-init`, `remote-status` (Neon, FU1 and FU2); `workers-ai-live-check` (Workers AI, FU8) |

`phase0-exit` is the local exit gate for Phase 0 (see [Quality gates](#quality-gates)). Run `make help` for the current list, and see [Make target reference](#make-target-reference) for what each target does and where it is defined.

## Repo layout

| Path | Contents |
|---|---|
| `apps/api` | FastAPI service with `/healthz` and `/readyz` |
| `apps/worker` | DBOS worker with the scheduled heartbeat workflow |
| `apps/combined` | The combined process (`cairn-combined`): the API and the DBOS jobs in one service, for the free-tier hosting |
| `apps/mcp` | MCP server stub, built in Phase 4 |
| `apps/web` | React, TypeScript and Vite web app, built with pnpm 10.8.1 |
| `packages/core` | Settings, database bootstrap, migration runner, telemetry and shared test fixtures. mypy runs in strict mode here. |
| `packages/connectors` | Connector protocol stub, filled in Phase 1 |
| `packages/retrieval` | Placeholder, filled in Phase 1 |
| `packages/eval` | Scenario models, validator and canaries; S1 and S2 spike harnesses |
| `scenarios/kestrel_pay` | Kestrel Pay scenario data and broken validator fixtures |
| `mk` | One Make fragment per area, included by the root `Makefile` |
| `scripts` | Developer tooling: env generation, docs checker, license checker |
| `tests` | Tests for the tooling in `scripts` and the repo setup |
| `infra` | Container and observability configuration (filled in by the compose tasks) |
| `docs` | PRD, threat model, acceptance tests, ADRs in `docs/adr`, spike reports in `docs/spikes`, deploy runbook in `docs/deploy.md`, evidence in `docs/evidence/phase-0` and `docs/evidence/free-tier` |

Each Python package uses a `src/` layout with its tests next to it in `tests/`. Tests are marked `unit` (default), `integration` (needs the database), `matrix` (permission assertions), `smoke` (needs the running stack) or `obs` (needs the running obs profile; `make obs-check`).

## Definition of done

From the plan's global definition of done. An item is ticked only when all of these hold:

- Merged through a PR with passing CI.
- Unit tests for new logic, plus an integration test for every new path that touches the database, a source or a model.
- The permission matrix still passes with 0 observed leaks, and every new query path is added to the matrix.
- Eval smoke is not regressed beyond the gate tolerance (it starts in Phase 1).
- Docs are updated, and an ADR exists for any new or changed decision.
- Traces and structured logs are visible for new endpoints and workflow steps.
- No secrets, tokens or real company data in code, fixtures, prompts or logs.
- Status is reported as implemented, tested and documented, separately.

A phase is done only when its exit gate passes. Phase 0 has no remote yet, so its local exit gate is `make phase0-exit` plus the user steps U3 and U4 below.

## Quality gates

Every gate maps to a `make` target, and CI runs the same targets.

| Gate | Tooling | Make target | Blocks merge when |
|---|---|---|---|
| Format and lint | ruff, eslint, prettier | `fmt-check`, `lint`, `web-check` | Any error |
| Types | mypy (strict in `packages/core`), tsc | `types`, `web-check` | Any error |
| Unit tests | pytest, vitest | `test-unit`, `web-check` | Any failure |
| Integration | pytest against the compose database | `test-integration` | Any failure |
| Migrations | Apply all migrations to a fresh database | `migrations-check` | Any failure |
| Permission matrix | pytest marker `matrix` | `matrix` | Any observed leak |
| Stack smoke | pytest marker `smoke` against the running core profile | `smoke` | Any failure |
| Free-tier emulation | `freetier` profile: smoke, managed-mode permission matrix, RAM budget (`ft-app` at 512 MB and 0.1 CPU) | `freetier-check` (after the core `down`) | Any failure, or a missed RAM budget |
| Eval smoke | 20 C1 dev questions | Not active until Phase 1 | A deterministic failure, or Recall@5 down more than 0.05 (Proposed target) |
| Dependency and secret scan | pip-audit, pnpm audit, gitleaks | `audit`, `secrets`, `secrets-history` (CI) | High or critical finding, or any secret |
| Extra checks | docker compose, actionlint, scenario validator, docs checker, license policy | `compose-check`, `actionlint`, `scenario-validate`, `docs-check`, `licenses` | Any failure |

`make gates` runs all of them in one command, in the same order as the CI jobs, and `make phase0-exit` adds the S1 and S2 smoke runs and prints the exit-gate checklist. The core stack is brought down before `freetier-check` starts, so the two profiles never share the Docker VM; the CI integration job does the same (`make smoke`, `make down`, `make freetier-check`). `workers-ai-live-check` is in no gate and no workflow.

### Make target reference

Every target has a one-line description in `make help`. This table adds the file that defines it and what to know before you run it. Targets marked pending have no rule yet; check with `make -n <target>`.

| Target | Defined in | Notes |
|---|---|---|
| `help` | [Makefile](Makefile) | Lists every documented target |
| `fmt`, `fmt-check`, `lint`, `types`, `test-unit` | [mk/python.mk](mk/python.mk) | `HYPOTHESIS_PROFILE=dev` or `ci` sets the number of property test examples |
| `env`, `env-update` | [mk/env.mk](mk/env.mk) | `env` writes `.env`; exits non-zero if it already exists. `env-update` is `scripts/dev_env.py --add-missing`: it appends the keys of `.env.example` that `.env` lacks, with fresh random secrets and a dated comment, and never rewrites or reorders an existing line; running it twice changes nothing the second time. A key marked `user-provided` is appended with an empty value |
| `compose-check` | [mk/db.mk](mk/db.mk) | Validates `docker-compose.yml` for the `core`, `obs`, `experiments` and `freetier` profiles using `.env.example`; needs the `docker compose` command and no `.env` |
| `up-db`, `db-init`, `db-status`, `db-reset` | [mk/db.mk](mk/db.mk) | Need Docker running and `.env`. `db-reset` deletes the `cairn_pgdata-pgvector` volume and asks first; `CONFIRM=yes` skips the question. `db-reset-guard` (no Docker call) makes `db-reset` refuse any other `DB_VOLUME`, so the old `cairn_pgdata` volume can never be reached from `make` (FU7) |
| `scenario-validate` | [mk/scenario.mk](mk/scenario.mk) | `SCENARIO_FILE=path` checks another file. Exit 0 valid, 1 violations, 2 file unreadable |
| `audit`, `secrets`, `secrets-history`, `licenses`, `actionlint` | [mk/security.mk](mk/security.mk) | `secrets`, `secrets-history` and `actionlint` run in pinned containers. `secrets` scans the working tree. `secrets-history` runs `gitleaks git` over the full history, needs a git checkout and is run by CI, not by `gates`. `actionlint` lints every file in `.github/workflows` and fails if there is none |
| `web-check`, `web-fmt`, `web-lock`, `web-clean` | [mk/web.mk](mk/web.mk) | `WEB_RUNNER=host` uses the host's Node 24 instead of the container |
| `web-dist` | [mk/deploy.mk](mk/deploy.mk) | Builds `apps/web/dist` (static files, `_routes.json`, `_headers`) in the pinned Node container for the Cloudflare Pages Direct Upload. Safe for agents and CI: it never logs in to or calls Cloudflare. The upload is the user step FU6 |
| `env-neon`, `remote-init`, `remote-status` | [mk/deploy.mk](mk/deploy.mk) | **User-run** (FU1, FU2); agents and CI never call them. `env-neon` writes `.env.neon` from `.env.neon.example` with fresh secrets and empty `user-provided` keys, and refuses to overwrite. `remote-init` runs `cairn-db init --allow-plaintext-password` in the `cairn-python:local` container with `--env-file .env.neon` against your Neon project (roles, databases, extensions, migrations); it needs `CAIRN_DB_SSLMODE=verify-full`. `remote-status` runs `cairn-db status` (read only). Both first check, without connecting and without printing a value, that `.env.neon` has every `user-provided` key filled. Steps and troubleshooting: [docs/deploy.md](docs/deploy.md) |
| `workers-ai-live-check` | [mk/providers.mk](mk/providers.mk) | **User-run** (FU8). Three small real calls (embed, rerank, text) to Cloudflare Workers AI with the token in `.env`, under 100 of the 10,000 free neurons of the day. It is in no gate and no workflow; `test-unit` and `test-integration` exclude the `live` marker |
| `docs-check` | [mk/docs.mk](mk/docs.mk) | `DOCS_ONLY=<group>` runs one group |
| `test-integration`, `migrations-check`, `matrix` | [mk/db-tests.mk](mk/db-tests.mk) | Need the `db` service (`make up-db`). Tests skip, and `migrations-check` exits 2, when the database is not reachable. Each run uses a throwaway `cairn_test_*` database |
| `up`, `down`, `logs`, `smoke` | [mk/stack.mk](mk/stack.mk) | `up` builds and starts the whole core profile and waits until it is healthy; on failure it prints `docker compose ps` and the last 50 log lines. `UP_BUILD=` skips the image build. `down` removes containers and the network but never the volumes (`db-reset` deletes the database volume). `logs` takes `LOG_TAIL=n` and `SERVICE=name`. `smoke` runs `up`, then `pytest -m smoke tests/smoke` |
| `freetier-up`, `freetier-smoke`, `matrix-managed`, `freetier-ram`, `freetier-check`, `freetier-down`, `freetier-logs` | [mk/freetier.mk](mk/freetier.mk) | The `freetier` profile emulates Render free (`ft-app`: 512 MB, 0.1 CPU) and Neon's owner role (`ft-db` with a non-superuser `cairn_admin`). Never run it together with the core profile on an 8 GB host: `make down` first. `freetier-up` is `docker compose --profile freetier up --wait --build` and takes about 2.5 minutes. `freetier-smoke` runs `pytest -m freetier tests/freetier` against the running profile (skips with a message when `ft-app` is not up); its last test stops `ft-app` to time the shutdown, so run `freetier-up` again before another smoke run. `matrix-managed` runs `CAIRN_TEST_ADMIN_MODE=managed pytest -m "matrix or managed"` against `ft-db` as `cairn_admin` (13 passed, 1 skipped). `freetier-ram` runs `scripts/freetier_ram.py`: it brings the profile up itself (`up --wait`, so an `ft-app` stopped by the smoke module starts again), refuses to run while core containers are up, samples `ft-app` with `docker stats --no-stream` every 2 s for 60 s idle and 60 s under load from two clients (`/readyz` with the proxy header from `.env`, `/healthz`, an unknown path, 0.2 s pause), and writes `docs/evidence/free-tier/freetier-ram-<date>.md` and `.json`. It passes when the idle peak is at most 300 MiB, the load peak at most 384 MiB, failed requests at most 2%, and `ft-app` was neither OOM-killed nor restarted; exit 0 pass, 1 budget missed, 2 Docker or the profile unusable. `FT_RAM_ARGS` passes options (`--no-up`, `--idle-s`, `--load-s`, `--interval-s`, `--out-dir`). The token is never printed. `freetier-check` runs the five in order and stops at the first failure; the whole run takes about 8 to 10 minutes (the profile is built and started twice). `freetier-logs` shows the recent logs of the freetier services (`LOG_TAIL=n`); CI uploads them as `freetier-logs.txt` when a step fails. `freetier-down` removes the four freetier containers and never touches volumes or core containers |
| `up-obs`, `down-obs`, `logs-obs`, `obs-check` | [mk/obs.mk](mk/obs.mk) | `up-obs` starts the core profile plus the obs profile (OpenTelemetry Collector, Prometheus, Grafana) and sets `OTEL_TRACES_EXPORTER=otlp` for the api and the worker through compose, so `.env` is not edited and plain `make up` still exports nothing. Switching between `up` and `up-obs` recreates only the api and the worker (and web, which waits on the api); the database is untouched. Grafana is at `http://127.0.0.1:3000` (user `admin`, password `GRAFANA_ADMIN_PASSWORD` from `.env`), Prometheus at `http://127.0.0.1:9090`, the collector's OTLP/HTTP port at `127.0.0.1:4318`; `CAIRN_GRAFANA_PORT`, `CAIRN_PROMETHEUS_PORT` and `CAIRN_OTLP_PORT` move them. The first Grafana start is slow (see Troubleshooting). `down-obs` stops and removes the three obs containers only and keeps every volume; run `make up` afterwards to turn the exporter off again. `logs-obs` shows the collector log, which prints each exported span (`LOG_TAIL=n`). `obs-check` runs `up-obs`, then `pytest -m obs tests/obs`: one `/readyz` call must show up in the collector log and as a `traces_span_metrics_calls_total` series for `cairn-api` in Prometheus, and Grafana must list the Prometheus data source. It leaves the obs profile running; `test-unit` excludes the `obs` marker |
| `ram-report` | [mk/ram.mk](mk/ram.mk) | Runs `scripts/measure_ram.py`: `docker compose --profile core up --wait` (with `--build` unless `UP_BUILD=` is empty), a 60 s warm-up (20 `/readyz` calls, one worker heartbeat), then `docker stats --no-stream` every 5 s for 60 s for the db, api, worker and web containers, plus each container's cgroup `memory.stat` (`anon`, `file`, `shmem`), because `docker stats` over-reports file cache. Writes `docs/evidence/phase-0/core-ram-<date>.md` and `.json` with per-container mean and peak, the total peak against the about 4 GiB estimate and the 6 GiB ceiling, the Docker VM and host memory, and the image digests. Exit 0 within the ceiling, 1 when the total peak is above 6 GiB, 2 when Docker or the stack is unusable. `RAM_ARGS="--no-up --warmup 10"` passes options (`--no-up`, `--warmup`, `--duration`, `--interval`, `--out-dir`). It never stops or resets the database container |
| `gates` | [mk/gates.mk](mk/gates.mk) | Runs `fmt-check lint types test-unit web-check scenario-validate compose-check actionlint licenses docs-check audit secrets`, then `up-db test-integration matrix migrations-check smoke`, then `down`, then `freetier-check` (the core stack is down first, so the profiles never share the Docker VM; about 8 to 10 minutes more), stopping at the first failure. Needs Docker running and `.env` (with the freetier keys: `make env-update`); the stack is left running after a failure so you can look at it. `make -n gates` lists every step without running anything. The CI workflow [.github/workflows/ci.yml](.github/workflows/ci.yml) calls the same targets |
| `phase0-exit`, `phase0-exit-preview` | [mk/exit.mk](mk/exit.mk) | Local exit gate. `phase0-exit` runs `gates` (which ends with `freetier-check`), then `up-db`, `s1-smoke` and `s2-smoke`, then `down`, stopping at the first failure; then `scripts/phase0_exit.py` checks that `brainstorm.md` and `development_plan.md` still match `docs/evidence/phase-0/source-docs.sha256` (read only), that the newest `core-ram-<date>` report from `make ram-report` exists, says pass and names the `pgvector/pgvector` image for the `db` service, and that the newest `docs/evidence/free-tier/freetier-ram-<date>.json` exists and says pass, and prints the checklist: EG1 to EG5 with the command that proves each (EG5 is `make freetier-check`), the user steps U1 to U7 and FU1 to FU9 as NOT automated, and the Verify items (V9 to V11) and spikes (S2, S5) still open. It runs no git command. Needs Docker and `.env`, and builds images, so run it on an idle machine. `make -n phase0-exit` lists the steps; `phase0-exit-preview` prints the checks and checklist without running any gate and without claiming a result. Exit 1 when a document hash differs or any RAM evidence is missing, failed or from a `db` image other than `pgvector/pgvector` |

## AI-assisted workflow

Rules from section 18 of the brainstorm:

- One component per session, starting from written interfaces and acceptance tests.
- The assistant reports what is implemented, what is tested and what is documented, separately.
- Nothing is done until its integration tests and the permission matrix pass.
- A second model reviews diffs for security-sensitive code: authorization, sync and MCP.
- Any change to D1 to D20 gets an ADR.
- No real company data or secrets in prompts.

How to hand a task to an assistant:

1. Name the item by its plan number (for example `1.12`) or a small group of numbers. Items are numbered phase.item in `development_plan.md`.
2. Link the brainstorm section the item cites, and the acceptance test or threat id it must satisfy from [docs/acceptance-tests.md](docs/acceptance-tests.md) or [docs/threat-model.md](docs/threat-model.md).
3. Ask for the interface and the tests first, then the implementation.
4. Require the three-part status report and the `make` targets that were run, with their results.
5. For authorization, sync or MCP changes, have a second model review the diff before merging.

Agents and scripts never run `git commit` or `git push`, and edit `brainstorm.md` or `development_plan.md` only under an approved spec, with `docs/evidence/phase-0/source-docs.sha256` updated alongside. Evidence for finished work goes to `docs/evidence/phase-0/` as short notes.

### Worked hand-off examples

Each example is a prompt you can paste, then adapt. The plan items are real Phase 0 items from [development_plan.md](development_plan.md).

Example 1, a single item with a security test:

```text
Implement plan item 0.17 (role test): the app role owns no tables, lacks
BYPASSRLS, and reads zero rows from an RLS-enabled table that has no policy.
Context: threat TM-01 in docs/threat-model.md and ADR-0004.
Write the test first and show it failing, then fix the code.
Run: make fmt-check lint types test-unit, then the integration test against
the compose database (make up-db, make db-init).
Report implemented, tested and documented separately, with each make target
you ran and its result.
```

Example 2, a small group of items in one session:

```text
Implement plan items 0.12 and 0.18 together: the scenario validator and its
test on scenario v1 plus three broken fixtures. Files: packages/eval and
scenarios/kestrel_pay. Do not touch packages/core.
Run: make scenario-validate, make test-unit.
Do not run git commit or git push. Do not edit brainstorm.md or
development_plan.md. Add a short note to docs/evidence/phase-0/.
```

Example 3, a documentation-only item:

```text
Polish plan item 0.23 (CONTRIBUTING.md). Only edit CONTRIBUTING.md.
Mark a make target (pending) only if `make -n <target>` says there is no
rule for it. Run: make docs-check.
```

When the result comes back, check three things: the status report has all three parts, the named `make` targets were run (re-run them if in doubt), and any new database path appears in the permission matrix.

## ADR process

Decisions D1 to D20 are final for the MVP. Changing one, or making a new architectural decision, needs an ADR. Cairn's own ADRs use four digits (ADR-0001) and the MADR 4.0 format. The template, numbering, status values and the index are in [docs/adr/README.md](docs/adr/README.md), and the blank form is [docs/adr/template.md](docs/adr/template.md). Kestrel Pay scenario decisions use three digits (ADR-007) and live in the scenario file, not in `docs/adr`.

`make docs-check` fails if ADR-0001 to ADR-0005 or ADR-0013 to ADR-0015 lose their front matter, expected status, required sections or keywords. ADR-0001 is `superseded by ADR-0013`. ADR-0006 to ADR-0012 are reserved for Phase 1 and the spike tracker (see the index).

## Data rules

- Synthetic and public data only (D20). No real company data, customer data or personal data, in code, fixtures, prompts, logs or screenshots.
- Fictional names for every person, team, partner and repository. Display names are first names only.
- URLs use reserved `.example` domains.
- No real secrets anywhere. `.env.example` has placeholders, and `.env` is generated locally by `make env` and is never committed. `make secrets` scans the working tree.
- Canary strings (`KP-HR-...` and similar) are test data. They exist so tests can detect leaks. Never put one in a place its scenario scope does not allow.
- Log lines carry stable error codes, not exception text, hostnames or credentials.

## Troubleshooting

### Docker Desktop memory and the database container

- Symptom: `make up-db` waits for a long time, then reports the `db` service as unhealthy, or `docker compose ps` shows `db` as exited with code 137. Cause: the container was killed for lack of memory. Fix: Docker Desktop, Settings, Resources, Memory: set at least 4 GB (user step U1), apply and restart, then run `make up-db` again. Check the setting with `docker info --format '{{.MemTotal}}'` (bytes).
- The `db` service has a 1 GiB limit (`CAIRN_DB_MEM_LIMIT`, default `1024m`). Raise it in `.env` only if you see out-of-memory kills in `docker compose logs db`; the long-running core services are meant to stay within 4 GB in total.
- Symptom: `make up-db` fails right away with a message such as `run make env`. Cause: `.env` is missing or lacks a secret. Fix: run `make env` (it refuses to overwrite an existing file, so delete a broken `.env` first), then `make up-db`.
- Symptom: `make db-init` fails with a password authentication error after you regenerated `.env`. Cause: the database volume still holds the old role passwords. Fix: `make db-reset` (this deletes every database in the `cairn_pgdata-pgvector` volume), then `make up-db` and `make db-init`.
- Symptom: `make up-db` fails because port 5432 is in use. Cause: a local PostgreSQL is already listening. Fix: stop it, or set `CAIRN_DB_PORT` (and `CAIRN_TEST_ADMIN_PORT`) in `.env` to a free port. The port binds to `127.0.0.1` only.
- Symptom: `make compose-check` says `profile <name> is invalid`. Cause: `docker-compose.yml` has a syntax error or a variable that `.env.example` does not define. Fix: run `docker compose --env-file .env.example --profile core config` to see the exact message.
- Symptom: `docker volume ls` still lists `cairn_pgdata` next to `cairn_pgdata-pgvector`. Cause: Phase 0 stored its data in `cairn_pgdata`, which the previous database image initialized. The `db` service now mounts `cairn_pgdata-pgvector` and nothing mounts the old one; the new image cannot read it. Fix: nothing is needed. Remove the old volume yourself when you are satisfied (FU7, [docs/deploy.md](docs/deploy.md)); no make target deletes it.
- Symptom: `make db-init`, `cairn-db migrate` or `remote-init` stops with `ChecksumMismatch` for `0001_baseline.sql`. Cause: the database recorded the earlier version of the baseline migration, which was edited in place once (ADR-0013, `0001_baseline`). Only a database created before the free-tier retarget has that row; a fresh Neon project does not. Fix: for the local database, `make db-reset`, `make up-db`, `make db-init`. Never change the checksum by hand.
- Symptom: `make freetier-up` takes two to three minutes, and `docker compose ps` shows `ft-app` as `starting` for a long time. Cause: `ft-app` is limited to 0.1 CPU, as on Render free, so imports and the healthcheck are throttled; its health check allows 180 s before it counts failures. Fix: wait; the profile is ready when `freetier-up` returns. Do not raise `cpus` to speed it up, because the RAM and timing evidence assumes the limit.
- Symptom: `make freetier-up` fails because a port is in use, or `freetier-smoke` skips. Cause: the freetier profile publishes `ft-db` on `127.0.0.1:55433` and `ft-app` on `127.0.0.1:8010` (`CAIRN_FREETIER_DB_PORT`, `CAIRN_FREETIER_APP_PORT`); these are separate from the core ports. A smoke skip means `ft-app` is not running (its last test stops it). Fix: set the two variables in `.env` to free ports, check with `lsof -nP -iTCP:55433 -sTCP:LISTEN`, and run `make freetier-up` again. Never run it while the core profile is up: `make down` first.
- Symptom: `make freetier-up` fails with `run make env-update`. Cause: `.env` predates the freetier keys (`CAIRN_PROXY_TOKEN`, `CAIRN_MANAGED_ADMIN_PASSWORD` and others). Fix: `make env-update`.
- Symptom: `remote-init`, `remote-status` or the hosted app fails with an SSL or certificate error against Neon. Cause: `CAIRN_DB_SSLMODE` is not `verify-full`, `CAIRN_DB_SSLROOTCERT` no longer points to `/etc/ssl/certs/ca-certificates.crt`, or `CAIRN_DB_HOST` is not the Neon direct endpoint host (the pooled `-pooler` host is not used). Fix: restore the values of `.env.neon.example`; `require` is not used. Other Neon, Render and Cloudflare symptoms are in the table at the end of [docs/deploy.md](docs/deploy.md).
- Symptom: any `docker` command says it cannot connect to the daemon. Fix: start Docker Desktop and wait until it reports running. `make db-status` also needs a running `db` service, because it connects to the published port with the values in `.env`.

### Observability profile (`make up-obs`)

- Symptom: `make up-obs` waits a long time on `grafana`. Cause: the first start on an empty `grafana-data` volume runs about 700 database migrations and registers the bundled plugins; the healthcheck allows 180 s for it. Later starts take seconds. Fix: wait, or read `docker compose logs grafana`.
- Symptom: `docker stats` shows `otel-collector` or `grafana` at 90 to 100 % of its limit. Cause: the figure includes memory-mapped program files (the contrib collector binary is about 200 MB), which the kernel drops under pressure; it is not an out-of-memory condition. Check `docker inspect --format '{{.State.OOMKilled}}' <container>` instead; the measurements are in `docs/evidence/phase-0/task-10.1-notes.md`.
- Symptom: Prometheus has no `traces_span_metrics_calls_total` series. Cause: the api and worker were started by plain `make up`, so they export nothing. Fix: run `make up-obs`, call `/readyz` once, wait for the next 15 s scrape.
- Symptom: after `make down-obs` the api and worker log OTLP export errors. Cause: they still export to the removed collector. Fix: `make up` recreates them with the exporter off.

### Other common failures

- `uv sync --locked` fails with a lock error: `uv.lock` is out of date with `pyproject.toml`. Run `uv lock` and review the diff; do not use `--frozen` to get past it.
- `make web-check` fails to pull the Node image: Docker is not running or offline. Use `WEB_RUNNER=host` only if Node 24 is installed (user step U7).
- `make docs-check` fails on a link: the finding names the file and line. Run `make docs-check DOCS_ONLY=links` while you fix it.
- `make actionlint` fails with "no workflow files": `.github/workflows/ci.yml` is missing or was moved. Restore it.
- `make gates` stops at the first failing gate and leaves the stack running. Fix the gate, run `make <gate>` alone, then `make gates` again.

## User-owned steps

These steps stay with the project owner. Agents and scripts never run `git commit` or `git push`, and never perform the steps below.

| Id | Step | When |
|---|---|---|
| U1 | Give Docker Desktop at least 4 GB of memory (Settings, Resources). Verify the current setting. | Before the database tasks |
| U2 | Run `git init -b main` in the repo, then `uv run pre-commit install`. | After the skeleton exists |
| U3 | Create the demo GitHub organization and confirm that private repositories and teams work on its plan (plan item 0.10, Verify). Record the result in `docs/evidence/phase-0/github-org.md`. | Phase 0, by 29 Oct |
| U4 | Commit the work, create the remote repository, push, confirm CI is green on main, and merge ADR-0001 to ADR-0005 and ADR-0013 to ADR-0015. | End of Phase 0 |
| U5 | Tick items in `development_plan.md` and add progress-log rows that link `docs/evidence/phase-0/`. | Each working day |
| U6 | Choose the repository license before the first push. Recommendation: Apache-2.0. Grafana (obs profile only) is an AGPL-3.0 service run as a separate container, which does not change the license of this repository; the database image `pgvector/pgvector` is under the PostgreSQL License. Verify the AGPL obligations before any public deployment of the obs profile, and read the terms of the hosted services and models in [docs/licenses.md](docs/licenses.md). | Before U4 |
| U7 | Optional: install Node 24 on the host for `pnpm dev` and `WEB_RUNNER=host`. | Any time |
| FU1 | Create the Neon account and a project on PostgreSQL 18; run `make env-neon`; copy the direct endpoint host and the `neondb_owner` password into `.env.neon`. | Before FU2 |
| FU2 | Run `make remote-init` and `make remote-status` from the laptop. | After FU1 |
| FU3 | Create the Cloudflare account, note the account id, create an API token scoped to Workers AI only. | Any time |
| FU4 | Create the Render account; after U4, create the Blueprint from `render.yaml` and enter `CAIRN_DB_HOST`, the app and DBOS passwords from `.env.neon`, `CLOUDFLARE_ACCOUNT_ID`, `CLOUDFLARE_API_TOKEN` and a new `CAIRN_PROXY_TOKEN`. | After FU2, FU3 and U4 |
| FU5 | Trigger the first Render deploy and confirm `/healthz`. | After FU4 |
| FU6 | Create the Pages project, set `API_ORIGIN` in `apps/web/wrangler.toml` to the Render URL, run `make web-dist`, deploy with the pinned wrangler, set the `CAIRN_PROXY_TOKEN` Pages secret to the Render value, open `/api/readyz`. | After FU5 |
| FU7 | Remove the Phase 0 volume with `docker volume rm cairn_pgdata` once satisfied (nothing in the repository does it). | After the store swap |
| FU8 | Optional: put a Workers AI token in `.env` and run `make workers-ai-live-check`. | After FU3 |
| FU9 | Tick plan items and add progress-log rows, as in U5. | Each working day |

FU1 to FU9 belong to the free-tier deployment. Each has a section with exact commands and checks in [docs/deploy.md](docs/deploy.md). Agents never create accounts, enter secrets, connect to Neon, Render or Cloudflare with your credentials, or deploy; they print the step id and continue.
