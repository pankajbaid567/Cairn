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

## Not yet verified (needs the Neon steps AU1 to AU3)

- The first start of the container against Neon, `/readyz`, and the end-to-end check in the runbook.
- Cold-start time (first request after an idle stop) and Neon's compute hours under real use.
- The Neon region chosen and the Mumbai to Neon round-trip time.
- Retrieval quality and answer quality with these models (the Phase 5 live run).
- The LLM reranker against no reranker (ablation).

## Found while checking, not caused by this work

`make secrets` reports three findings: `CAIRN_GITHUB_WEBHOOK_SECRET` and `CAIRN_GITHUB_PRIVATE_KEY` field definitions in `packages/core/src/cairn_core/config.py` (Phase 3, they hold alias names, not secrets) and the planted canary token in `apps/api/tests/integration/test_canary_sync_states.py`. They were not fixed or allowlisted here.
