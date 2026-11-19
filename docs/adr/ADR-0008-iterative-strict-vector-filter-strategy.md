---
status: accepted
date: 2026-11-04
decision-makers: Pankaj Baid
---

# ADR-0008: Filter vector search by ACL with iterative strict-order HNSW scans and ef_search 100

Refines [ADR-0013](ADR-0013-postgres-pgvector-fts-neon-store.md). Decision PD1-11 of the Phase 1 design, changed by measurement. Open question 1 of the brainstorm. Evidence: [S1 report](../spikes/S1/2026-11-04-report.md).

## Context and Problem Statement

pgvector's HNSW index returns the nearest neighbours first and applies the ACL filter afterwards. With a selective filter (a user who can read 1% to 5% of the chunks) the scan can return too few rows. Which strategy meets recall@10 of at least 0.95 against exact search, with a p95 of at most 150 ms at 5% selectivity (Appendix C, Proposed target)?

## Considered Options

* `iterative_strict` with `hnsw.ef_search = 40` (the plan's starting default), and the variants `iterative_relaxed`, `post_filter_ef40`, `post_filter_ef200`.
* `iterative_strict_ef100`: `hnsw.iterative_scan = strict_order`, `hnsw.ef_search = 100`.
* `exact_prefilter`: read the visible rows through the ACL index and sort exactly.
* Partial HNSW indexes for large groups (not built).

## Decision Outcome

Chosen option: "`iterative_strict_ef100`", set per transaction behind `CAIRN_VECTOR_STRATEGY`, because it was the only approximate strategy whose recall stayed at or above 0.99 at every selectivity while keeping the lowest p95.

The S1 benchmark (`cairn-s1 bench`) loaded 20,000 synthetic 384-dimension vectors into the real `cairn.embeddings` table, with its HNSW index and RLS policy, and ran every strategy as `cairn_app`, 30 queries per cell, against `exact_prefilter` under the same visibility:

| Selectivity | `iterative_strict` (ef 40) recall | `iterative_strict_ef100` recall | `post_filter_ef40` recall | `exact_prefilter` p95 |
|---|---|---|---|---|
| 1% | 1.00 | 1.00 | 1.00 | 2.6 ms |
| 5% | 1.00 | 1.00 | 1.00 | 3.4 ms |
| 20% | 0.92 | 0.99 | 0.75 (7.7 of 10 rows) | 23.1 ms |
| 50% | 0.94 | 1.00 | 0.94 | 36.6 ms |

At 1% and 5% the planner used the ACL index and an exact sort, so every strategy is exact there. At 20% and 50% the HNSW scan is used and `ef_search = 40` is too low to reach 0.95, which is why the default moved from 40 to 100. `iterative_strict_ef100` had p95 of 1.1 to 2.5 ms in every cell.

## Consequences

* Good: the default never returns fewer than 10 rows and meets the recall target at every measured selectivity.
* Bad: the benchmark is 20,000 synthetic vectors on a local Docker VM, not the demo corpus and not Neon. At the 33-chunk demo corpus every strategy is exact anyway. Re-run `cairn-s1 bench --rows 100000` before relying on it for a larger corpus.
* Bad: a larger `ef_search` costs a little latency at high selectivity; it was within noise here.

## Confirmation

`cairn-s1 bench` writes `docs/spikes/S1/<date>-report.md` and `-results.json`, runs only against a local database (it refuses other hosts without `--allow-remote`), and spends no neurons. Unit tests in `packages/retrieval` pin the strategy table and `CAIRN_VECTOR_STRATEGY` validation.
