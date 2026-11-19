# Cairn handoff

Written 3 Nov 2026 for Claude Code. Read this first, then `docs/deploy.md` and `docs/evidence/free-tier/README.md`.

Project: Cairn ("Company Brain"), permission-aware evidence search and decision memory. Repo: `/Users/pankajbaid/projects/company_brain` (not a git repo yet; the user does `git init` and the first push, step U4). Specs live outside the repo in `/Users/pankajbaid/projects/.kiro/specs/`.

## 1. Where things stand

| Item | State |
|---|---|
| Phase 0 foundations (spec `cairn-phase-0-foundations`, 74 tasks) | Done. `make phase0-exit` passed on 30 Oct on the old store. |
| Free-tier retarget (spec `cairn-free-tier-retarget`, 70 tasks) | 65 of 70 done. Left: task 14.1 (final run), checkpoints 7, 11 and 15, and the parent 14. |
| `development_plan.md` | Fully revised (task 13.2): Deployment target section, Phase 0 items 0.25 to 0.54, Phase 1, 3, 5, 6 changes. Hash record refreshed. Its header still says "being retargeted"; update that line once 14.1 passes. Ticking plan items is the user's job. |
| `brainstorm.md` | Targeted edits done (task 13.1, 15 hunks). Nine places outside the design table still describe the old design; listed in `docs/evidence/free-tier/source-doc-edits.md` under "Not changed, for a later decision". |
| Phase 1 spec (`cairn-phase-1-evidence-search`) | Revised for free tier, not started: 39 requirements, 92 leaf tasks, 30 properties, user steps PU-1 to PU-13. |
| Deployed anywhere | No. Nothing was ever deployed or called live (no Neon, Render or Cloudflare call by any agent). |

### Can it be deployed on Cloudflare, Render and Neon free tiers?

The repo contains everything needed and each piece is tested locally, but it is untested live, and the last full run is missing.

Ready:
- `render.yaml`: one free Docker web service running `cairn-combined`, `/healthz` check, secrets are `sync: false`.
- `apps/web/wrangler.toml`, `public/_routes.json`, `public/_headers`, `functions/api/[[path]].ts` (same-origin `/api/*` proxy that adds `X-Cairn-Proxy-Token`). `make web-dist` builds `apps/web/dist`.
- Neon bootstrap from the laptop: `make env-neon`, `make remote-init` (uses `--allow-plaintext-password`, needs `verify-full`), `make remote-status`.
- `docs/deploy.md`: runbook with steps FU1 to FU9, each marked "Needs user".
- Local proof: the `freetier` compose profile (`ft-app` at 512 MB and 0.1 CPU, `ft-db` with a non-superuser admin like Neon). `make freetier-check` passed end to end on 3 Nov: idle peak 94 MiB (budget 300), load peak 95 MiB (budget 384), 0 failed requests, no OOM, no restart; managed matrix 13 passed, 1 skipped.

Not done or not proven:
1. The final `make gates` and `make phase0-exit` have not been run after the last changes (task 14.1 was dispatched twice and was cut off by a network error). Last known results: `make test-unit` 1808 passed, 1 xfailed; `make docs-check`, `licenses`, `audit` ok; `make secrets` clean after a comment fix in `models/fake.py`; `make phase0-exit-preview` ok. Treat these as unverified until 14.1 passes.
2. Only a live deploy can confirm: Neon accepts the generated role passwords and lets `neondb_owner` create roles and extensions (FV3); `wrangler pages deploy dist` keeps the hand-written `_routes.json` and picks up `functions/` (FV7); whether Render's health probes keep the service awake (FV6, FU5); real Workers AI response shapes (the contract fixtures were written from docs, not recorded).
3. Neon free budget: DBOS polls its database about 9.5 times a second while launched, so Neon stays awake whenever Render is awake. At 0.25 CU that is 186 CU-hours if Render never sleeps, against 100 free. Normal use (a few visits a day) costs about 19. Do not add a keep-alive pinger (ADR-0014, OQ-3).
4. The public Pages URL has no user authentication. The proxy token only protects the direct Render URL. Add real auth before any real data (threat model residual risks).

## 2. What to do next

