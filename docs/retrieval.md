# Retrieval

How a question becomes a cited, permission-filtered answer. Describes the code as built in Phase 1. Decisions are in [ADR-0004](adr/ADR-0004-layered-authorization.md), [ADR-0006](adr/ADR-0006-rls-first-retrieval-with-explicit-acl-predicate.md), [ADR-0008](adr/ADR-0008-iterative-strict-vector-filter-strategy.md), [ADR-0011](adr/ADR-0011-transactional-outbox-for-embedding-jobs.md) and [ADR-0015](adr/ADR-0015-workers-ai-models-and-neuron-budget.md).

## Pipeline

```
question ──► embed (Workers AI, outside any DB connection)
        ──► one REPEATABLE READ transaction as cairn_app:
              L1 snapshot   caller + group:public + current memberships, ACL version
              L2 + L3       full-text candidates  ∥  dense candidates   (ACL predicate + RLS)
              reciprocal rank fusion (k = 60)  ──►  chunk details for the top 30
        ──► rerank (Workers AI cross-encoder, outside the transaction)
        ──► pack to a token budget  ──►  answer model  ──►  strict JSON  ──►  cited answer
        ──► audit row (ids and hashes only)  ──►  response
```

* **B0** ranks by dense distance only; full-text results fill slots only when dense returns fewer than `limit` rows, and the run counts the filled slots. **B1** is full-text plus dense, RRF, then rerank. Both go through the same library the API uses, as `cairn_app` under RLS.
* **Degradation.** If the embedding call fails with a code the search can survive (`budget_exhausted`, `daily_limit`, `rate_limited`, `server`, `timeout`, `transport`, `bad_response`), the search continues on full-text candidates and the response lists `dense` in `degraded`. If rerank fails, the fused order is kept and `rerank` is listed. `not_configured`, `auth` and `bad_request` are configuration faults and propagate as 503 `models_unavailable`. An answer-model failure is 502 `llm_unavailable`, and an exhausted budget is 503 `model_budget_exhausted` with `Retry-After`; an uncited answer is never returned.

## Indexes (migration 0005)

| Index | On | Used for |
|---|---|---|
| `chunks_tsv_en_idx` (GIN) | `tsv_en`, stored generated: `to_tsvector('english', title || ' ' || text)` | stemmed prose |
| `chunks_tsv_simple_idx` (GIN) | `tsv_simple`, stored generated: `to_tsvector('simple', text)` | identifiers such as `t3_5` and `ADR-007` |
| `chunks_acl_gin_idx`, `embeddings_acl_gin_idx` (GIN) | `acl int[]` | the ACL overlap predicate |
| `embeddings_hnsw_idx` (HNSW, m 16, ef_construction 64) | `embedding vector(384)`, cosine | dense search |

The lexical query is built from the question's distinct word tokens joined with ` or `, because `websearch_to_tsquery` ANDs bare words and drops most rows for a natural question (`lexical_terms`, at most 32). The rank is `ts_rank_cd(tsv_en, …, 32) + ts_rank_cd(tsv_simple, …, 32)`. It reads no corpus statistics, so ranking leaks nothing about hidden documents.

## Permissions

Every retrieval SQL text is in `cairn_retrieval.queries` and runs only through `run_registered()`. Each template carries `{acl:<alias>}` for every protected relation it reads, rendered as `alias.acl && %(acl_groups)s::int[]`. RLS policies on `items`, `revisions`, `chunks` and `embeddings` use `nullif(current_setting('cairn.principal_groups', true), '')::int[]`, so a missing, empty or stale setting matches no row. `items.acl`, `revisions.acl`, `chunks.acl` and `embeddings.acl` are sorted copies of `item_acls`, updated in the same transaction as any ACL change.

The API role `cairn_app` can only read these tables and insert into `audit_log`; content is written by `cairn_ingest`. An unknown ACL reference rejects the document (`acl_unresolved`) instead of narrowing it silently.

## Models and neuron cost

| Role | Default | Cost (Estimate, `NEURON_RATES`) |
|---|---|---|
| Embed | `@cf/baai/bge-small-en-v1.5`, 384 dimensions | about 220 neurons for the whole demo corpus; about 1 per query |
| Rerank | `@cf/baai/bge-reranker-base` | one call per query, about 3 to 4 neurons for 30 passages |
| Answer | `@cf/google/gemma-4-26b-a4b-it` | about 37 per answer (reasoning tokens count as output) |
| Judge | `@cf/zai-org/glm-4.7-flash` | about 24 per judged answer |

Every call reserves its estimate first against the daily cap (9,000 neurons, split 5,000 for the Render service and 4,000 for laptop runs). The fakes are the default locally and in CI. See ADR-0007 and ADR-0012: the live comparison that confirms these defaults has not been run.

## Filter strategy (S1)

`CAIRN_VECTOR_STRATEGY=iterative_strict_ef100` sets `hnsw.iterative_scan = strict_order` and `hnsw.ef_search = 100` for the transaction. On 20,000 synthetic vectors: recall@10 of 1.00 at 1% and 5% selectivity (the planner used the ACL index and an exact sort), 0.99 at 20% and 1.00 at 50%, p95 between 1.1 and 2.5 ms. The plan's starting default (`ef_search = 40`) reached only 0.92 and 0.94 at 20% and 50% and is not used. Details and caveats: [ADR-0008](adr/ADR-0008-iterative-strict-vector-filter-strategy.md), `docs/spikes/S1/`.

## Full-text under RLS (S2)

At 20,000 rows the GIN full-text index was used in 0 of 48 plans under RLS (the operators are not leakproof), and used in 24 of 24 without RLS, where it was 2x to 3x faster. The difference is at most about 3 ms per query at this size, so the pipeline keeps RLS plus the explicit predicate ([ADR-0006](adr/ADR-0006-rls-first-retrieval-with-explicit-acl-predicate.md)). It was not measured above 20,000 rows.

## Answer composition

The prompt `answer-v1` passes the packed chunks as `<evidence id="E1" kind="…" tier="…" title="…">` blocks with `<` and `&` escaped, and says the blocks are quoted data. The model must reply with JSON: `{"abstain": bool, "answer": str, "claims": [{"text": str, "evidence": ["E1"]}]}`. Invalid JSON, `abstain: true`, or no claim backed by a packed block gives the constant reply "Not found in sources you can access." with `abstained: true`. Labels outside the pack are dropped, labels become citation ids `chunk:<external id>@<hash8>:<start>-<end>`, and `[E1]` markers become `[n]` indexes into `citations`. Every citation offset resolves to the exact source text (`resolve_citation`).

## Audit

One row per authenticated `/search` or `/answer`: the question as an HMAC-SHA-256 under `CAIRN_AUDIT_HASH_KEY`, the principal snapshot (id, groups, ACL version), the returned item ids and, for answers, the SHA-256 of the canonical answer JSON. Never question or answer text. The table is append-only for every role (a trigger blocks `UPDATE`, `DELETE` and `TRUNCATE`).

## Evaluation

`cairn-eval phase1` runs B0 and B1 on the 30 C1 dev questions against a freshly seeded database and writes Parquet, `run.json` and a summary. 18 of the 30 are eligible for Recall@5 (gold evidence that Phase 1 imports). With the deterministic fake models: B0 0.898, B1 0.981, 0 leaks, 0 citation failures. **These are fake-model numbers on a 33-chunk corpus; they are a regression baseline, not a quality result.** The real-model numbers need the user-run live evaluation (`make eval-phase1 EVAL_MODELS=live`).
