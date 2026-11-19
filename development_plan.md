# Cairn development plan: phase-wise checklist

> Derived from [brainstorm.md](brainstorm.md): its [decision log](brainstorm.md#1-tldr-and-decision-log), [roadmap](brainstorm.md#18-roadmap-27-oct-to-20-nov-2026) and [evaluation plan](brainstorm.md#16-evaluation-plan).
> Window: 27 Oct to 20 Nov 2026, in seven phases. Prepared 27 Oct 2026. Revised 1 Nov 2026 for the free-tier deployment target (ADR-0013, ADR-0014, ADR-0015).
> Status: the Phase 0 skeleton (items 0.1 to 0.24) was built locally 27 to 30 Oct on the ParadeDB image and is being retargeted to free tiers (items 0.25 to 0.54). Phases 1 to 6 have not started. Ticking items and adding progress-log rows stay with the user.
> Decisions D1 to D20 are final for the MVP. Changing one needs an ADR. ADR-0013 supersedes ADR-0001 (D5 to D7), ADR-0014 amends D18 and ADR-0015 amends D16.

## How to use this plan

- Work top to bottom. Each phase has a goal, deliverables, numbered checklist items grouped Build, Test, Docs and Demo, a measurable exit gate and a "cut if behind" list.
- Items are numbered phase.item (for example 1.12), so commits, the progress log and assistant prompts can reference them. Numbers are stable: an item added later takes the next free number in its phase and sits in its group, so numbers inside a group are not always consecutive.
- Tick items in place (`- [ ]` becomes `- [x]`) in the same commit as the work, and add a row to the [progress log](#progress-log) at the end of each working day.
- A phase is done only when its exit gate passes. If a phase runs a day late, apply the [cut-line ladder](#cut-line-ladder) before borrowing time from the next phase.
- The retarget extended Phase 0 past 29 Oct. Phases 1 to 6 keep their planned dates; the slip is absorbed with the cut-line ladder, and the progress log records any re-dating.
- Every item must fit the [deployment target](#deployment-target) and its design rules. An item that cannot fit needs an ADR that changes the rule, or a cut.
- Spike results can reopen D1, D3, D5 to D8, D11, D13 and D16. Record each result in the [spike tracker](#spike-tracker) and in an ADR before building on it.
- When delegating to a coding assistant, hand over one item or a small group by number, with its tests and the linked brainstorm section, and follow the AI-assisted workflow rules in [section 18](brainstorm.md#18-roadmap-27-oct-to-20-nov-2026).
- Cairn's own ADRs use four digits (ADR-0001). Kestrel Pay scenario decisions use three (ADR-007). Numbers 0006, 0007, 0008, 0011 and 0012 belong to Phase 1, 0009 and 0010 to the spike tracker, and 0013 to 0015 to the free-tier retarget. The next free number is ADR-0016.
- Labels follow the brainstorm: Proposed target is an acceptance target, Estimate is a planning number and Verify is a fact still to confirm.

## Deployment target

Production runs on free plans only (ADR-0014). The laptop runs the same code in compose: profile `core` for development, and profile `freetier`, which emulates the Render limits and Neon's owner role so CI can test them.

| Piece | Production (free plan) | Local |
|---|---|---|
| Web | Cloudflare Pages static build. A Pages Function forwards same-origin `/api/*` calls to Render and adds the `X-Cairn-Proxy-Token` header | `web` (nginx with an `/api/` proxy), profile `core` |
| API and jobs | One Render free web service (Docker) running `cairn-combined`: FastAPI and DBOS in one process | Split `api` and `worker`, profile `core`; `ft-app` running `cairn-combined`, profile `freetier` |
| Database | Neon free, PostgreSQL 18 with pgvector and row-level security; admin role `neondb_owner`, bootstrapped in managed mode from the laptop (ADR-0013) | `pgvector/pgvector` PostgreSQL 18 image pinned by digest; `db` in superuser mode, `ft-db` in managed mode |
| Models | Cloudflare Workers AI over REST: embeddings `bge-small-en-v1.5` (384 dimensions), rerank `bge-reranker-base`, answer `gemma-4-26b-a4b-it`, judge `glm-4.7-flash`; Anthropic and OpenAI optional with the user's key (ADR-0015) | Deterministic fakes by default; Workers AI only when the user puts a token in `.env` |

```mermaid
flowchart LR
  B["Browser"] --> P["Cloudflare Pages<br/>static web build"]
  B -->|"same origin /api/*"| F["Pages Function<br/>adds X-Cairn-Proxy-Token"]
  F --> R["Render free web service<br/>cairn-combined: FastAPI + DBOS<br/>512 MB, 0.1 CPU"]
  R --> N[("Neon free<br/>PostgreSQL 18, pgvector, RLS")]
  R --> W["Workers AI<br/>embed, rerank, answer, judge"]
  G["GitHub webhooks, Phase 3"] --> E["Edge webhook receiver<br/>Pages Function"]
  E --> N
  E -.->|"wake"| R
  L["Laptop: make remote-init<br/>owner credentials stay here"] --> N
```

Free-tier limits that constrain the plan. The user gathered them on 30 Oct 2026; the retarget's Verify item FV0 records each with its URL in `docs/evidence/free-tier/verify/`.

| Service | Limit | Source |
|---|---|---|
| Render free web service | 512 MB RAM and 0.1 CPU; sleeps after 15 min without inbound traffic and takes about 1 min to wake; 750 instance-hours per workspace per month; no free background workers or cron jobs; free Render Postgres expires after 30 days (not used) | Render docs on free instances (FV0) |
| Neon free | No time limit; 1 GB storage and 100 CU-hours per project per month; 5 GB egress; scales to zero after 5 min and resumes in a few hundred ms; pgvector supported; `pg_search` retired on 21 Sep 2026; `neondb_owner` has BYPASSRLS and is not a superuser | Neon plan docs, the `pg_search` retirement notice and the database access docs (FV0) |
| Cloudflare | Pages: unlimited static requests, 500 builds per month, 20,000 files. Workers free: 100,000 requests per day, 10 ms CPU per request. Queues free: 10,000 operations per day, 24 h retention. Containers need the paid plan (not used). Workers AI: 10,000 neurons per day free; some large models paid-only since 28 Jul 2026 | Cloudflare Pages, Workers, Queues and Workers AI pricing docs and the 28 Jul 2026 changelog (FV0) |
| Rejected hosts | Oracle Always Free A1 was halved to 2 OCPU and 12 GB in June 2026 and reclaims idle instances. Supabase free pauses after 7 days idle | User research, 30 Oct 2026 (FV0) |

Design rules these limits imply. Each item below follows them:

1. Fit 512 MB. Production is one process. The combined process stays at or below 300 MiB idle peak and 384 MiB small-load peak inside the 512 MiB limit, with no OOM kill and no restart (Proposed target, ADR-0014). `make freetier-check` proves it on every gate run.
2. No in-process models. Embeddings, rerank, answers and judging go to Workers AI or an optional paid provider over HTTPS. No model files in any image (ADR-0015).
3. No always-on worker. Jobs run inside `cairn-combined`; nothing may assume a separate worker container or a cron in production. Local `core` keeps the split `api` and `worker` for development.
4. Sleep and wake. The service stops after 15 min idle and nothing runs while it sleeps. Periodic jobs are idempotent and catch up on start. No keep-alive pinger (ADR-0014): one would keep Neon awake and spend about 186 of its 100 CU-hours a month. Work that cannot wait for a wake, such as receiving a GitHub webhook, runs at the edge.
5. Neuron budget. Every Workers AI call reserves its estimated neurons first, against a cap of 9,000 per UTC day (90% of the free 10,000), and is refused past the cap (ADR-0015). Every item that adds model calls estimates their neuron cost.
6. Neon storage of 1 GB. Only the frozen demo corpus is deployed. Spike and evaluation corpora stay local, and the spike commands refuse remote hosts (ADR-0013).
7. Neon compute of 100 CU-hours a month, about 400 awake hours at 0.25 CU (Estimate). Nothing keeps the database awake.
8. Edge quotas. Static requests are free; Functions run only on `/api/*` and the webhook path, within 100,000 requests a day and 10 ms CPU each.
9. No paid dependency. Every hosted service stays on a free plan, and paid model providers stay optional.
10. Credentials. The Neon owner and admin passwords stay on the laptop. Every hosted runtime connection uses a role without BYPASSRLS (ADR-0013).

| Phase | Dates | Exit gate in short |
|---|---|---|
| [0. Foundations](#phase-0-foundations-27-to-29-oct) | 27 to 29 Oct; built 27 to 30 Oct, retarget in progress | CI green, ADRs merged, scenario validates, free-tier gate passes |
| [1. Evidence search](#phase-1-evidence-search-30-oct-to-2-nov) | 30 Oct to 2 Nov | Persona switch changes results, 0 canary leaks, Recall@5 recorded, fits 512 MB |
| [2. Decision memory](#phase-2-decision-memory-3-to-6-nov) | 3 to 6 Nov | Overlaps rejected, as-of accuracy recorded, projection tests pass |
| [3. Live sync and permissions](#phase-3-live-sync-and-permissions-7-to-10-nov) | 7 to 10 Nov | Revocation p95 <= 60 s measured, deletion and drain-before-serve tests pass |
| [4. Experience and agents](#phase-4-experience-and-agents-11-to-14-nov) | 11 to 14 Nov | MCP passes the matrix, injected issue causes no leak or action |
| [5. Evaluation](#phase-5-evaluation-15-to-17-nov) | 15 to 17 Nov | One command regenerates every results table, within the neuron cap |
| [6. Ship](#phase-6-ship-18-to-20-nov) | 18 to 20 Nov | Every MVP exit criterion checked on the free-tier deployment |

```mermaid
flowchart LR
  P0["Phase 0 Foundations and free-tier retarget"] --> P1["Phase 1 Evidence search"]
  P1 --> P2["Phase 2 Decision memory"]
  P2 --> P3["Phase 3 Live sync and permissions"]
  P3 --> P4["Phase 4 Experience and agents"]
  P4 --> P5["Phase 5 Evaluation"]
  P5 --> P6["Phase 6 Ship"]
  SA["Spikes S1, S2, S3"] -.-> P1
  SB["Spike S4"] -.-> P2
  SC["Spike S5"] -.-> P3
  SD["Spike S6 (optional)"] -.-> P5
```

## Global definition of done

Every checklist item meets all of these before it is ticked:

- [ ] Merged through a PR with passing CI.
- [ ] Unit tests for new logic, plus an integration test for every new path that touches the database, a source or a model. CI has no provider token, so model paths use the deterministic fakes or cached embeddings there.
- [ ] Permission matrix still passes with 0 observed leaks, and every new query path is added to the matrix.
- [ ] Eval smoke not regressed beyond the gate tolerance below.
- [ ] Runs inside the free-tier emulation within the memory budget: `make freetier-check` passes with the new code loaded in `cairn-combined` (ADR-0014).
- [ ] Adds no paid dependency: every hosted service stays on a free plan, and any paid model provider stays optional (ADR-0014, ADR-0015).
- [ ] Neuron cost estimated for every new Workers AI call, per call and per day, and the call goes through the neuron budget (ADR-0015).
- [ ] Docs updated, and an ADR exists for any new or changed decision.
- [ ] Traces and structured logs visible for new endpoints and workflow steps.
- [ ] No secrets, tokens or real company data in code, fixtures, prompts or logs.
- [ ] Status reported as implemented, tested and documented, separately.

## Quality gates

`make gates` runs these locally in the same order as CI. The core stack is stopped before the `freetier` profile starts, because the two do not fit the 3.83 GiB Docker VM together.

| Gate | Tooling | Runs on | Blocks merge when |
|---|---|---|---|
| Format and lint | ruff, eslint, prettier | Every push | Any error |
| Types | mypy (strict in packages/core), tsc (web and the Pages Functions) | Every push | Any error |
| Unit tests | pytest, vitest (including the Pages Function proxy) | Every push | Any failure |
| Integration | pytest against compose core with the pgvector PostgreSQL 18 image (ADR-0013) | Every PR | Any failure |
| Migrations | Apply all migrations to a fresh database | PRs with migrations | Any failure |
| Permission matrix | pytest marker `matrix` | PRs touching api, worker, combined, core, retrieval or mcp; full run nightly | Any observed leak |
| Managed-mode matrix | `make matrix-managed`: role, default-deny and bootstrap tests against `ft-db` as the non-superuser admin `cairn_admin`, shaped like Neon's `neondb_owner` (ADR-0013) | Every PR, inside the free-tier gate | Any failure or observed leak |
| Free-tier gate | `make freetier-check`: `freetier` profile up, smoke, managed-mode matrix, RAM check, down (ADR-0014) | Every PR; part of `make gates` and `make phase0-exit` | Any failure, a RAM peak over the budget, an OOM kill or a restart |
| Eval smoke | 20 C1 dev questions: deterministic checks plus Recall@5 from cached embeddings. CI has no Cloudflare token, so a cache miss fails with a message to refresh the cache locally, and answer checks use the fakes (ADR-0015) | PRs touching retrieval, prompts or models | A deterministic failure, or Recall@5 down more than 0.05 (Proposed target) |
| Dependency and secret scan | pip-audit, npm audit, a secret scanner, and checks that `render.yaml` and `wrangler.toml` hold no secret values | Every PR | High-severity finding or any secret |

## Phase 0. Foundations (27 to 29 Oct)

Goal: lock decisions, contracts and the scenario, and get a running skeleton, so later phases only add behavior. Then retarget the skeleton to the free-tier deployment (ADR-0013 to ADR-0015) before Phase 1 builds on it.

Deliverables: PRD v1, ADR-0001 to ADR-0005, threat model v1, acceptance test list, Kestrel Pay scenario YAML v1 with a validator, repo skeleton with CI, compose core profile with health endpoints, S1 and S2 harnesses. From the retarget: the pgvector store with native full-text search and ADR-0013, managed-mode bootstrap, the combined process, the `freetier` profile and `make freetier-check` in the gates, the Workers AI client with the neuron budget and provider interfaces (ADR-0015), and the deploy artifacts with their runbook (ADR-0014).

Items 0.1 to 0.24 were built locally 27 to 30 Oct on the ParadeDB image. The retarget rewrote 0.4, 0.5, 0.14, 0.16 and 0.24 (ADR-0013, ADR-0014) and adds 0.25 to 0.54.

Build:

- [x] 0.1 Create the repo layout from [section 9](brainstorm.md#9-architecture-and-stack-decisions): apps/api, apps/worker, apps/web, apps/mcp, packages/core, packages/connectors, packages/retrieval, packages/eval, scenarios/kestrel_pay, docs/adr.
- [x] 0.2 Python 3.12 workspace: pyproject, locked dependencies with hashes, ruff, mypy, pytest and pre-commit.
- [x] 0.3 Web scaffold with React, TypeScript, Vite and TanStack Query (D19), plus eslint, prettier and vitest.
- [x] 0.4 docker-compose.yml with profiles core, obs, experiments and freetier (D18, amended by ADR-0014). Core runs the pgvector PostgreSQL 18 image pinned by digest, api, worker and web. First built on the ParadeDB image; 0.25 swaps it (ADR-0013).
- [x] 0.5 Database bootstrap: extensions vector (at least 0.8.0) and btree_gist; a migration owner role and a non-owner app role without BYPASSRLS ([section 11](brainstorm.md#11-permission-model)). First built with pg_search; 0.25 removes it and 0.26 adds the managed mode Neon needs (ADR-0013).
- [x] 0.6 Migration tooling with a baseline migration.
- [x] 0.7 FastAPI app with /healthz and /readyz (database reachable, extension versions), and a worker that launches DBOS and runs a no-op scheduled workflow (D10).
- [x] 0.8 OpenTelemetry tracing, with trace ids in structured JSON logs.
- [x] 0.9 CI workflow running every quality gate that applies so far.
- [ ] 0.10 Create the demo GitHub org now and confirm private repos and teams work on its plan (Verify, open question 9).
- [x] 0.11 Kestrel Pay scenario YAML v1 from [section 17](brainstorm.md#17-synthetic-company-kestrel-pay): people, teams, repos, channels, ADR-007 to ADR-019 with their twists, canary strings, and a JSON Schema for the file.
- [x] 0.12 Scenario validator script.
- [x] 0.13 Start S1: synthetic chunk generator with ACL labels at 1, 5, 20 and 50% selectivity.
- [x] 0.14 Start S2: FORCE RLS with the app role, then EXPLAIN ANALYZE on lexical, HNSW and hybrid queries. The 27 Oct run used ParadeDB BM25; 0.27 re-runs it on native full-text search (ADR-0013).

Test:

- [ ] 0.15 CI green on main with the skeleton.
- [x] 0.16 Integration smoke: compose core starts, and /readyz lists vector and btree_gist (pg_search until the retarget, ADR-0013).
- [x] 0.17 Role test: the app role owns no tables, lacks BYPASSRLS, and reads zero rows from an RLS-enabled table that has no policy (default deny).
- [x] 0.18 Validator test: passes on scenario v1 and fails on three broken fixtures (a twist without gold questions, an inconsistent date, an unplaced canary).

Docs:

- [x] 0.19 PRD v1 (docs/prd.md) from [sections 4](brainstorm.md#4-problem-users-and-jobs-to-be-done), [7](brainstorm.md#7-feature-brainstorm-prioritization-and-mvp) and [8](brainstorm.md#8-journeys-ux-concepts-and-success-metrics): problem, personas, jobs, US1 to US9, non-goals and metrics.
- [x] 0.20 ADR-0001 store (D5 to D7), ADR-0002 jobs (D10), ADR-0003 temporal model (D9), ADR-0004 authorization layers (D11, D12) and ADR-0005 evaluation protocol (D17), in MADR 4.0 format with a Confirmation section. ADR-0001 is superseded by ADR-0013 in 0.41.
- [x] 0.21 Threat model v1 (docs/threat-model.md) from [section 15](brainstorm.md#15-threat-model), with one test id per threat.
- [x] 0.22 Acceptance test list (docs/acceptance-tests.md) mapping US1 to US9 and the five MVP exit criteria to named tests.
- [x] 0.23 CONTRIBUTING.md with the definition of done and the AI-assisted workflow rules.

Demo:

- [x] 0.24 `docker compose --profile core up` comes up healthy on the laptop. Record RAM use against the about 4 GB Estimate. The 30 Oct report on the ParadeDB image peaked at 0.22 GiB; 0.37 re-measures on the pgvector image.

### Free-tier retarget (ADR-0013 to ADR-0015)

Build:

- [x] 0.25 Store swap (ADR-0013): the `db` service runs `pgvector/pgvector` PostgreSQL 18 pinned by digest to the pgvector version Neon offers (Verify), on a new volume `pgdata-pgvector`. The ParadeDB-era volume `cairn_pgdata` is left unmounted for the user to remove (0.52). pg_search leaves bootstrap, readiness, the tests and the license report; `0001_baseline.sql` is edited once in place, the one exception to forward-only migrations; readiness adds `extensions_outdated` for vector below 0.8.0.
- [x] 0.26 Managed-Postgres bootstrap mode (ADR-0013): `superuser` and `managed` admin modes, detected from `rolsuper` and overridable with `CAIRN_DB_ADMIN_MODE`. In managed mode the admin creates the roles with CREATEROLE, grants itself SET on `cairn_owner` and `cairn_dbos`, and creates extensions under `SET ROLE cairn_owner`. TLS settings (`CAIRN_DB_SSLMODE`, `CAIRN_DB_SSLROOTCERT`) reach every connection, and the spike commands refuse non-local database hosts without `--allow-remote`.
- [x] 0.27 S2 on native full-text search (ADR-0013): a stored generated tsvector column with a GIN index, queried with `websearch_to_tsquery` and ranked with `ts_rank_cd`; the BM25 variants become FTS variants and the plan analysis reads the GIN index. Re-run at 20,000 rows and report next to the 27 Oct BM25 report.
- [x] 0.28 Combined process (ADR-0014): `apps/combined` (`cairn-combined`) serves FastAPI with uvicorn and launches DBOS and the heartbeat schedule from a background jobs runner that retries with backoff. `/healthz` checks the process only; `/readyz` adds a jobs check. The heartbeat runs every 5 minutes without backfill, and stops while the service sleeps.
- [x] 0.29 Proxy token (ADR-0014): when `CAIRN_PROXY_TOKEN` is set, the API checks `X-Cairn-Proxy-Token` in constant time and answers 404 `not_found` without it; `/healthz` is exempt so Render can probe it. The value is never logged.
- [x] 0.30 `freetier` compose profile (ADR-0014): `ft-db` (tmpfs data) where the image superuser creates only `cairn_admin`, shaped like `neondb_owner`; `ft-db-init` bootstraps in managed mode as `cairn_admin`; `ft-app` runs `cairn-combined` with 512 MB memory, no swap, 0.1 CPU, `PORT=10000` and the proxy token on.
- [x] 0.31 `make freetier-check` (up, smoke, managed-mode matrix, RAM check, down) in `make gates` after `make down`, in the CI integration job and in `make phase0-exit` (ADR-0014).
- [x] 0.32 Workers AI REST client in `cairn_retrieval.cloudflare` on httpx (ADR-0015): at most 3 attempts with jittered backoff on 429 and 5xx, `Retry-After` honored up to 30 s, no retry on the daily-allocation error, and no token, URL or payload text in logs or errors.
- [x] 0.33 Neuron budget guard (ADR-0015): each attempt reserves its estimated neurons before the call; the cap is `CAIRN_WORKERS_AI_DAILY_NEURONS` (default 9,000 per UTC day); stores in memory for tests and in Postgres (`cairn_meta.provider_usage`, migration 0002) for the combined process, so a restart after sleep does not reset the count.
- [x] 0.34 Provider interfaces (ADR-0015): async `Embedder`, `Reranker` and `LlmProvider` with Workers AI adapters and deterministic fakes. Defaults `bge-small-en-v1.5` (384 dimensions), `bge-reranker-base`, `gemma-4-26b-a4b-it` for answers and `glm-4.7-flash` for judging; configuration rejects an answer and judge from the same model family. `.env.example` and CI use the fakes. `make workers-ai-live-check` is user-run only (0.53).
- [x] 0.35 Deploy artifacts (ADR-0014): `render.yaml` (free plan, Docker, `cairn-combined`, health check `/healthz`, every secret `sync: false`); the Pages config in `apps/web` (`wrangler.toml`, `_routes.json` limited to `/api/*`, `_headers`) and the Function `functions/api/[[path]].ts` that strips `/api`, sets the proxy token, drops any client copy and answers 503 `api_unavailable` with `Retry-After` after a 90 s upstream timeout; `.env.neon.example` and the user-run targets `env-neon`, `remote-init` and `remote-status`; `make web-dist` for the Pages upload.
- [x] 0.36 `make env-update` appends missing template keys to `.env` with fresh secrets and never rewrites an existing line, so local settings such as `CAIRN_DB_PORT=55432` survive (ADR-0014).

Test:

- [x] 0.37 The Phase 0 integration, matrix, migrations-check and smoke suites pass on the pgvector image, /readyz lists exactly vector and btree_gist with schema head 2, and the core RAM report is re-measured.
- [x] 0.38 Managed-mode matrix: the role and default-deny tests pass against `ft-db` as `cairn_admin`; the admin is not a superuser, does bypass RLS, and is never a runtime role.
- [x] 0.39 Free-tier smoke and RAM check: `/healthz` is 200 without the proxy header and `/readyz` is 404 without it and 200 with it; the heartbeat runs; `ft-app` has 512 MB, no swap, 0.1 CPU and only the runtime passwords; idle peak <= 300 MiB, small-load peak <= 384 MiB, no OOM kill or restart (Proposed target).
- [x] 0.40 Provider and deploy tests: Workers AI contract tests through a mock transport, the budget never admits past its cap, the fakes are deterministic, the Pages proxy passes its vitest table, and `render.yaml` holds no secret value.

Docs:

- [x] 0.41 ADR-0013 (store on PostgreSQL 18 with pgvector and native full-text search, Neon in production; supersedes ADR-0001), ADR-0014 (Cloudflare Pages, one Render free web service in combined mode, Neon free) and ADR-0015 (Workers AI models behind provider interfaces, with a neuron budget); ADR-0001 marked superseded; the ADR index and the docs checker updated.
- [x] 0.42 Threat model: TM-11 adds the Neon owner role, TM-13 adds Workers AI as a processor of chunk text, new TM-15 covers hosting providers and hosted secrets, new TM-16 covers free-tier exhaustion. Acceptance list notes on the demo deployment, the PRD stack sentence, CONTRIBUTING targets and troubleshooting, and the license report (Grafana in the obs profile is the only AGPL component left).
- [x] 0.43 docs/deploy.md runbook: topology and limits with sources, Neon, Cloudflare and Render setup, first deploy, the end-to-end check through `/api/readyz`, operations (sleep and wake, Neon CU-hours, the neuron budget, password rotation, teardown) and troubleshooting. Each user step is marked with its FU id.
- [x] 0.44 Source documents: targeted edits to brainstorm.md, this plan revised for the free-tier target, and `docs/evidence/phase-0/source-docs.sha256` regenerated.

Demo:

- [x] 0.45 `make freetier-check` passes on the laptop, and its RAM report is saved in `docs/evidence/free-tier/`.

User-run deploy steps. These are user-owned: no agent runs or ticks them, and `make phase0-exit` prints them as not automated.

- [ ] 0.46 FU1 (user-owned): create the Neon account and a PostgreSQL 18 project in the chosen region (default `aws-ap-southeast-1`, next to Render `singapore`); run `make env-neon` and put the direct endpoint host and the `neondb_owner` password in `.env.neon`.
- [ ] 0.47 FU2 (user-owned): run `make remote-init` and `make remote-status` from the laptop.
- [ ] 0.48 FU3 (user-owned): create the Cloudflare account, note the account id, and create an API token scoped to Workers AI only.
- [ ] 0.49 FU4 (user-owned): after the repo is pushed (Phase 0 step U4), create the Render Blueprint from `render.yaml` and enter `CAIRN_DB_HOST`, the app and DBOS passwords from `.env.neon`, the Cloudflare account id and token, and a new `CAIRN_PROXY_TOKEN`.
- [ ] 0.50 FU5 (user-owned): trigger the first Render deploy and confirm `/healthz`.
- [ ] 0.51 FU6 (user-owned): create the Pages project, set `API_ORIGIN` in `apps/web/wrangler.toml` to the Render URL, run `make web-dist`, deploy with the pinned wrangler, set the `CAIRN_PROXY_TOKEN` Pages secret to the Render value, and open `/api/readyz`.
- [ ] 0.52 FU7 (user-owned): remove the ParadeDB-era volume with `docker volume rm cairn_pgdata` once satisfied with the new store.
- [ ] 0.53 FU8 (user-owned, optional): put a Workers AI token in `.env` and run `make workers-ai-live-check`.
- [ ] 0.54 FU9 (user-owned): tick plan items and add progress-log rows each working day.

Exit gate:

- EG1: CI green on main, and ADR-0001 to ADR-0005 and ADR-0013 to ADR-0015 merged, with ADR-0001 marked superseded by ADR-0013.
- EG2: Scenario YAML v1 passes the validator, and the validator rejects all three broken fixtures.
- EG3: /readyz healthy in compose core on the pgvector image, and the role test passes.
- EG4: S1 and S2 harnesses committed, with the S2 re-run on native full-text search recorded.
- EG5: `make freetier-check` passes: free-tier smoke, managed-mode matrix and the combined-process RAM budget (ADR-0014).
- The user-run steps 0.46 to 0.54 are not part of the gate. The hosted deploy (0.46 to 0.51) is needed by the Phase 1 demo and by Phase 6 at the latest.

Cut if behind: the web scaffold beyond a placeholder page, the obs profile, CONTRIBUTING polish, the optional live Workers AI check (0.53), and the hosted deploy steps 0.48 to 0.51, which can wait for Phase 6. Never cut the ADRs, the role test, the scenario validator, the managed-mode matrix or the free-tier gate.

## Phase 1. Evidence search (30 Oct to 2 Nov)

Goal: cited, permission-filtered search and answers over imported Kestrel Pay content, with the B0 and B1 baselines measured, running inside `cairn-combined` within the free-tier memory budget.

Deliverables: importers that write items and revisions with content hashes, span-preserving chunks, Workers AI embeddings through the neuron budget (ADR-0015), native full-text search and HNSW indexes (ADR-0013), principals and ACL tables, the ACL predicate with an RLS backstop, fusion and a hosted rerank, a basic cited answer from the default Workers AI model, persona dev login locally and a demo persona mode on the deployment, an eval harness with B0 and B1 on 30 questions and a cached-embeddings mode, canary tests v1, and the Phase 1 jobs running in the combined process (ADR-0014).

Build:

- [ ] 1.1 Finish S2 by 30 Oct on native full-text search (0.27) and record RLS-first or SECURITY DEFINER retrieval functions in ADR-0006 (open question 2).
- [ ] 1.2 Run S3 on 30 Oct: choose the embedding model and reranker from the Workers AI catalog (defaults `bge-small-en-v1.5` at 384 dimensions and `bge-reranker-base`, ADR-0015), measuring Recall@5 on 20 dev questions, latency from the Render region and neurons per query, and record them in ADR-0007 (D16, open questions 3 and 4).
- [x] 1.3 Scenario renderers v1: Slack-export-format chat JSON, meeting notes as Markdown with front matter, and docs and ADR files in a local folder. GitHub objects follow in Phase 3.
- [x] 1.4 Importers for chat exports, meeting notes and Markdown or ADR files into items and revisions, with a normalized content hash so unchanged content is skipped.
- [x] 1.5 Span-preserving chunker that keeps offsets into the revision and respects headings and thread boundaries.
- [x] 1.6 Embedding workflow on DBOS, enqueued through the transactional outbox (ADR-0011) because DBOS has its own database (ADR-0014). It calls the Workers AI embedder through the neuron budget in batches of at most 50 and stores the model key and dimension on every row; content with an unchanged hash and model key is never re-embedded.
- [x] 1.7 Indexes: native full-text search with stored generated tsvector columns (`tsv_en`, english configuration over title and body; `tsv_simple`, simple configuration over body) and GIN indexes, queried with `websearch_to_tsquery` and ranked with `ts_rank_cd`, plus pgvector HNSW with iterative scans (ADR-0013).
- [x] 1.8 Access tables: principals, memberships with valid_period WITHOUT OVERLAPS, item_acls and acl_versions. Importers write ACLs from channel membership and front matter.
- [x] 1.9 L1 principal snapshot, L2 ACL predicate in every retrieval query, and L3 RLS policies with FORCE and SET LOCAL principals, or the S2 fallback.
- [x] 1.10 Retrieval: full-text and dense candidates, reciprocal rank fusion, the hosted cross-encoder rerank (`bge-reranker-base` on Workers AI, ADR-0015) and token-budget packing. B0 is dense-only; B1 is full-text plus dense plus RRF plus rerank. When the reranker fails or the budget is used up, the fused order is kept and the response records it.
- [x] 1.11 Answer endpoint that composes only from packed evidence with citations, using the default Workers AI answer model or an optional Anthropic or OpenAI key (ADR-0015), plus the standard "not found in sources you can access" reply. A provider error or an exhausted budget returns 503 with a fixed code, never an uncited answer.
- [x] 1.12 Persona dev login for Asha, Ben, Carla and Priya, enabled in the local profile and, on the deployed stack (ADR-0014), only in demo persona mode: `CAIRN_ENV=demo`, synthetic Kestrel Pay data only, a visible "demo personas, synthetic data" banner, no real accounts, a session lifetime cap and a per-client rate limit. The rule is recorded in an ADR before the first deploy.
- [x] 1.13 Audit log v1: append-only rows with question hash, principal snapshot, returned item ids and answer hash.
- [ ] 1.14 Pick the answer model and judge model pair on 20 questions and record it in ADR-0012 (open question 5). The default is the Workers AI pair `gemma-4-26b-a4b-it` (answer, Google) and `glm-4.7-flash` (judge, Zhipu), from different families (D16, ADR-0015); Anthropic and OpenAI models are optional with the user's key. Set the cost budget per answer in neurons (Estimate: about 37 per answer, about 61 per judged answer). Run the comparison with the fakes first, and spread it over more than one day if it would pass the 9,000-neuron daily cap.
- [x] 1.15 Eval harness v1: question file format, B0 and B1 runners, Recall@5, results written to Parquet, and a cached-embeddings mode that stores corpus and question embeddings by content hash and model key, so re-runs and the CI eval smoke spend no neurons and need no token.
- [x] 1.16 Minimal Ask page with a persona switcher, the answer and its citations. In production it is served from Cloudflare Pages and shows a "waking the API" state on 503 `api_unavailable`.
- [x] 1.17 Finish S1 by 2 Nov on the local database (the spike commands refuse remote hosts, ADR-0013) and record the vector ACL strategy in ADR-0008 (D7, open question 1).
- [x] 1.28 Run the Phase 1 jobs inside `cairn-combined` (ADR-0014): the outbox drainer and the embedding workflows start from the jobs runner after DBOS launches, and the drainer sweeps once on every start because the service sleeps. The `cairn_ingest` role is created by the same managed-mode bootstrap (ADR-0013). Local core keeps the split api and worker at 384 MB each.

Test:

- [x] 1.18 Importer idempotency: a re-import creates no revisions, and one edit creates exactly one.
- [x] 1.19 Every citation offset resolves to the exact source text.
- [x] 1.20 Query registry test that fails if any retrieval SQL path lacks the ACL predicate.
- [x] 1.21 RLS backstop: with the L2 predicate removed in a test build, the app role still sees only allowed rows, and with no principal setting it sees none.
- [x] 1.22 Canary matrix v1: every persona on search and answer, with and without access. HR and partner canaries never appear.
- [x] 1.23 For the same question, the not-found reply is identical whether or not hidden matches exist.
- [ ] 1.24 B0 and B1 on 30 C1 dev questions, with Recall@5, seeds, model keys and neurons spent recorded.
- [x] 1.29 `make freetier-check` passes with the Phase 1 code loaded, and `make matrix-managed` covers `cairn_ingest`: the combined process stays within the memory budget (idle peak <= 300 MiB, small-load peak <= 384 MiB) with no OOM kill (Proposed target, ADR-0014).
- [x] 1.30 Provider failure paths: with the fakes set to fail, and with the budget at its cap, search still returns fused full-text and dense results, the answer endpoint returns 503 with a fixed code, and no call is made past the cap (ADR-0015).

Docs:

- [x] 1.25 docs/retrieval.md: pipeline, full-text and HNSW indexes, the models with their neuron costs, and the S1 numbers behind the chosen filter strategy.
- [ ] 1.26 ADR-0006 to ADR-0008, ADR-0011 (outbox) and ADR-0012 (model pair) merged.

Demo:

- [ ] 1.27 Ben and Asha ask the same question and get different, correctly cited answers, on the deployed free stack in demo persona mode (the deploy is user-run, 0.46 to 0.51) or on local compose if the deploy is not up yet. Save a recording.

Exit gate:

- Switching persona changes results as expected on 5 scripted questions.
- 0 observed canary leaks on search and answer paths.
- Recall@5 for B0 and B1 recorded on 30 C1 dev questions, and neurons per answer recorded against the 1.14 budget.
- `make freetier-check` passes with the Phase 1 code loaded.
- ADR-0006 to ADR-0008, ADR-0011 and ADR-0012 merged.

Cut if behind: rerank (keep fusion), Ask page polish (use the API and a CLI), the hosted run of the 1.27 demo (keep the local recording). Never cut the query registry test, the RLS backstop test, canary matrix v1, the neuron budget or the free-tier gate.

## Phase 2. Decision memory (3 to 6 Nov)

Goal: decisions become versioned, bitemporal, cited records with timelines and as-of answers, and derived claims respect permissions. See [section 10](brainstorm.md#10-data-model-and-temporal-semantics).

Deliverables: decision detection and MADR extraction; candidate decisions, claims and support spans; aliases; bitemporal decision_versions with the EXCLUDE constraint; a timeline API with as-of parameters; supersession, reversion and correction handling; claim-level projection; lineage-driven invalidation. Every LLM call goes through the neuron budget and its results are cached (ADR-0015).

Build:

- [ ] 2.1 Run S4 on 3 Nov: label 30 decisions across ADRs, chat and meeting notes, score extraction with the default Workers AI model through the neuron budget, estimate the neurons for a full extraction of the Kestrel corpus, and record granularity and extraction policy in ADR-0009 (D1, D3, open question 6).
- [x] 2.2 Cheap detectors: ADR paths and front matter, decision phrases, and decision sections in meeting notes. PR labels follow in Phase 3.
- [x] 2.3 LLM extraction into the MADR schema with evidence spans and the 3 to 5 most recent prior decisions as context. Output is always a candidate. Calls go through the neuron budget, and results are cached by revision hash, model key and prompt version, so a re-run spends no neurons on unchanged content (ADR-0015).
- [x] 2.4 Tables: entities, aliases, claims, claim_support, decisions, decision_versions, decision_links, anchors, lineage and review_items.
- [x] 2.5 decision_versions with valid_period, recorded_period and the GiST EXCLUDE constraint (D9).
- [x] 2.6 Transactional write helpers for supersession, reversion, correction and backdating.
- [x] 2.7 Status mappers from MADR, KEP and PEP status fields onto the lifecycle (D3), including decisions with no single approver (open question 7).
- [x] 2.8 Alias resolution: deterministic matching first, then embeddings with LLM confirmation through the neuron budget, every merge reviewable.
- [x] 2.9 Timeline API, and valid_time and recorded_time parameters on decision and answer endpoints, both defaulting to now.
- [x] 2.10 Decision index in retrieval, and graph expansion over supersession and anchor edges, 1 to 3 hops with an ACL check per hop (D8).
- [x] 2.11 L5 claim-level projection: a claim is visible only if one full support set is visible, and summaries with any hidden input are regenerated per principal set (D12). Regenerated summaries are cached by principal-set hash and input hashes, built on read, and counted against the neuron budget.
- [x] 2.12 Lineage-driven invalidation: an input that changes, is deleted or changes ACLs marks dependent artifacts stale, and they rebuild on next read. Rebuilds are idempotent DBOS steps, so one cut short by Render sleep resumes on the next start (ADR-0014).
- [x] 2.13 Decision page v1: status and verification badges, claims with citations, and a timeline list with an as-of control.

Test:

- [x] 2.14 The EXCLUDE constraint rejects overlapping versions of one decision and accepts adjacent ranges.
- [x] 2.15 Flag-vendor backdating: "as known today" for 5 Jun returns vendor Y, and "believed on 8 Jun" for 5 Jun returns vendor X.
- [x] 2.16 As-of answers never cite evidence recorded after the requested recorded time (the Q42 case in [section 16](brainstorm.md#16-evaluation-plan)).
- [x] 2.17 Projection: Ben sees ADR-007's status and claims C1 and C3, never C2, and the regenerated summary carries no trace of C2 and no hidden count.
- [x] 2.18 ADR-012 superseded by ADR-018 and the ADR-009 partial revert return the right status at four dates each.
- [x] 2.19 Lineage: editing a cited doc marks dependent summaries stale, and the next read rebuilds them.
- [x] 2.20 Canary matrix extended to decision page, timeline and as-of paths.
- [ ] 2.21 As-of accuracy for B1 and B2 measured on the C1 dev split, locally with Workers AI and cached embeddings, with neurons spent recorded. CI runs the deterministic part with the fakes.

Docs:

- [x] 2.22 docs/temporal-model.md: the five query types, the change taxonomy and the edge cases.
- [ ] 2.23 ADR-0009 merged.

Demo:

- [x] 2.24 ADR-007 archaeology, from proposal on 3 Mar to PR #123 on 12 Mar, and ADR-012 time travel: Kafka on 10 Mar, superseded on 15 Jul. Run on local compose, and on the deployment when it is up (user-run).

Exit gate:

- Overlapping versions rejected, and tests 2.15 to 2.18 pass.
- As-of accuracy recorded on C1 dev for B1 and B2.
- Both demos run with no manual database edits.
- `make freetier-check` passes with the Phase 2 code loaded, and the day's extraction stays under the neuron cap.

Cut if behind: LLM confirmation for aliases (keep deterministic matching), graph expansion beyond 1 hop, decision page polish, the hosted run of the 2.24 demo. Never cut the EXCLUDE constraint, the as-of tests or the projection tests.

## Phase 3. Live sync and permissions (7 to 10 Nov)

Goal: GitHub changes and access revocations reach every query path within the SLOs, and deletions purge everything derived from them. See [section 11](brainstorm.md#11-permission-model) and [section 12](brainstorm.md#12-ingestion-and-sync).

Why the receiver sits at the edge: the Render service sleeps after 15 min idle and takes about 1 min to wake, while GitHub expects a webhook response within 10 s. A delivery to a sleeping service would fail, and permission events would wait for reconciliation. So a Pages Function on the Cloudflare free plan receives webhooks, records them durably and wakes Render, and the API applies pending permission events before it answers any query after a wake (drain-before-serve). This amends ADR-0014 and is recorded in a new ADR (3.30).

Deliverables: a GitHub App on the demo org; an HMAC-verified webhook receiver at the edge with a thin inbox and a wake call, plus the same receiver contract in the API for local runs; drain-before-serve on every start; DBOS ingestion workflows with fetch-on-notify; PR, review, issue and Markdown ingestion; repo, team and collaborator permission sync; an ACL priority queue; reconciliation and redelivery; deletion purge through lineage; freshness tracking; the revocation and freshness timing harness, which counts cold starts.

Build:

- [ ] 3.1 Run S5 on 7 Nov: add and remove members, teams, collaborators and repo visibility on the demo org, and log which events fire and which API calls reveal state. Record coverage and the polling interval in ADR-0010 (D11, D13).
- [ ] 3.2 Push the rendered Kestrel Pay repos to the demo org: payment-service, ledger, mobile-app, platform-infra, security-policies, kestrel-sdk and eng-docs.
- [ ] 3.3 GitHub App with least-privilege read permissions. The webhook secret lives in the Pages project secrets and in local `.env`; the private key lives in Render's environment (`sync: false`) and in local `.env`. Neither is ever committed or logged.
- [x] 3.4 Webhook receiver at the edge: a Pages Function at `/webhooks/github` (added to `_routes.json`) checks the HMAC SHA-256 signature in constant time, inserts a thin inbox row keyed by delivery id (event, action, installation, org and repo ids, received time; no payload text) with `ON CONFLICT DO NOTHING`, answers 202 well inside 10 s, then wakes Render with a `GET /healthz` after the response. Default write path: Neon's serverless HTTP driver as a new role `cairn_edge` with INSERT on the inbox table only, created by the managed-mode bootstrap (ADR-0013). Fallbacks if that fails its Verify: Hyperdrive, or Cloudflare Queues on the free plan (10,000 operations per day, 24 h retention). The API implements the same contract for local runs, fed by a webhook forwarder (Verify), and one set of signed fixture deliveries tests both.
- [ ] 3.5 DBOS workflow per delivery id, started by the inbox drainer inside `cairn-combined`: fetch current state, upsert a revision only if the hash changed, write ACLs, chunks, embeddings and lineage in one transaction, then enqueue extraction through the outbox (ADR-0011). Embedding and extraction calls go through the neuron budget.
- [x] 3.6 Connectors for PRs, reviews, issues, comments, and Markdown and ADR files on push, including PR-label decision detection.
- [x] 3.7 Permission sync from events plus a permission poll that runs on every start (inside drain-before-serve, 3.28) and on a schedule while the service is awake, using conditional requests where supported (Verify) and writing time-aware memberships. There is no always-on poller: Render free has no cron, and no keep-alive is used (ADR-0014).
- [x] 3.8 ACL priority queue ahead of content jobs, with per-scope ordering so content waits for pending ACL jobs (the new-enemy rule).
- [x] 3.9 L4 late recheck before composing, and L7 cache keys that include the principal-set hash and ACL version.
- [x] 3.10 Reconciler on every start and on a schedule while awake: list recent deliveries, redeliver failures through the REST API within GitHub's redelivery window (Verify), diff object lists, and backfill at low priority.
- [x] 3.11 Deletion purge through lineage: chunks, embeddings, cache entries and support sets. Decisions with no support left become unsupported.
- [x] 3.12 Freshness tracking and badges (Live, Delayed, Stale, Imported), per source and per answer.
- [x] 3.13 Timing harness: scripted source changes, polling every path until content is unreachable or queryable, timings written to Parquet. Each trial records whether the API was warm or asleep at the change. Revocation time runs from the GitHub change to the send time of the first probe after which no probe on any path returns the content; freshness time runs from the change until a probe returns the new content, so a wake counts in full. Cold-start trials are included in every reported p95, and warm and cold p95 are also reported separately.
- [x] 3.14 Sources and sync health page, and Access and audit page v1.
- [ ] 3.15 GitHub OAuth login that maps users to principals, with the callback on the Pages origin so cookies stay first-party, and persona dev login kept for local runs and demo persona mode (cut ladder step 5).
- [x] 3.28 Drain-before-serve: on every start, after DBOS launches, the jobs runner applies all pending ACL-class inbox rows (membership, team, collaborator, repository visibility and installation events) and runs the permission poll for the changes S5 found without events; readiness reports a sync check. Every query path (search, answer, decision, timeline and MCP) checks a sync barrier: while any ACL-class row received before the request is unapplied, it waits up to 20 s and then fails closed with 503 `permissions_syncing`. Content events drain after ACL events.

Test:

- [x] 3.16 On both receivers (edge and local): a bad signature gets 401, a duplicate delivery is a no-op, and out-of-order events keep the newest state. The edge Function stays under the 10 ms CPU limit on the largest fixture (Verify).
- [x] 3.17 Receiver outages: (a) the edge receiver returns 500 for 5 minutes during scripted changes, and reconciliation recovers every missed change; (b) the API is stopped or asleep while the edge records events, and on start drain-before-serve applies them before any query is answered.
- [x] 3.18 Revocation: remove Ben from the security team and confirm ADR-019 evidence is unreachable on every path, over at least 20 trials locally with p95 recorded. The deployment re-measure is 6.6.
- [x] 3.19 New-enemy: revoke, then immediately add a sensitive file. Ben never sees it in any trial.
- [x] 3.20 Deletion: delete eng-docs/design/ledger-kafka.md. Its spans, embeddings and cache entries disappear, and claims it alone supported become unsupported.
- [x] 3.21 An answer cached for Asha is never served to Ben.
- [x] 3.22 Canary matrix extended with the after-revocation, after-deletion, in-flight ACL update and pending-at-wake states.
- [x] 3.23 Freshness: scripted PR edits become queryable, over at least 20 trials locally with p95 recorded.
- [x] 3.29 Drain-before-serve: with ACL-class inbox rows pending at start, every query path either waits for the drain or returns 503 `permissions_syncing`, and never returns content the pending events revoke.

Docs:

- [x] 3.24 docs/sync.md: the S5 event list, the edge receiver, sleep and wake behavior, drain-before-serve, failure modes and a recovery runbook.
- [x] 3.25 Audit retention rule after source deletion, decided in a threat model review (open question 14).
- [ ] 3.26 ADR-0010 merged.
- [x] 3.30 ADR for the edge webhook receiver and drain-before-serve (next free number, amends ADR-0014) merged; the threat model's TM-15 gains the `cairn_edge` credential and the webhook secret held at the edge.

Demo:

- [ ] 3.27 Live revocation with a visible timer: remove Ben on GitHub, his results disappear, and the audit entry shows the timing. Run on local compose; on the deployment once 6.3 is live (user-run), with the API asleep at the start of at least one take.

Exit gate:

- Revocation p95 <= 60 s and freshness p95 <= 60 s, each measured locally over at least 20 trials (Proposed target). If the deployment is up, its first trials are recorded with cold starts counted; the gate for the deployment is 6.13.
- Deletion, new-enemy, cache and drain-before-serve tests pass on every path.
- Reconciliation recovers the 5-minute edge outage and the stopped-API case with no manual steps.
- `make freetier-check` passes with the Phase 3 code loaded.

Cut if behind: GitHub OAuth login (keep persona login), sync health page polish, backfill throttling, and the edge receiver if the hosted deployment is cut (cut ladder step 10; the local receiver stays). Never cut HMAC verification, inbox idempotency, drain-before-serve, the timing harness, the deletion purge or the new-enemy test.

## Phase 4. Experience and agents (11 to 14 Nov)

Goal: a bounded answer workflow with verification, conflicts and abstention, the P0 screens, and a read-only MCP server that passes the permission matrix. See [section 13](brainstorm.md#13-extraction-retrieval-and-answering) and [section 14](brainstorm.md#14-agent-interface-mcp).

Deliverables: the LangGraph workflow (analyze, plan, retrieve, expand, rerank, compose, verify) inside the combined process's memory budget; conflict detection and the review queue; the UI screens on Cloudflare Pages; an MCP stdio server with five tools and an environment token, run on the user's machine; MCP paths in the canary matrix; injection and setup docs.

Build:

- [x] 4.1 LangGraph workflow with the seven steps (D14), checkpoints and one trace span per step, running in `cairn-combined`. Its memory is measured with `make freetier-ram`; if it does not fit the budget, the same steps run as plain async functions, and that change of D14 gets an ADR (ADR-0014). Every model call in the workflow goes through the neuron budget (ADR-0015).
- [x] 4.2 Verifier: each claim must be supported by its cited spans, dates must fit valid_period, and citations must sit inside the visible set, with the L6 canary scan on output.
- [x] 4.3 Abstention path: the standard not-found reply plus what was searched.
- [x] 4.4 Conflict detection: deterministic rules first, then an LLM judge from a different model family than the answer model (`glm-4.7-flash` by default, ADR-0015) through the neuron budget. Conflicts open review items and mark decisions disputed, and are never auto-resolved (D4).
- [x] 4.5 Review queue: verify, edit, dispute and reject, with an evidence diff and a required resolution reason.
- [ ] 4.6 Screens from [section 8](brainstorm.md#8-journeys-ux-concepts-and-success-metrics): Ask with the full answer card, Decision page with the as-of slider, Review queue, Sources and sync health, Access and audit, and the admin Query inspector. Every screen handles the waking state (503 `api_unavailable` or `permissions_syncing` with `Retry-After`).
- [ ] 4.7 Admin view-as: read-only, time-boxed, reason required, audited and visible to the viewed user (P1).
- [x] 4.8 MCP stdio server with search_context, get_decision, decision_history, get_evidence and pre_change_brief (D15). It runs on the user's machine, against local compose or against the deployed API through the Pages origin; it is never a hosted process in the MVP. A per-user delegated token comes from the environment, and audit rows carry both identities.
- [x] 4.9 MCP limits: response size caps, a per-token rate limit, a per-token share of the daily neuron budget, and public untrusted content excluded by default and labeled when included.
- [x] 4.10 Prompt assembly that passes evidence as delimited data with source and tier labels. The workflow has no action tools.
- [ ] 4.11 Stretch: PR guard prototype for decisions with a machine-checkable confirmation rule, advisory only (cut ladder step 3).
- [ ] 4.12 Stretch: MCP over HTTP with OAuth 2.1 per the spec, only if the demo needs a remote client (cut ladder step 7, open question 11). If built, it runs inside `cairn-combined` behind the Pages proxy and within the memory budget.

Test:

- [x] 4.13 Verification drops unsupported claims, and the answer abstains when nothing survives.
- [x] 4.14 ADR-015 conflict: both dates appear with tiers and sources, and nothing is auto-resolved.
- [x] 4.15 Canary matrix on all five MCP tools, for agents acting for Asha and for Ben.
- [x] 4.16 Injected public kestrel-sdk issue: no restricted content in any output, and no instruction to act in Cairn's output.
- [x] 4.17 Token tests: a missing or wrong token gets nothing, and an agent token never exceeds its user's access.
- [ ] 4.18 Accessibility: keyboard paths, visible focus, labels and an automated scan on every screen. Full validation still needs manual testing with assistive technologies.
- [ ] 4.19 Usability check with 3 people on the default as-of view (open question 8).
- [x] 4.20 Latency p95 on C1 dev recorded locally (Proposed target <= 5 s). On the deployment, warm latency at 0.1 CPU and the cold-start wake are reported separately. `make freetier-check` passes with the workflow loaded.

Docs:

- [x] 4.21 docs/mcp.md: Claude Code setup against local compose or the deployed API, tool contracts, and the warning against pairing Cairn with write-capable tools on untrusted repos.
- [x] 4.22 docs/security.md: injection posture and residual risks, including evidence text sent to Workers AI or an optional provider.

Demo:

- [ ] 4.23 Claude Code, acting for Asha, calls pre_change_brief for db/pool.py and plans around ADR-009's partial revert, with the MCP server on the laptop.

Exit gate:

- 0 observed leaks on every MCP path in the canary matrix.
- The injected issue causes no leak and no action.
- J1, J4, J5 and J6 run end to end with no manual database edits.
- `make freetier-check` passes with the workflow loaded.

Cut if behind: the stretch items, admin view-as, the Query inspector, then review UI polish down to a list with action buttons (cut ladder step 6). Never cut the verifier, abstention, the MCP matrix run or the injected-issue test.

## Phase 5. Evaluation (15 to 17 Nov)

Goal: reproducible B0 to B3 results with ablations, confidence intervals and an error taxonomy, following [section 16](brainstorm.md#16-evaluation-plan). Evaluation runs locally on compose core and the experiments profile, within the Workers AI daily allocation (ADR-0015).

Deliverables: frozen C1 v1; B0 to B3 runs and ablations; a C2 PEP and KEP subset and, if time allows, a C3 EnterpriseRAG-Bench subset; the ACL-filtered vector study; latency and cost numbers in neurons; judge calibration; an error taxonomy; DuckDB results tables; a neuron plan for the phase.

Neuron plan (Estimate, at the per-call costs of 1.14 and a C1 test split of about 75 questions). The cap allows 27,000 neurons over the three days.

| Run | Neurons (Estimate) |
|---|---|
| Embed the C1 corpus once, then cached | about 220 |
| B0 to B3 answers on the C1 test split (about 300 answers at about 37) | about 11,100 |
| Judge those answers (about 24 each) | about 7,200 |
| Judge calibration on 50 human-labeled answers | about 1,200 |
| Verification ablation with answers on 20 dev questions | about 1,200 |
| Other ablations, scored on retrieval metrics from cached embeddings | under 100 |
| Total | about 21,000, leaving about 6,000 for re-runs, C2 and C3 |

Build:

- [ ] 5.1 Freeze C1 v1: about 150 questions with dev and test splits, and the corpus hash recorded.
- [ ] 5.2 C2 subset: PEP and KEP lifecycles with status and replacement fields, git history for recorded times, and ADR histories from the published replication packages if their licenses allow. Check per-file and package licenses (Verify, open question 10). Embedding the subset costs about 1,836 neurons per million tokens, so its size is chosen to fit the plan above.
- [ ] 5.3 C3 subset: EnterpriseRAG-Bench GitHub, Confluence and Slack documents with Basic, Semantic, Project, Conflicting and Info-not-found questions. Check the dataset card license (Verify, cut ladder step 4). Sized to the neuron plan, and loaded only into the local database.
- [ ] 5.4 Runners for B0, B1, B2 and B3 plus ablations: rerank, graph expansion, decision index, verification, iterative scans, and each of L4, L5 and L7. Models, prompts and seeds are pinned, Workers AI model keys are recorded, and answers are cached by variant, question, evidence hash, model key and prompt version.
- [ ] 5.5 Judge calibration: a judge from a different model family (`glm-4.7-flash` for the `gemma-4-26b-a4b-it` answers, or an optional paid judge with the user's key, ADR-0015), at least 50 human labels, and agreement reported.
- [x] 5.6 ACL-filtered vector study at 1, 5, 20 and 50% selectivity: recall@10 against exact search, p50 and p95 latency, and index size. It runs on the local database with S1's synthetic vectors, so it spends no neurons and never touches Neon (ADR-0013).
- [ ] 5.7 Stretch: Qdrant and pgvectorscale arms of the study in the experiments profile (cut ladder step 2). The experiments profile is local only and is never deployed (ADR-0014).
- [x] 5.8 Latency and cost per query for every variant: neurons per answer and their dollar equivalent at the Workers AI paid rate (Verify), plus tokens and dollars for any paid provider used.
- [x] 5.9 Bootstrap 95% confidence intervals, per-category tables and error taxonomy labels.
- [x] 5.10 One command regenerates every table from Parquet through DuckDB.
- [ ] 5.11 Optional S6 Graphiti baseline on 50 documents and 20 as-of questions (cut ladder step 1).
- [x] 5.18 Schedule the runs over 15 to 17 Nov against the 9,000-neuron daily cap, using the plan above. If a run would pass the cap, move the judge to an optional paid key or finish the run on the next UTC day; never raise the cap above the free allocation.

Test:

- [ ] 5.12 A re-run with the same seeds reproduces deterministic metrics exactly from cached embeddings and answers, and judged metrics within their confidence intervals on a 20-question re-judged sample.
- [x] 5.13 Deterministic suites (permissions, as-of, leaks) report separately from judged metrics.
- [x] 5.14 The full canary matrix, about 390 assertions (Estimate), passes on the frozen build.
- [x] 5.19 Regenerating the tables from Parquet makes no provider call (checked with the fakes set to fail on use), and every table records the model keys and the neurons spent.

Docs:

- [ ] 5.15 docs/results.md: tables with confidence intervals, failures included, model and corpus versions, and cost in neurons.
- [x] 5.16 Error analysis: the top failure categories, each with an example.

Demo:

- [ ] 5.17 Walk through the results table and one failure case.

Exit gate:

- One command regenerates every results table from a clean checkout.
- C1 test results reported against the Proposed targets (Recall@5 >= 0.85, as-of accuracy >= 0.85), with misses explained rather than hidden.
- Judge agreement reported, and 0 observed leaks on the full matrix.
- Neurons spent recorded per run, and no day passed the cap.

Cut if behind: the Graphiti baseline, the Qdrant and pgvectorscale arms, then the C3 subset, keeping C1 and C2, then judged metrics beyond a stratified sample of the C1 test split (cut ladder step 8). Never cut the C1 test results, the full matrix or the reproducible tables.

## Phase 6. Ship (18 to 20 Nov)

Goal: an honest public package: a README with guarantees and results, a three-minute video, a free-tier deployment and a write-up. See [section 20](brainstorm.md#20-portfolio-packaging-and-interview-prep).

Deliverables: a completed security review checklist, pinned dependencies, the README, the video, a deployment on Cloudflare Pages, one Render free web service and Neon free per ADR-0014 and `docs/deploy.md` (TLS is automatic on `pages.dev`, `onrender.com` and Neon), or a local run with video, a write-up, and the MVP exit criteria check.

Build:

- [x] 6.1 Pin dependencies with hashes, including the Pages Function packages, generate an SBOM, and clear or document every audit finding.
- [x] 6.2 Security review checklist: database roles and RLS (including the Neon owner's BYPASSRLS and the laptop-only owner credentials), secrets in Render's environment and the Pages secrets, the proxy token, webhook verification at the edge and the `cairn_edge` role, MCP tokens, LLM egress to Workers AI and any optional provider with their data terms (Verify), log contents, synthetic data only.
- [ ] 6.3 Deploy by 18 Nov per ADR-0014 and `docs/deploy.md`, run by the user (0.46 to 0.51): the Neon free project bootstrapped from the laptop with `make remote-init`, the Render free web service from `render.yaml` running `cairn-combined`, and the Pages project with the `/api` proxy and the edge webhook receiver; demo persona mode and synthetic data only. This answers open question 12: Oracle Always Free A1 is rejected (halved to 2 OCPU and 12 GB in June 2026, and it reclaims idle instances), and so is Supabase free (it pauses after 7 days idle).
- [x] 6.4 Seed and reset scripts so every demo starts from the frozen corpus. The seed is a data-only dump (items, revisions, chunks with their stored embeddings, ACLs and decisions) loaded from the laptop as `cairn_owner`, so seeding spends no neurons. It refuses to load when the dump plus the current database would pass 512 MB, half of Neon's 1 GB (Estimate for the Kestrel corpus: a few MB). Reset truncates the content tables and reloads; local runs use the same dump.
- [x] 6.17 Free-plan cost check: Render on the free plan, Neon on the free plan, Cloudflare Pages and Workers on the free plan, Workers AI under the daily allocation, and no paid add-on. No keep-alive pinger or uptime monitor is used (ADR-0014), because it would keep Neon awake and spend about 186 of its 100 CU-hours a month; the cold start is accepted and documented. Recorded in `docs/deploy.md` with the date checked.

Test:

- [ ] 6.5 Full CI, the canary matrix, the eval smoke and `make freetier-check` pass on the release commit.
- [ ] 6.6 Re-measure revocation and freshness p95 on the demo deployment with the 3.13 harness, at least 20 trials each, with at least 5 of them starting with the API asleep. Cold starts are counted in the p95 and also reported separately.
- [ ] 6.7 The quick start works from a fresh clone on a clean machine in under 30 minutes (Proposed target).
- [ ] 6.18 Wake test on the deployment: after more than 15 minutes idle, the first `/api/readyz` through Pages answers within the 90 s proxy timeout or returns 503 `api_unavailable` with `Retry-After`, and the web shows the waking state.

Docs:

- [x] 6.8 README: pitch, guarantees and non-guarantees, architecture diagram with the free-tier topology, results table with the commit hash, quick start, threat model summary, limitations (including the cold start and the free-tier limits). Run the "Cairn" name collision check first and rename if needed (D2).
- [x] 6.9 Write-up covering the problem, design, results and failures.
- [x] 6.10 Resume bullets filled in with measured numbers only.

Demo:

- [ ] 6.11 Record the three-minute video following the script in section 20.

MVP exit criteria, from [section 16](brainstorm.md#16-evaluation-plan):

- [x] 6.12 Canary matrix: 0 observed leaks across all personas, paths and states.
- [ ] 6.13 Revocation p95 and freshness p95 <= 60 s over at least 20 trials on the demo deployment, cold starts counted (6.6).
- [ ] 6.14 Recall@5 and as-of accuracy >= 0.85 on the C1 test split.
- [ ] 6.15 B0 to B3 table with confidence intervals published, failures included.
- [ ] 6.16 Live demos of J1, J2, J3 and J5 with no manual database edits.

Exit gate: items 6.12 to 6.16 are ticked, each with an evidence link in the progress log. A missed target is reported in the README as missed, with its measured value. If cold starts push freshness p95 past 60 s, the README reports the all-trials and warm-only values.

Cut if behind: the hosted deployment (ship local plus video, cut ladder step 10), write-up length. Never cut the README guarantees and results, the video or the exit criteria check.

## Post-MVP backlog

Roughly in priority order. Each item needs its own ADR before work starts.

- [ ] Incident and postmortem memory, the next wedge (D1), reusing decisions, timelines and permissions.
- [ ] PR guard for code-inferable decisions, advisory only, if 4.11 did not ship.
- [ ] Living briefs per service, regenerated through lineage and projection.
- [ ] Decision-debt view: stale, disputed and unowned decisions.
- [ ] Drift detection between accepted decisions and current code.
- [ ] Safe ADR write-back as draft PRs, human-triggered from the UI only.
- [ ] Capture bot: "record this as a decision" from chat.
- [ ] PII and secret scanning at ingest.
- [ ] ACL watermark so clients can require a minimum ACL version.
- [ ] Remote MCP over HTTP with OAuth 2.1, if 4.12 did not ship.
- [ ] Live Slack connector, planned around the rate limits in [section 3](brainstorm.md#3-stress-test-of-the-original-research-doc).
- [ ] Notion and Confluence connectors.
- [ ] OpenFGA policy model once nested group semantics appear (D11).
- [ ] Graphiti baseline, if 5.11 was cut.
- [ ] Agent decision-trace ingestion, the later wedge (D1).
- [ ] Multi-tenant hosting with tenant partitioning.
- [ ] Paid upgrade path, if the free tiers stop fitting: a paid Render instance (always on, which removes the cold start from the SLOs), a paid Neon plan (more storage and compute), Workers AI usage past the daily allocation, and the Workers paid plan (more CPU per request, Containers). Each is a cost decision with its own ADR amending ADR-0014 or ADR-0015.

## Spike tracker

| Spike | Question | Timebox | When | Feeds | Recorded in | Status |
|---|---|---|---|---|---|---|
| S1 ACL-filtered retrieval | Which filter strategy meets recall and latency at 1% to 50% selectivity? | 1 day | Phases 0 and 1, done by 2 Nov; local database only | D7 | ADR-0008 | Harness built in Phase 0 |
| S2 RLS and EXPLAIN | Do the native full-text GIN index and the pgvector HNSW index stay in use under FORCE RLS with a non-owner role? | 0.5 day | Phase 0, done by 30 Oct; re-run on full-text search in 0.27 (ADR-0013) | D5, D6, D11 | ADR-0006 | First run 27 Oct on ParadeDB BM25; full-text re-run pending |
| S3 Models | Which Workers AI embedding model and reranker fit the latency target and the neuron budget? | 0.5 day | Phase 1, 30 Oct | D16 | ADR-0007, choosing within ADR-0015 | Not started |
| S4 Extraction quality | Does extraction reach useful precision on 30 labeled decisions? | 1 day | Phase 2, 3 Nov | D1, D3 | ADR-0009 | Not started |
| S5 GitHub permission events | Which events and API calls reveal member, team, collaborator and visibility changes? | 0.5 day | Phase 3, 7 Nov | D11, D13 | ADR-0010 | Not started |
| S6 Graphiti (optional) | Is a Graphiti baseline worth running on C1? | 0.5 day | Phase 5, if time allows | D8 | docs/results.md | Not started |

Method and success criteria for each spike are in [Appendix C](brainstorm.md#appendix-c-spike-plans).

Open Verify items and the checklist item that resolves each:

- [ ] Private repos and teams on the demo org's plan (0.10).
- [ ] Full-text GIN and pgvector HNSW index use under RLS, and array-overlap filter pushdown (0.27, 1.1).
- [ ] The pgvector version Neon offers for PostgreSQL 18, at least 0.8.0, and the matching image digest (0.25).
- [ ] PostgreSQL 18 on Neon free, and Neon accepting SCRAM verifiers and the SET grants of managed mode (0.26, 0.47).
- [ ] Free-tier limits at deploy time: Render free (512 MB, 15-minute sleep, 750 hours), Neon free (1 GB, 100 CU-hours), Pages Functions and Workers AI daily neurons (0.35, 6.3, 6.17).
- [ ] Workers AI model availability on the free allocation and neuron rates for the default models (0.33, 1.2, 1.14).
- [ ] Edge receiver: Neon's HTTP driver or Hyperdrive from a Pages Function on the free plan, the wake call after the response, and CPU time per delivery under 10 ms (3.4, 3.16).
- [ ] A webhook forwarder for local runs (3.4).
- [ ] GitHub permission event names and actions (3.1).
- [ ] GitHub API rate limits and conditional requests (3.7).
- [ ] GitHub's redelivery window for failed deliveries (3.10).
- [ ] PEP per-file licenses and ADR replication package licenses (5.2).
- [ ] EnterpriseRAG-Bench dataset card license (5.3).
- [ ] The Workers AI paid rate per 1,000 neurons, for the dollar equivalent (5.8).
- [ ] Data terms of Workers AI and of any optional provider, and the licenses or terms of the default models (6.2).
- [ ] "Cairn" name collisions (6.8).

## Cut-line ladder

Apply in order when a phase runs a day late, and record each cut in the progress log with its reason.

| Step | Cut | Checklist item | What remains |
|---|---|---|---|
| 1 | Graphiti baseline | 5.11 | Postgres graph expansion only |
| 2 | Qdrant and pgvectorscale arms of the vector study | 5.7 | pgvector strategies |
| 3 | PR guard prototype | 4.11 | Backlog item |
| 4 | EnterpriseRAG-Bench subset | 5.3 | C1 and C2 |
| 5 | GitHub OAuth login | 3.15 | Persona dev login |
| 6 | Review UI polish | 4.5, 4.6 | A list with action buttons |
| 7 | MCP over HTTP | 4.12 | MCP over stdio |
| 8 | Judged metrics beyond the neuron plan | 5.4, 5.5, 5.18 | Deterministic metrics on the full C1 test split; judged metrics on a stratified sample |
| 9 | Hosted runs of the Phase 1 and Phase 2 demos | 1.27, 2.24 | Local recordings |
| 10 | Hosted deployment | 6.3, 6.6, 6.17, 6.18, the edge half of 3.4 | Local compose plus video; `make freetier-check` evidence; revocation and freshness from local runs, labeled as local in the README |

Never cut: the permission matrix (1.22, 2.20, 3.22, 4.15, 5.14, 6.12), the revocation test (3.18, 6.13), as-of correctness (2.15, 2.16), the eval table (5.10, 6.15), the free-tier gate (0.31) and the neuron budget guard (0.33).

## Risk watchlist

Check at the points listed. The full register is in [section 19](brainstorm.md#19-risks-and-open-questions-for-the-prd).

| Risk | Watch signal | Check at | Response |
|---|---|---|---|
| Extraction precision too low | S4 precision below 0.7 | Phase 2 start | Keep candidates only, lean on ADR files as T1 anchors, revisit D1 |
| Indexes ignored under RLS | Sequential scans on the full-text or HNSW queries in S2 EXPLAIN output | Phase 0 end | SECURITY DEFINER retrieval functions |
| Filtered vector recall collapses | Recall@10 ratio below 0.95 at 5% selectivity | Phase 1 end | Iterative scans or partitioning, per S1 |
| GitHub permission events incomplete | S5 finds unobserved changes | Phase 3 start | Shorter permission poll interval while awake, and the poll on every wake |
| Revocation SLO missed | p95 above 60 s | Phase 3 end | Profile the ACL queue and widen late-recheck coverage |
| Render sleep against the SLOs | Freshness p95 above 60 s on cold-start trials, or webhooks lost while the service sleeps | Phase 3 end, 6.6 | Edge receiver and drain-before-serve; report all-trials and warm-only values; no keep-alive (ADR-0014); a paid always-on instance is post-MVP |
| Free-tier limits change | A provider changes or removes a free limit or feature, as with Neon's `pg_search` (21 Sep 2026), Oracle A1 (June 2026) and the paid-only Workers AI models (28 Jul 2026) | Phase 3 start, Phase 6 start | Re-check the limits table, record the change in an ADR, fall back to local plus video (cut ladder step 10) |
| Neuron exhaustion | `budget_exhausted` during a demo or an eval day, or daily use above 7,000 | Phases 1, 2, 5 and 6 | Cached embeddings and answers, a smaller model, runs spread over days, an optional paid key, a reserve kept for demo days |
| Neon storage or compute | Database size above 512 MB, or more than 70 CU-hours used in a month | Phase 6 start | Deploy only the frozen demo corpus, keep spike and eval corpora local (ADR-0013), no keep-alive |
| Combined process over the memory budget | `make freetier-ram` peaks above 300 MiB idle or 384 MiB under small load, or an OOM kill | Each phase end | Profile imports and caches, move batch work to the laptop, replace LangGraph with plain steps by ADR |
| Edge receiver limits | Function CPU above 10 ms per delivery, or daily requests near 100,000 | Phase 3 | Thin inbox rows, Queues fallback, fewer subscribed events |
| Scope creep | Any phase a day late | Daily | Apply the cut-line ladder |
| LLM cost overrun | Neurons per answer above the 1.14 budget | Phases 1 and 5 | Smaller extraction model, ACL-keyed caching |
| Unreliable judge | Agreement below 0.8 | Phase 5 | More human labels, more weight on deterministic checks, an optional paid judge |
| Laptop resources | Core profile above 6 GB RAM, or core and freetier running together on the 3.83 GiB Docker VM | Phase 0 end | Fewer containers; run the profiles one at a time |

## Progress log

Add one row per working day. Reference item numbers in Done and Next, and link evidence such as CI runs, Parquet files and recordings for every exit gate.

| Date | Phase | Done | Blocked | Next |
|---|---|---|---|---|
| 27 Oct 2026 | Before Phase 0 | brainstorm.md and this plan written | None | 0.1 to 0.5, 0.10 |
| 4 Nov 2026 | Phase 1 | Phase 0 and the retarget ticked from the handoff (65 of 70 retarget tasks; user steps 0.46 to 0.54, 0.10 and 0.15 stay open). Phase 1 built and tested locally on the fakes: migrations 0003 to 0006, cairn_ingest role, connectors and importers, chunker, renderer, retrieval registry with the ACL predicate, B0/B1 pipeline, answer composition, audit log, persona login with demo mode and rate limits, outbox drainer and DBOS embedding workflow wired into the combined process. Canary matrix v1, registry and RLS backstop tests pass (apps/api/tests/integration). | Live Workers AI calls and the deploy (user-run) | 1.15 eval harness, 1.16 Ask page, 1.14 and 1.2 harnesses, docs and ADRs, then Phase 2 |
| 2026-11-04 | 1 | Phase 1 code complete: eval harness, Ask page, S1 (ADR-0008), retrieval docs, provider-failure tests; freetier-check passed (7 steps, ram 100 MiB) | 1.1, 1.2, 1.14, 1.24, 1.26 (ADR-0007/0012 proposed), 1.27 need live Workers AI / user steps | Phase 2 decision memory |
| 2026-11-04 | 2 | Decision memory: migrations 0007-0009, bitemporal store with GiST EXCLUDE, visible-events-only state, claim L5 visibility + citations, lineage invalidation, decision API and page, as-of search, demos 11/11 | 2.1 and 2.21 need live Workers AI (S4, as-of accuracy); 2.23 ADR-0009 stays proposed until S4 | Phase 3 live sync and permissions |
| 2026-11-04 | 2 | Decision memory: migrations 0007-0009, bitemporal store with GiST EXCLUDE, visible-events-only state, claim L5 visibility + citations, lineage invalidation, decision API and page, as-of search, demos 11/11 | 2.1 and 2.21 need live Workers AI (S4, as-of accuracy); 2.23 ADR-0009 stays proposed until S4 | Phase 3 live sync and permissions |
| 2026-11-04 | 2 | Decision memory: migrations 0007-0009, bitemporal store with GiST EXCLUDE, visible-events-only state, claim L5 visibility + citations, lineage invalidation, decision API and page, as-of search, demos 11/11 | 2.1 and 2.21 need live Workers AI (S4, as-of accuracy); 2.23 ADR-0009 stays proposed until S4 | Phase 3 live sync and permissions |
| 2026-11-04 | 3 | Live sync built and tested against a fake GitHub: edge receiver + local receiver on shared signed fixtures, cairn_edge insert-only role, inbox, ACL-first drainer, permission sync, deletion purge, reconcile, sync barrier, L4/L7, freshness badges, timing harness 20+20 trials (freshness p95 0.77 s) | 3.1-3.3 and 3.27 need your GitHub org/App; 3.5 runs as a thread not a DBOS workflow; 3.14, 3.15, 3.22, 3.26 and the readyz sync check not done | Finish Phase 3 leftovers, then Phase 4 |
| 2026-11-04 | 3 | Canary matrix v2 across sync states (readable, in flight, revoked, pending at wake, deleted); /readyz sync check | 3.1-3.3, 3.27 user-run; 3.5 thread not DBOS; 3.14 pages, 3.15 OAuth open | Sources/sync health and audit pages, then Phase 4 |
| 2026-11-04 | 3 | Sources/sync health and Access/audit pages (API + web, 101 web tests); my_audit and source_freshness SQL functions (migration 0011) | 3.15 OAuth cut for now (persona login kept); 3.1-3.3, 3.27 user-run | Free-tier gate with Phase 3 code, then Phase 4 |
| 2026-11-04 | 3 | make freetier-check passed with Phase 3 code (first run missed the RAM request budget by 3 of 144 under 0.1 CPU; re-run 0 of 121, then full pass) | 3.1-3.3, 3.5 (DBOS), 3.15, 3.26, 3.27 | Phase 4 |
| 2026-11-04 | 4 | Seven-step workflow as plain async functions (ADR-0017), verifier (support, dates, visible set, L6 canary scan) with abstention, conflict detection + review queue API (migration 0012), ADR-015 conflict test | 4.5 UI, MCP (4.8-4.9, 4.15-4.17), screens, docs | MCP server and agent tokens |
| 2026-11-04 | 4 | MCP stdio server (5 read-only tools), delegated agent tokens + cairn-agent CLI, /brief /evidence routes, untrusted-content label, limits (size cap, 30/min, per-agent daily call cap), canary matrix on all five tools for agents of Asha and Ben, injected-issue test, docs/mcp.md + docs/security.md | 4.9: the per-token neuron share is a per-agent daily call cap (MCP tools spend only query embed/rerank neurons). 4.5 UI, 4.6 screens, 4.7, 4.18-4.20, 4.23 (Claude Code demo, user-run) open | Review queue page, lint/types/test pass, freetier-check with workflow |
| 2026-11-04 | 4 | Review queue page (verify/edit/dispute/reject with required reason, both sides of a conflict shown) + API | 4.6 partial: Decision page uses date inputs not a slider, no Query inspector; 4.18 axe scan not run (no dependency); 4.7, 4.19, 4.20, 4.23 open | Verification pass, freetier-check with workflow |
| 2026-11-04 | 4 | make freetier-check passes with the workflow loaded (after fixing a 0.1 CPU shutdown regression: direct exit after clean shutdown, lazy sync import); answer latency p95 17.8 ms locally with fake models (docs/evidence/phase-4) | 4.20 deployment warm/cold latency is user-run; 4.6 slider/Query inspector, 4.7, 4.18 axe scan, 4.19, 4.23 open | Phase 5 evaluation |
| 2026-11-04 | 5 | Evaluation tooling: B0-B3 runners, bootstrap CIs, error taxonomy, per-category tables, DuckDB table regeneration (no provider call), deterministic suites (as-of 12/14 with the 2 misses explained, must-not-cite 0/4, leaks 0), cost estimates, judge-calibration tooling, C1 freeze record; fake-model baseline only | 5.1 (46 questions, target ~150), 5.2, 5.3, 5.4 ablations L4/L5/L7 toggles, 5.5 labels, 5.11, 5.12 judged part, 5.15 live numbers, 5.17 need you or live runs | Phase 6 |
| 2026-11-05 | 6 | Supply chain (hashed pins, CycloneDX SBOMs, audit with 1 documented moderate dev-only finding), security review checklist, seed dump/load/reset (no neurons, size-guarded), free-plan cost check as a test, README, write-up, resume bullets (measured only), canary matrix 0 leaks (50 tests) | 6.8: name collision found with other Cairn projects, rename NOT done (your decision); no git so results are identified by corpus hash. 6.3 deploy, 6.5 CI on a release commit, 6.6/6.13 deployment timings, 6.7 clean-machine quick start, 6.11 video, 6.14 real-model accuracy, 6.15 live B0-B3 table, 6.16 live demos, 6.18 wake test need you | Hand-off |