1. Make sure Docker Desktop is running (`open -a Docker`), the machine is idle, and no `core` and `freetier` containers coexist (the Docker VM has ~3.83 GiB; host has 8 GB).
2. Task 14.1: `make gates`, then `make phase0-exit`. Fix root causes only; never weaken a test or gate. Then write `docs/evidence/free-tier/summary.md` (what ran, counts, RAM numbers, deviations, user-owned steps) and add the README index row in `docs/evidence/free-tier/`.
3. Small doc fixes found on the way (do them before 14.1):
   - `CONTRIBUTING.md` intro and one later line still say agents never edit `brainstorm.md` or `development_plan.md`. Reword: edited only by an approved spec, with `docs/evidence/phase-0/source-docs.sha256` refreshed alongside.
   - `docs/threat-model.md` TM-11 row cites `test_app_role_cannot_bypass_rls`; no function has that name. Find the real role/default-deny test and cite it (`scripts/check_docs.py` and its tests must still pass).
   - Header lines of `development_plan.md` ("being retargeted") once the gate passes. This file is a source document: after any edit, regenerate the hash record with `shasum -a 256 brainstorm.md development_plan.md > docs/evidence/phase-0/source-docs.sha256` and note it in `docs/evidence/free-tier/source-doc-edits.md`.
4. Checkpoints 7, 11 and 15 are verification only: they pass when 14.1's gates pass (checkpoint 7 also lists `make s2-smoke` and `make phase0-exit-preview`).
5. Then the user runs FU1 to FU9 from `docs/deploy.md` (accounts, secrets, deploys; agents must not). FU7 is `docker volume rm cairn_pgdata` (the old Phase 0 volume, nothing mounts it).
6. After that, execute the Phase 1 spec `cairn-phase-1-evidence-search`. Phase 1 needs ADR numbers 0006, 0007, 0008, 0011, 0012; 0009 and 0010 are for the spike tracker; next free is ADR-0016.

## 3. Architecture in one page

- Web: Cloudflare Pages static build. Pages Function forwards `/api/*` to Render with `X-Cairn-Proxy-Token`.
- API and jobs: one Render free service, `cairn-combined` (`apps/combined`): FastAPI plus DBOS in one process. Heartbeat cron `0 */5 * * * *`, backfill off.
- Database: Neon free PostgreSQL 18, pgvector, RLS. Lexical search is native full-text search (tsvector, GIN, `websearch_to_tsquery`, `ts_rank_cd`). Local image `pgvector/pgvector:0.8.6-pg18` pinned by digest, volume `cairn_pgdata-pgvector`. No ParadeDB or `pg_search` anywhere outside history (`tests/scripts/test_no_paradedb.py` enforces it over code and docs).
- Roles: `cairn_owner`, `cairn_app`, `cairn_dbos`; the Neon owner/admin passwords (BYPASSRLS) stay in `.env.neon` on the laptop and never reach Render. DBOS keeps its own database `cairn_dbos`; the transactional outbox (ADR-0011) stays for Phase 1.
- Managed mode: `cairn-db` auto-detects non-superuser admin (`rolsuper`). TLS: `CAIRN_DB_SSLMODE=verify-full`, `CAIRN_DB_SSLROOTCERT=/etc/ssl/certs/ca-certificates.crt` (`sslrootcert=system` loads no roots in the image, FV11). Extensions `vector` (>= 0.8.0) and `btree_gist`.
- Models: Workers AI over REST: `@cf/baai/bge-small-en-v1.5` (384 dim), `@cf/baai/bge-reranker-base`, answer `@cf/google/gemma-4-26b-a4b-it`, judge `@cf/zai-org/glm-4.7-flash` (answer and judge must be different model families; settings refuse the same family). Licenses: MIT, MIT, Apache-2.0, MIT. Anthropic and OpenAI are optional and raise `NotImplementedError` for now. Fakes are the default locally and in CI.
- Neuron budget: 10,000 free per UTC day; cap 9,000. Every attempt reserves first (`NeuronBudget`, Postgres store in `cairn_meta.provider_usage`, migration 0002). Reasoning tokens count as output (FV9); an empty answer with `finish_reason: length` is a failure.
- Decisions are in ADR-0013 (store, supersedes ADR-0001), ADR-0014 (deployment), ADR-0015 (models and budget).

## 4. Hard rules (keep them)

- No git commands, commits or pushes by agents. No live Neon, Render or Cloudflare calls by agents; FU steps are the user's. `make env-neon`, `remote-init`, `remote-status` and `workers-ai-live-check` are user-run and in no gate.
- Never delete, mount or reuse Docker volume `cairn_pgdata`. Never touch other projects' containers (`openwa-api`, `taskflow-*`).
- Never run profiles `core` and `freetier` together.
- Local `.env` is append-only through `make env-update`; keep `CAIRN_DB_PORT=55432` (host port 5432 is taken by another project; ft-db is 55433, ft-app 8010). Never print or read secret values.
- Do not change `canary_token` or its strict xfail. A pending decision on the `\x1f` canary question is open.
- Tests that build `ApiSettings` for `build_app` must pass `"CAIRN_PROXY_TOKEN": None` explicitly, because the local `.env` holds a token.
- A Make variable in `mk/*.mk` ending in `_IMAGE` is read by the license scanner as a container image and must carry a digest. Use another name (`NEON_IMAGE_REF`).
- `0001_baseline.sql` was edited in place once (checksum change recorded in ADR-0013 and a test). Do not edit applied migrations again; add `0003_*`.
- Docker Desktop sometimes quits; `open -a Docker` restarts it and containers auto-restart.

