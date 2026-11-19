# Evidence: AWS deployment (2026-11-19)

What was run, where, and what it showed. Procedure: [deploy-aws.md](../../deploy-aws.md). Decision: [ADR-0018](../../adr/ADR-0018-aws-deployment-neon-database-bedrock-models.md). Numbers are single runs on one host, not a benchmark.

## Built and checked

| Check | Result |
|---|---|
| `make lint`, `make types` (mypy strict on core), `make docs-check`, `make fmt-check` | pass |
| `make test-unit` | 2,111 passed, 0 failed, 0 skipped, 1 xfail (the known canary-separator gap). Before this work: 1,986 passed and 54 skipped because the database was down; this run had the local database up, so those 54 ran. 71 tests are new (Bedrock client, adapters and settings 31, dimension change 9, waker 23, secrets script 8) |
| `cairn-db` dimension change on a real PostgreSQL with pgvector (`packages/core/tests/integration/test_embed_dim_integration.py`) | 2 passed: empty table widened 384 to 1024 with the HNSW index rebuilt; vectors of another dimension block the change, nothing changes, `--reset-embeddings` deletes them and the new width takes a 1,024-dimension vector |
| Image `cairn-combined:aws` built on the `t4g.small` host (linux/arm64) from `infra/docker/python-app.Dockerfile` | built; the production configuration loads and validates inside it (`SearchSettings`, `IngestSettings`), providers are built as Bedrock adapters |
| CloudFormation stack `cairn` (policy on the instance role, artifact bucket, log group) | `CREATE_COMPLETE` |
| SSM `/cairn/*`: session secret, audit key, proxy token | created on AWS (64 random hex characters each), never printed |

## Bedrock through the app's adapters, from the host with the instance role (one run)

