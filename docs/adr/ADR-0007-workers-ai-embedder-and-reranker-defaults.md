---
status: proposed
date: 2026-11-04
decision-makers: Pankaj Baid
---

# ADR-0007: Embed with bge-small-en-v1.5 (384 dimensions) and rerank with bge-reranker-base, until the live S3 run decides otherwise

Refines [ADR-0015](ADR-0015-workers-ai-models-and-neuron-budget.md). Decision PD1-12 of the Phase 1 design. **Status proposed: the spike S3 has not been run live.** The numbers this ADR needs (Recall@5 per model, latency from the Render region, neurons per query) require real Workers AI calls with the user's token; an agent makes none (PD1-24).

## Context and Problem Statement

Which Workers AI embedding model and reranker give the best Recall@5 within the latency target (p95 of an answer at most 5 s, Proposed target) and the daily neuron allowance (9,000, split 5,000 for the deployment and 4,000 for laptop runs)?

## Considered Options

* `@cf/baai/bge-small-en-v1.5` (384 dimensions) with `@cf/baai/bge-reranker-base`: the defaults of ADR-0015, `vector(384)` in the schema.
* `@cf/baai/bge-base-en-v1.5` (768) or `@cf/baai/bge-m3` (1024): better recall possible, more neurons, and a schema migration plus a re-embed.
* No reranker (`CAIRN_RERANKER=none`): saves neurons and latency.

## Decision Outcome

Provisional choice: the defaults, because they keep the column at `vector(384)` and cost the least per 1,000 input tokens. The S3 harness decides with the rule from the design: among embedders within 0.03 Recall@5 of the best, take the fewest neurons per 1,000 input tokens, then the lower p95; keep the reranker only if its B1 Recall@5 is at least the no-rerank value and its p95 for 50 passages is at most 1,500 ms.

## Consequences

* Good: nothing in the schema depends on this ADR being final for the 384-dimension default.
* Bad: until S3 runs, retrieval quality on real embeddings is unmeasured. The fake-model Recall@5 recorded in `docs/evidence/phase-1/eval/` says nothing about it.
* A different dimension adds one migration (`vector(<d>)`, HNSW rebuild) and a re-embed (`cairn-import reembed --all`).

## Confirmation

Run `cairn-s3 run --mode live` (user-run), then `--mode replay` (agents) from the model cache, and replace this section with the measured table. `CAIRN_RERANK_MIN_SCORE` is set from the abstention separation it reports.