## 5. Useful commands

```sh
make help
make fmt-check lint types test-unit web-check compose-check docs-check licenses audit secrets
make up-db test-integration matrix migrations-check smoke   # core stack
make down && make freetier-check                            # ~8 min, idle machine
make phase0-exit-preview                                    # prints checks, no gates
make gates && make phase0-exit                              # final run, long
```

## 6. Key places

- Deploy artifacts: `render.yaml`, `apps/web/{wrangler.toml,public/,functions/}`, `mk/deploy.mk`, `.env.neon.example`, `docs/deploy.md`.
- Core: `packages/core/src/cairn_core/{config,bootstrap,cli,health,migrate}.py`, `apps/combined/`, `apps/api/src/cairn_api/proxy_token.py`.
- Providers: `packages/retrieval/src/cairn_retrieval/{cloudflare,models,llm}/`; live check `packages/retrieval/tests/live/`, `mk/providers.mk`.
- Evidence: `docs/evidence/free-tier/` (task notes, `verify/FV0` to `FV13`, RAM report), `docs/evidence/phase-0/`.
- Docs: `docs/adr/`, `docs/threat-model.md` (TM-01 to TM-16), `docs/acceptance-tests.md`, `docs/prd.md`, `CONTRIBUTING.md`.
- Specs: `/Users/pankajbaid/projects/.kiro/specs/{cairn-phase-0-foundations,cairn-free-tier-retarget,cairn-phase-1-evidence-search}/` (each has requirements.md, design.md, tasks.md).

## 7. Open points for the user

- U2 to U7 from Phase 0 are still open (see `CONTRIBUTING.md`); FU1 to FU9 in `docs/deploy.md`.
- Canary `\x1f` decision (pending).
- OQ-3: no keep-alive pinger (decided, but revisit if the demo's cold start hurts).
- Plan/spec mismatches to resolve in a later pass: Phase 1 Requirement 20.1 says only the section 16 locations change; Phase 1 design section 18 says B0 and B1 both use full-text search while the plan has B0 dense-only; AT-EC2 and AT-EC5 should count cold starts (acceptance list now reports them separately); the Phase 3 edge webhook receiver adds role `cairn_edge` and needs an ADR (ADR-0016) and a spec.
- The FT-12 and license table wording in the spec's `design.md` still says "Gemma terms"; Gemma 4 is Apache-2.0 (FV10), so OQ-5 needs no handling.

## 8. AWS deployment (added 2026-11-19)

The user chose AWS for everything except the database: Neon stays, models are Amazon Bedrock (ap-south-1), the app runs on the existing EC2 `t4g.small` behind `deploy/aws/cairn_waker.py`, which sleeps the container when idle so Neon's free hours last. Read [ADR-0018](docs/adr/ADR-0018-aws-deployment-neon-database-bedrock-models.md), [docs/deploy-aws.md](docs/deploy-aws.md) and [docs/evidence/aws/README.md](docs/evidence/aws/README.md).

Done: Bedrock embedder, LLM reranker, answer and judge adapters with a daily dollar cap (`packages/retrieval/src/cairn_retrieval/bedrock/`); `cairn-db init --embed-dim` (`cairn_core/embed_dim.py`); the waker; `scripts/aws_push_neon_secrets.py`; `infra/aws/cairn.yaml` (stack `cairn` is created); SSM parameters for the session secret, audit key and proxy token; the image is built on the host; the systemd unit is installed and enabled but **not started** (it needs the Neon parameters); a Caddy site `cairn.3-109-36-90.sslip.io` points at the waker.

**Update, same day:** AU1 to AU3 are done (Neon in ap-southeast-1), the waker is started, the corpus is seeded, the app answers with citations, idle stop works, and the live Phase 5 run is in `docs/evidence/phase-5/runs/2026-11-19-live/`. Repo: github.com/pankajbaid567/Cairn. Open: a larger real corpus and question set, the reranker ablation, a Neon owner password rotation (it was pasted in a chat), the rename. Never read `.env` or `.env.neon`.