| Call | Model | Result |
|---|---|---|
| embed query and two documents | `amazon.titan-embed-text-v2:0`, 1,024 dimensions | norm 1.000; cosine to the PostgreSQL passage 0.679 and to the unrelated SQLite passage 0.246; 685 ms for the first three calls (includes the client's start-up) |
| rerank three passages (listwise, `openai.gpt-oss-20b`) | one call | scores 10, 0, 0 for the relevant passage first; 270 ms |
| answer | `openai.gpt-oss-120b-1:0` | 96 in / 40 out tokens, $0.000046, 337 ms |
| judge | `qwen.qwen3-32b-v1:0` | 40 in / 12 out tokens, $0.000011, 174 ms |

This shows the IAM policy, the model access, the request shapes, the response parsing and the pricing path work end to end. It says nothing about answer quality or Recall.

## Live on AWS with Neon (2026-11-19, one run each, not a benchmark)

Host: the shared `t4g.small` in ap-south-1 behind Caddy and the waker. Database: Neon (PostgreSQL 18.7, pgvector 0.8.7), region `ap-southeast-1` (Singapore; Mumbai was not used). Public URL: `https://cairn.3-109-36-90.sslip.io`.

| Step | Result |
|---|---|
| `make remote-init` on Neon | 12 migrations applied; embedding column widened 384 to 1024 |
| Runtime settings to SSM `/cairn/*` (`make aws-secrets`) | 4 parameters; owner, admin and edge passwords stay on the laptop |
| Seed: Kestrel Pay corpus (33 chunks) embedded with Titan v2 1,024-d, gold decisions loaded | 33 chunks, 33 embeddings, 14 decisions, 22 claims, 41 events |
| Cold start (first request after the container was stopped), through the waker and Caddy | 8 to 11 s with the image cached on the host (40 s on the very first start, which also pulled and created the container) |
| `/api/readyz` | 200: database, extensions, role `cairn_app`, schema 12 of 12, jobs |
| Neon round trip from Mumbai (readyz database check) | 677 to 863 ms for the probe, which opens a connection |
| Warm `/api/readyz` | 1.0 s |
| Persona login, search, answer through the page's API | working |
| Answer, "why did payment-service move from MongoDB to PostgreSQL and who approved it" | 8.4 s; 2 claims, both kept by the verifier, 0 dropped, `degraded: []` |
| Answer, "what did we decide about Kafka for ledger events and is it still in force" | 7.4 s; says the Kafka approach (ADR-012) is replaced by the outbox (ADR-018); 2 of 2 kept |
| Bedrock spend recorded in `cairn_meta.provider_usage` for the day, all runs above | 4,567 micro-dollars ($0.0046) |

## Two bugs found by running it for real

1. A macOS `tar` added AppleDouble `._*` files; one named `._0001_baseline.sql` made migration discovery fail and the container exit at start. `deploy/aws/package.sh` now sets `COPYFILE_DISABLE=1`, excludes `._*`, and refuses a tarball that has any.
2. The listwise LLM reranker returned 25 scores for 26 passages (a counting slip), so the pipeline degraded to the fused order (`degraded: ["rerank"]`) on every search. The prompt now asks for scores keyed by passage number and the parser accepts that (or a list of exactly the right length), so a miscount cannot shift scores onto the wrong passage. After the change, two answers ran with `degraded: []`. A failed reply now logs `bedrock.rerank.bad_reply` with the stop reason and the first 160 characters. Also fixed: the importer and the outbox drainer assumed Workers AI budgets and model keys (`UnpricedModel` on the first embedding run); both are now provider-aware.

## Phase 5 evaluation with the real models (2026-11-19, one run)

Models: Titan v2 (1,024-d), `gpt-oss-20b` LLM reranker, `gpt-oss-120b` answers, `qwen3-32b` judge. Database: local PostgreSQL 18 with pgvector, the Kestrel Pay corpus (33 chunks), 30 dev and 16 test questions. Raw files: `docs/evidence/phase-5/runs/2026-11-19-live/`, tables in `docs/results.md`.

| Result | Dev (30) | Test (16) |
|---|---|---|
| Recall@5, B0 (BM25 + vectors) / B1 (+ rerank) | 0.982 / 1.000 | 1.000 / 1.000 |
| Permission leaks, all four variants | 0 | 0 |
| p50 latency B0 / B1 | 144 / 479 ms | 128 / 424 ms |
| Reranker degraded to the fused order (`bad_reply`) | 2 of 30 queries | 0 of 16 |
| Answers (B3): abstention decided correctly | 22 of 30 | 9 of 16 |
| Answers (B3): gold sources cited (recall) | 0.52 | 0.29 |
| Dropped claims (verifier) | 1 | 0 |

Deterministic suites: as-of status accuracy 12 of 14 (0.857); both misses are questions where the asker cannot read the PR that carries the status change, so the system correctly says "accepted" and not "implemented"; must-not-cite violations 0 of 4; permission leaks 0.

How to read this. Retrieval is saturated: with 33 chunks Recall@5 is about 1.0 for every variant, so this corpus cannot show that the reranker or the vectors help, and it does not. The weak spots are in the answers: abstention was right on 31 of 46 questions (67%), and the answers cite the gold sources only 29% to 52% of the time (they often cite a different, valid source; this metric counts only the labelled ones). The reranker still returns a miscounted list on a few queries. Next steps: a larger real corpus (PEPs or KEPs), 150 or more questions, human-checked judge labels, and the reranker ablation on that corpus.

## Not yet verified

- Neon compute hours over a week of use. (Idle stop was seen working: about 20 minutes after the last request the waker reported `app_running: false`.)
- The LLM reranker against no reranker (ablation), and Titan against Cohere embeddings.
- Mumbai as the Neon region (a project can only be created in one region; Singapore adds latency to every database call).

## Found while checking, not caused by this work

`make secrets` reports three findings: `CAIRN_GITHUB_WEBHOOK_SECRET` and `CAIRN_GITHUB_PRIVATE_KEY` field definitions in `packages/core/src/cairn_core/config.py` (Phase 3, they hold alias names, not secrets) and the planted canary token in `apps/api/tests/integration/test_canary_sync_states.py`. They were not fixed or allowlisted here.
