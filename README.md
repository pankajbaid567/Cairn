# Cairn

Permission-aware evidence search and decision memory for engineering teams. Ask why something was built the way it was, and get an answer that cites the documents, chats and meeting notes you are allowed to read, shows what was decided and when, and says "not found" instead of guessing.

> **Name check (plan 6.8), decision needed.** A web search on 2026-11-04 found several unrelated projects also called "Cairn" in the same space (for example `cranmer/cairn`, a decision-memory MCP server for research groups, and `isaacriehm/cairn`, persistent decisions for Claude Code). The name may cause confusion if this goes public. Renaming means changing the `cairn_*` packages, roles and docs; I have not done it. Decide before publishing.

## What it guarantees, and what it does not

**Guarantees (each backed by a test that fails if it breaks):**

* **No answer, snippet, citation, count or timeline reveals anything the asker cannot read.** Access is enforced in the database (row-level security) and again in every query, and the canary matrix plants secret tokens and checks that none reaches anyone who may not read them (50 tests, 0 leaks, including all five MCP tools for two agents acting for two users).
* **A revocation closes every query path before it finishes applying.** Once a permission event is stored, queries answer 503 `permissions_syncing` until it is applied.
* **Decisions are answered as of a date, and as known on a date.** "What held on 5 June, as we believed on 8 June?" has a stored, tested answer, derived only from the events the asker can read.
* **A claim is shown only if one full set of its sources is readable.** Deleting a source removes its text, vectors, cache entries and sole-supported claims.
* **Generated answers are verified.** A claim that its cited text does not support is dropped; if nothing survives, the reply is the one constant not-found message.

**Non-guarantees:**

* It is not a security product audited by anyone else. The data is synthetic.
* Answer quality with real models is **not yet measured** (see Results). Everything below labelled "fake" checks the pipeline, not the models.
* The verifier is lexical: it can pass a claim that rearranges true words into a false meaning.
* Public issues and pull requests are attacker-writable text; Cairn labels and fences them but a model may still quote them.
* Not multi-tenant; one organisation per deployment.

## Architecture (free-tier topology)

```
 GitHub ──webhook──▶ Cloudflare Pages Function ──▶ Neon (inbox, thin rows only; role cairn_edge)
                              │ wakes                          ▲
 Browser ──/api──▶ Pages Function (proxy) ──▶ Render free: cairn-combined ──┘
 Claude Code ──stdio──▶ cairn-mcp (your laptop) ──HTTPS + agent token──▶  (FastAPI + DBOS jobs + sync runner)
                                                                 │
                                       Neon free Postgres 18 (pgvector, FTS, RLS) · Workers AI (embed, rerank, answer)
```

One Render free service (512 MB, 0.1 CPU) runs the API, the job runner and the GitHub sync runner. Neon free holds everything, including a separate DBOS database. Cloudflare Pages serves the web app, proxies `/api`, and receives GitHub webhooks. Details: [docs/deploy.md](docs/deploy.md), [docs/sync.md](docs/sync.md), [docs/adr](docs/adr/README.md).

## Results

Full tables, generated from Parquet with `make eval-tables`, with 95% bootstrap intervals and every failure listed: **[docs/results.md](docs/results.md)**. Reading guide and error analysis: [docs/error-analysis.md](docs/error-analysis.md).

**The only completed run uses deterministic fake models (2026-11-04).** It shows the machinery works and gives regression baselines. It says nothing about real retrieval or answer quality; the targets (Recall@5 >= 0.85, as-of accuracy >= 0.85, judged on the C1 test split) are **not yet checked with real models**.

| Measured | Value | What it is |
|---|---|---|
| Permission leaks, all variants, both splits | 0 | fake-model run; deterministic |
| Canary matrix | 50 tests pass | includes sync states and all five MCP tools |
| As-of status, deterministic suite | 12 of 14 | the 2 misses are questions whose expected status depends on a pull request the asker cannot read; Cairn answers from what they can see ([explained](docs/error-analysis.md)) |
| Freshness p95, local, 20 trials (10 asleep) | 0.78 s | Cairn's pipeline against a fake GitHub; excludes GitHub delivery and Render's cold start |
| Revocation p95, local, 20 trials | 0.015 s | time until queries stop returning the content (fail closed); not time until service resumes |
| Answer workflow p95, local, fake models | 17.8 ms | excludes model latency |
| Vector filter recall@10 at 20% selectivity | 0.99 | `iterative_strict_ef100`, synthetic vectors, [S1](docs/spikes/S1/2026-11-04-report.md) |
| Memory of the free-tier container under load | about 102 MiB of 512 | `make freetier-check` |

Results are identified by the scenario and corpus hashes in [c1-freeze.json](docs/evidence/phase-5/c1-freeze.json); this tree is not a git repository, so there is no commit hash yet. C1 holds 46 questions (30 dev, 16 test), well under the planned ~150, so intervals are wide.

## Quick start (local, about 20 minutes plus image builds; not yet timed on a clean machine)

Needs Docker, [uv](https://docs.astral.sh/uv/) and Make.

```bash
make env                       # write .env with random secrets (refuses to overwrite)
make up                        # build and start the core stack, wait until healthy
make db-init                   # roles, extensions, migrations
make seed-local                # render the Kestrel Pay corpus and import it (fake embedder, no network)
make seed-decisions            # load the gold decisions, claims and conflicts
make decisions-demo            # ADR-007 archaeology and ADR-012 time travel against the API
make eval-phase5               # B0 to B3 on fake models, then: make eval-tables
```

Open the web app, pick a persona (Asha, Ben, Carla, Priya) and ask a question. Switching persona changes what you can see. For an agent: [docs/mcp.md](docs/mcp.md).

## Threat model, in short

Spoofed or over-privileged callers, cross-user leaks, prompt injection through source text, stale permissions, and hosting-provider exposure. Mitigations by layer, with a test per threat: [docs/threat-model.md](docs/threat-model.md). Review checklist: [docs/security-review.md](docs/security-review.md). Injection posture and what is sent to model providers: [docs/security.md](docs/security.md). Supply chain: [docs/supply-chain.md](docs/supply-chain.md).

## Limitations

* **Cold start.** Render free sleeps after 15 idle minutes and takes about a minute to wake; the web shows a waking state. No keep-alive is used on purpose.
* **Free-tier limits.** 512 MB and 0.1 CPU on Render, 0.5 GB data on Neon, 9,000 Workers AI neurons a day (5,000 on Render). See the [cost check](docs/deploy.md#free-plan-cost-check-checked-2026-11-04).
* **Deployment, GitHub org, live model runs and the video are not done**: they need accounts and people. The checklist of what remains is in [development_plan.md](development_plan.md).
* **Small evaluation set** (46 questions), one synthetic company.
* Not built: GitHub OAuth login (persona login only), admin view-as, a Query inspector, ablation toggles for L4/L5/L7, C2 and C3 corpora.

## Where things are

`apps/api` the FastAPI service · `apps/combined` the one-process deployment · `apps/worker` jobs and sync · `apps/mcp` the MCP server · `apps/web` React and the Pages Functions · `packages/core` settings, roles, migrations · `packages/retrieval` search, decisions, workflow, verifier · `packages/connectors` importers, GitHub sync · `packages/eval` scenario, runners, tables · `docs/` everything written down.
