---
status: accepted
date: 2026-11-03
decision-makers: Pankaj Baid
---

# ADR-0014: Deploy on Cloudflare Pages, one Render free web service in combined mode, and Neon free

Amends decision D18 of the [brainstorm decision log](../../brainstorm.md#1-tldr-and-decision-log) (the demo clause and the compose profiles) and confirms [ADR-0002](ADR-0002-dbos-durable-jobs.md) on Neon. It records decisions FT-09 to FT-19 and FT-27 of the `cairn-free-tier-retarget` spec. The store it runs on is [ADR-0013](ADR-0013-postgres-pgvector-fts-neon-store.md), and the hosted models are [ADR-0015](ADR-0015-workers-ai-models-and-neuron-budget.md). The step-by-step runbook is [docs/deploy.md](../deploy.md).

## Context and Problem Statement

D18 named an Oracle Always Free Ampere A1 VM for a public demo and left it as open question 12. In June 2026 Oracle halved that allocation and it can reclaim idle instances (FV0). The target is free plans only, so the demo has to run on limits that are documented today, and the claim that it fits has to be tested in CI instead of assumed.

The questions: where does a public demo run at no cost, how do one free web service and one free database divide the work, how does DBOS (ADR-0002) run when there is no background worker, what happens when the service sleeps, and which defaults does Phase 1 inherit?

## Decision Drivers

* No paid plan: no Cloudflare Workers Paid, no paid Render or Neon tier.
* The Render free web service has 512 MB of RAM and 0.1 CPU, sleeps after 15 minutes without inbound traffic, wakes in about a minute, and has no free background workers or cron jobs. Its 750 instance-hours a month are per workspace (FV0, FV6).
* Neon free has 100 CU-hours and 1 GB per project a month, scales to zero after 5 minutes, and `neondb_owner` has `BYPASSRLS` (ADR-0013).
* The public `onrender.com` URL must not be a second way in: only the liveness probe may be reachable without going through Cloudflare.
* A database outage must not make the host restart the service in a loop.
* The owner and admin credentials must never be stored on a hosted runtime; every runtime connection uses a NOBYPASSRLS role (ADR-0004).
* The free-tier claim must be reproducible on the laptop and in CI.

## Considered Options

* Cloudflare Pages for the static web build and a forwarding Function, one Render free web service running the API and DBOS in one process, and Neon free for the database (chosen).
* An Oracle Always Free Ampere A1 VM that runs the compose stack.
* Supabase free for the database.
* Render's free Postgres.
* Cloudflare Containers for the API.
* Neon with the Phase 0 text-search store.
* Local only, with a recorded video (the other half of D18).

## Decision Outcome

Chosen option: "Cloudflare Pages, one Render free web service in combined mode, and Neon free", because it is the only combination whose documented free limits (FV0, dated 30 Oct 2026) fit a Postgres with pgvector, an always-launchable job runner and a static web build at no cost. "Local plus video" stays as the fallback if a free-plan limit blocks the demo.

The decisions it records:

* Topology (FT-15, FT-16). The web build is a Cloudflare Pages project. `public/_routes.json` limits Functions to `/api/*`, so static requests never spend the 100,000 daily Function requests. The Function `functions/api/[[path]].ts` strips the `/api` prefix, forwards to the Render URL, sets the header `X-Cairn-Proxy-Token` and drops any client-supplied copy. Same origin keeps session cookies first-party. `public/_headers` copies the nginx security headers. The first deploy is a Direct Upload by the user with a pinned wrangler run through `pnpm dlx`, so the repo has no wrangler dependency.
* Proxy token (FT-16). When `CAIRN_PROXY_TOKEN` is set the API compares the header in constant time and answers 404 `not_found` with `Cache-Control: no-store` to anything else, so a direct hit on the Render URL learns nothing. `/healthz` is exempt so Render can probe it.
* Combined process (FT-10, FT-11). `cairn-combined` runs uvicorn and FastAPI in the main thread and launches DBOS and the heartbeat schedule from a background `JobsRunner` thread that retries with backoff. Render's health check path is `/healthz` (process only), so a database outage or a wake never restarts the service in a loop. In combined mode `/readyz` adds a `jobs` check that reports `jobs_not_launched` until DBOS is up. Local `core` keeps the split `api` and `worker` services.
* Secrets (FT-13, FT-14). Bootstrap and migrations run from the laptop with `make remote-init` and a gitignored `.env.neon`; the Blueprint `render.yaml` holds no secret value (`sync: false`). Render receives only the `cairn_app` and `cairn_dbos` passwords, the Cloudflare token and the proxy token. Connections use `sslmode=verify-full` with `sslrootcert=/etc/ssl/certs/ca-certificates.crt`, because `sslrootcert=system` loads no roots with psycopg-binary in the runtime image (FV11).
* Compose profiles (D18, FT-17, FT-18). `core`, `obs`, `experiments` and the new `freetier`. The profile runs `ft-db`, `ft-admin-init`, `ft-db-init` and `ft-app`, where `ft-app` is the combined image with `mem_limit: 512m`, `memswap_limit: 512m`, `cpus: 0.1`, `PORT=10000` and the proxy token on. The budget (Proposed target) is an idle peak of at most 300 MiB, a small-load peak of at most 384 MiB, at most 2% failed requests, no OOM kill and no restart. Measured on 3 Nov 2026 (`docs/evidence/free-tier/freetier-ram-2026-11-03.md`): idle peak 94.34 MiB, load peak 94.52 MiB, 0 of 298 requests failed. At 0.1 CPU `ft-app` needs about two minutes to become healthy (127.6 s in the first RAM run, [task 6.3 notes](../evidence/free-tier/task-6.3-notes.md); FV13).
* The free-tier gate (FT-19). `make freetier-check` (bring-up, smoke, managed-mode matrix, RAM check, tear-down) runs in `make gates` after `make down`, in the CI integration job, and in `make phase0-exit`, which also checks its RAM evidence. The core and freetier profiles never run together on the 3.83 GiB Docker VM.
* Environment file (FT-27). `.env` is append-only: `make env-update` adds missing keys and never rewrites a line, so the local `CAIRN_DB_PORT=55432` survives.

### DBOS placement and the outbox (FT-09)

DBOS keeps its own database `cairn_dbos`, owned by the role `cairn_dbos`, in the same Neon project and compute, reached through the direct (unpooled) endpoint. The reasons are the ones in ADR-0002 (job state must not sit beside restricted content, and the API role gets no access to it), plus a Neon-specific one: DBOS listens for notifications, which a transaction-mode pooler breaks. A second database on a branch shares its compute, so it costs no extra CU-hours. ADR-0002's pending question stands: an enqueue still cannot share a transaction with a write to `cairn`. Phase 1 therefore keeps the transactional `outbox` (PD1-07, ADR-0011): the import transaction inserts an outbox row next to its data, and a drainer in the combined process enqueues it.

FV5 checked DBOS 3.3.0 for the single long-lived process: after `DBOS.destroy()` a failed launch can be retried in the same process (so the runner has no give-up path), launch works from a non-main thread under uvicorn, `automatic_backfill` defaults to off (the code sets it explicitly), and TLS parameters in the system database URL work.

### Sleep, wake and the Neon compute budget (FT-12)

There is no keep-alive pinger. The demo heartbeat cron is `0 */5 * * * *` with `automatic_backfill` off. While Render has the service stopped nothing runs: heartbeats pause, missed ticks are not replayed, and on wake DBOS recovers pending workflows and the schedule resumes. The Pages Function waits up to 90 seconds for the wake and then answers 503 `api_unavailable`. Any periodic job added later must be idempotent and sweep once at start.

FV5 measured that a launched DBOS polls its database about 9.5 times a second. Neon scales to zero after 5 minutes without activity, so Neon is awake whenever the Render service is awake, and sleeps about 5 minutes after Render does. Neon compute hours therefore follow Render awake hours, not app traffic. At 0.25 CU the 100 CU-hours cover about 400 awake hours a month, fewer than Render's 750 free hours, so Neon is the limit that binds. When it runs out, Neon suspends the compute until the next period: `/healthz` still answers, `/readyz` returns 503, and the jobs runner keeps retrying. This corrects the earlier assumption that the heartbeat costs nothing; the cron is free, the polling is not. If the budget gets tight in Phase 1, DBOS polling intervals can be raised or fewer DBOS features run in the always-on process. Not changed here. Not yet known: whether Render's health probes count as inbound traffic for the 15-minute sleep (FV6). FU5 checks this on the first deploy.

### Defaults Phase 1 inherits

| Topic | Default for the revised Phase 1 spec |
|---|---|
| Lexical search | Native FTS. Chunk tables carry stored generated columns `tsv_en = to_tsvector('english', title \|\| ' ' \|\| body)` and `tsv_simple = to_tsvector('simple', body)` with GIN indexes. Queries OR `websearch_to_tsquery('english', q)` on `tsv_en` with `websearch_to_tsquery('simple', q)` on `tsv_simple` and rank with `ts_rank_cd(…, 32)`. B0 and B1 both use it. The Phase 0 BM25 items (PD1-02, V14) are dropped. |
| Models | `Embedder`, `Reranker` and `LlmProvider` from the retarget (async). Workers AI adapters by default, fakes in tests and CI. `bge-small-en-v1.5` at 384 dimensions. S3 (ADR-0007) chooses among Workers AI catalog models, and item 1.14 (ADR-0012) chooses the answer and judge pair under the family rule. Anthropic and OpenAI adapters are added in Phase 1. |
| Budget | Every Workers AI call goes through `NeuronBudget`, with the Postgres store in combined mode. Eval runs are planned against the 9,000-neuron daily cap (ADR-0015). |
| Memory | No in-process models. Local `api` and `worker` stay at 384m. The combined process must stay within the 512 MiB limit and the budget above, and `make freetier-check` keeps proving it. |
| Process | Production runs one combined process. Any Phase 1 thread (the outbox drainer) starts from `JobsRunner` after DBOS launches. Nothing may assume a separate worker container in production. |
| DBOS and the outbox | DBOS stays in `cairn_dbos` (separate database, direct endpoint). The transactional outbox (PD1-07, ADR-0011) stays required. The drainer sweeps once on start because of sleep. |
| Roles | `cairn_ingest` (Phase 1) is created by the same managed-mode bootstrap. No runtime role is BYPASSRLS or owns anything. `make matrix-managed` covers it. |
| Migrations | Head is 2 after the retarget (`0002_provider_usage.sql`). Phase 1 starts at 0003. |
| Connections | TLS settings from `libpq_tls()`, the Neon direct endpoint, and small pools (API pool at most 4, DBOS 5). |

### Consequences

* Good, because the whole demo costs nothing and its limits are written down with sources (FV0) and checked on every `make gates`.
* Good, because the owner and admin credentials never leave the laptop, and the runtime roles cannot bypass RLS.
* Good, because a database outage or a cold wake leaves `/healthz` up, so Render does not restart-loop the service.
* Good, because static requests never spend Function quota and the one public URL that bypasses Cloudflare answers nothing except the liveness probe.
* Bad, because a cold start takes about a minute for Render plus the Neon wake, so acceptance tests AT-EC2 and AT-EC5 are measured on a warm instance and the cold wake is reported separately.
* Bad, because heartbeats stop while the service sleeps, and Neon CU-hours, not Render hours, cap the awake time at about 400 hours a month.
* Bad, because free plans change without notice (Oracle did), so every number here is dated 30 Oct 2026 and must be re-read before relying on it.
* Bad, because Render's environment holds the app secrets (TM-15), and request floods can spend Function requests, Render hours, CU-hours or neurons (TM-16). Mitigations are the least-privilege runtime roles, a Workers AI-only token, the proxy token, `verify-full` TLS and the neuron budget.
* Bad, because Render, Neon and Pages behavior cannot be proven from the laptop. The emulated profile proves limits and roles; the real services are checked by the user steps FU1 to FU6.

### Confirmation

* `make freetier-check` runs `tests/freetier/test_freetier_profile.py`: `test_healthz_is_200_without_the_proxy_header`, `test_readyz_is_404_without_the_header_and_for_a_wrong_one`, `test_readyz_is_200_with_the_header_and_every_check_passes`, `test_heartbeat_has_a_success_run_in_the_dbos_database` (as `cairn_dbos`), `test_ft_app_has_the_render_free_limits`, `test_ft_app_environment_names_only_the_allowed_secrets` and the timed stop of `ft-app`. It then runs `make matrix-managed` and `make freetier-ram`.
* `scripts/freetier_ram.py` writes `docs/evidence/free-tier/freetier-ram-<date>.json`, and `scripts/phase0_exit.py` fails unless the newest one passed.
* `tests/scripts/test_render_blueprint.py` (`test_render_blueprint_has_no_secret_values`, `test_health_check_is_process_liveness_only`, `test_no_admin_owner_or_superuser_credential_key`), `tests/scripts/test_pages_config.py`, the Function's vitest cases for the proxy, and `tests/scripts/test_compose_config.py` (the freetier services, limits, loopback-only ports, and that no admin, owner or superuser password reaches `ft-app`).
* `apps/api/tests/unit/test_proxy_token.py` and its property tests; `apps/combined/tests/unit/test_app.py` and `test_jobs_runner_properties.py`.
* `make docs-check` requires the keywords of this ADR, and `tests/scripts/test_ci_workflow.py` asserts that CI runs `make freetier-check`.
* ADR-0011 (Phase 1) confirms the outbox with its own tests; this ADR only requires that it exists.
* The user steps FU1 to FU6 in [docs/deploy.md](../deploy.md) confirm the hosted services.

## Pros and Cons of the Options

### Cloudflare Pages, one Render free web service, Neon free

* Good, because every piece has a documented free plan: Neon has no time limit and needs no card (FV0), Pages has unlimited static requests, and Render has a monthly allowance of instance-hours. Whether Render and Cloudflare ask for a card was not checked.
* Good, because the same Docker image runs locally, in the emulated profile and on Render.
* Neutral, because three vendors mean three sets of terms and three places a limit can change.
* Bad, because of the cold start and the 512 MB, 0.1 CPU ceiling.

### An Oracle Always Free Ampere A1 VM

* Good, because a VM would run the full compose stack.
* Bad, because the allocation was halved to 2 OCPU and 12 GB on 15 Jun 2026 without an announcement, instances over the new limit were stopped, and idle instances can be reclaimed (FV0).

### Supabase free

* Bad, because a free project pauses after about a week of low activity (FV0), which suits a demo that may sit idle between reviews badly.

### Render's free Postgres

* Bad, because it is 1 GB and expires 30 days after creation, with a 14-day grace period before deletion (FV0).

### Cloudflare Containers for the API

* Bad, because Containers have no free allowance and are billed on Workers Paid ($5 a month) (FV0).

### Neon with the Phase 0 text-search store

* Bad, because Neon retired that extension on 21 Sep 2026. ADR-0013 records the history.

### Local only, with a recorded video

* Good, because it has no hosting limits.
* Bad, because nobody can try it. It stays as the fallback in D18.

## More Information

* [Deploy runbook](../deploy.md), with the user steps FU1 to FU9 and the limits table with sources.
* Verify results: [FV0, platform facts](../evidence/free-tier/verify/FV0-platform-facts.md), [FV5, DBOS in one process](../evidence/free-tier/verify/FV5.md), [FV6, Render](../evidence/free-tier/verify/FV6.md), [FV7, Pages](../evidence/free-tier/verify/FV7.md), [FV11, TLS roots](../evidence/free-tier/verify/FV11.md), [FV13, the freetier profile](../evidence/free-tier/verify/FV13.md).
* Revisit if: a free-plan limit blocks the demo, `ft-app` exceeds 512 MB with Phase 1 code, the Neon CU-hours cannot cover the awake time the demo needs, or Render's probes turn out to keep the service awake (FU5).
