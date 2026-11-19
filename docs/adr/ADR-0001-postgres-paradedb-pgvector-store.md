---
status: superseded by ADR-0013
date: 2026-10-27
decision-makers: Pankaj Baid
---

# ADR-0001: Store content, ACLs, search indexes and decisions in PostgreSQL 18 with ParadeDB and pgvector

Superseded by [ADR-0013](ADR-0013-postgres-pgvector-fts-neon-store.md) on 2026-11-03: the store is now plain PostgreSQL 18 with pgvector and native full-text search, on Neon in production. The text below is the original record and is not edited further.

Records decisions D5, D6 and D7 of the [brainstorm decision log](../../brainstorm.md#1-tldr-and-decision-log). Phase 0 design decisions PD-05 and PD-06 (bootstrap and roles) build on it. The spike outcomes that refine it are recorded in later ADRs (see Pending items).

## Context and Problem Statement

Cairn answers questions from versioned decision records and the evidence behind them, and it must answer only from sources the asker can open. That needs one place that holds source revisions, ACLs, chunks, embeddings, claims, bitemporal decisions, lineage and the audit log, and that can run lexical search, vector search and an ACL predicate in the same query.

The question: which store, and how many stores, keeps a permission revocation from having to chase copies of the same content across systems, while fitting an 8 GB laptop and a free, open source stack (principles 1 and 6 of brainstorm section 9)?

## Decision Drivers

* One source of truth: content, ACLs and derived artifacts change in one transaction boundary, so a revocation or a deletion cannot leave a stale copy in a second store.
* ACL predicates must be expressible inside lexical and vector queries, and RLS must work as a backstop.
* BM25 ranking quality for identifiers, error strings and ADR numbers, which dense vectors alone handle badly.
* Core profile of about 4 GB RAM (Estimate), with a 6 GB ceiling (D18).
* Free and open source by default, with licenses tracked.
* Exact, pinned versions and a reproducible compose stack.

## Considered Options

* PostgreSQL 18 through the ParadeDB image, with pg_search (BM25) and pgvector (HNSW) in the same database (chosen).
* PostgreSQL plus a separate search engine such as OpenSearch for lexical search and vectors.
* PostgreSQL plus a dedicated vector database such as Qdrant.
* PostgreSQL 18 with native full-text search and pgvector only (no ParadeDB).
* PostgreSQL with pgvectorscale for filtered vector search.

## Decision Outcome

Chosen option: "PostgreSQL 18 through the ParadeDB image, with pg_search and pgvector", because it is the only option that keeps content, ACLs, text index, vector index and audit behind one transaction boundary and one permission model, and it removes a second copy of every restricted chunk.

The three decisions it records:

* D5, store. One PostgreSQL 18 database, run from the ParadeDB image, holds revisions, ACLs, chunks, entities, claims, bitemporal decisions, lineage and audit. The image is pinned by an exact `*-pg18` tag and a sha256 digest. The required extensions are `pg_search`, `vector` and `btree_gist`, installed by the bootstrap command (not by the application role).
* D6, lexical search. ParadeDB BM25 through `pg_search`. Native PostgreSQL full-text search stays as the B0 baseline and as the fallback. ParadeDB is licensed AGPL-3.0, and it is not available on Neon, so a Neon-hosted database is out of scope.
* D7, vector search. pgvector HNSW with iterative scans enabled where the filter requires them. The final ACL-filter strategy is not fixed here. Spike S1 chooses it.

Revisit if (from the decision log):

* D5: S1 or S2 shows that ACL-filtered retrieval cannot meet recall and latency targets inside PostgreSQL.
* D6: AGPL-3.0 is unacceptable for a hosted demo, or ParadeDB misbehaves under RLS in S2.
* D7: filtered recall@10 falls below 0.95 of exact search at 5% selectivity (Proposed target).

### AGPL-3.0 note

ParadeDB and `pg_search` are AGPL-3.0, and Grafana in the obs profile is AGPL-3.0 as well. Cairn runs them as separate, unmodified services and does not link them into its own code. The repo stays open source, and any change to ParadeDB itself would be published (brainstorm risk 11, trigger: a closed hosted demo is needed). The repo license is a separate owner decision (open question OQ-1 in the Phase 0 design; recommendation Apache-2.0).

### Pending items

These are not settled by this ADR and stay pending until their spike finishes.

* Whether indexes stay in use under FORCE RLS with the non-owner role, and whether an array-overlap ACL predicate pushes down into the ParadeDB scan. Spike S2, due 30 Oct 2026. Outcome goes to ADR-0006 (RLS-first queries or SECURITY DEFINER retrieval functions). The Phase 0 S2 harness is the first evidence.
* The local embedding model, its dimension and the reranker. Spike S3, 30 Oct 2026. Outcome goes to ADR-0007.
* The vector ACL-filter strategy (post-filter, iterative scans, partitioning, partial indexes, exact search on pre-filtered sets). Spike S1, due 2 Nov 2026. Outcome goes to ADR-0008.
* Whether ParadeDB can include the integer-array ACL column in its BM25 index (Verify). The S2 harness records the outcome and falls back to a BM25 index over id and body.
* The exact ParadeDB tag and digest, and the pgvector version (iterative scans need at least 0.8.0) (Verify). Task 4.1 of the Phase 0 plan pins them and the `/readyz` output shows them.

### Consequences

* Good, because a revocation, a deletion or a purge touches one database, so lineage-driven purges cover text, vectors and caches without cross-store coordination.
* Good, because ACL predicates, RLS and the query live in SQL that can be tested and EXPLAINed (spike S2).
* Good, because the core profile has fewer containers to fit into the 4 GB Estimate.
* Good, because BM25 and vector scores can be fused in SQL (RRF) without leaving the database.
* Bad, because the database becomes a single point of load and failure for search and for transactions. A heavy index build competes with queries on an 8 GB laptop.
* Bad, because ParadeDB's AGPL-3.0 adds an obligation and rules out Neon and some managed Postgres services.
* Bad, because ACL-filtered HNSW search can lose recall at low selectivity (hypothesis H4 in brainstorm section 16). S1 measures it, and the answer may force partitioning or partial indexes.
* Bad, because `pg_search` and the image layout are tied to a specific PostgreSQL major version, so upgrades need a planned dump and restore.

### Confirmation

* `/readyz` returns 503 with `postgres_major_mismatch` unless the server major version is 18, and with `extensions_missing` unless `pg_search`, `vector` and `btree_gist` are installed. The smoke test (`make smoke`, requirement 17.6) checks that the response status is 200 and lists the three extensions.
* The baseline migration fails with a message naming every missing extension. `make migrations-check` applies all migrations twice on a fresh database and fails unless the second run applies nothing.
* `docker-compose.yml` references the database image by an exact tag ending in `-pg18` and a sha256 digest, checked by `make compose-check`.
* `make licenses` regenerates `docs/licenses.md` and records ParadeDB and Grafana as AGPL-3.0 images.
* ADR-0006 and ADR-0008, written when S2 and S1 finish, confirm or reopen D5 to D7 with measured results.

## Pros and Cons of the Options

### PostgreSQL 18 through the ParadeDB image, with pg_search and pgvector

* Good, because one transaction boundary covers content, ACLs, indexes and audit.
* Good, because BM25 supports filter pushdown, which suits ACL predicates (Verify in S2).
* Neutral, because pgvector HNSW filtering is approximate and needs iterative scans.
* Bad, because of the AGPL-3.0 license and the lack of Neon support.

### PostgreSQL plus a separate search engine (OpenSearch)

* Good, because it is mature for lexical search at scale.
* Bad, because every restricted chunk exists in two stores, and a revocation has to reach both.
* Bad, because the JVM service alone strains an 8 GB laptop.

### PostgreSQL plus a dedicated vector database (Qdrant)

* Good, because filterable HNSW is built for payload filters.
* Bad, because ACLs and vectors live in different systems, which breaks principle 1 of brainstorm section 9.
* Neutral, because Qdrant stays available as an optional baseline in the experiments profile (never on the demo path).

### PostgreSQL 18 with native full-text search and pgvector only

* Good, because it has no AGPL component and runs on any managed PostgreSQL.
* Bad, because native full-text ranking is weaker than BM25 for identifiers and rare terms.
* Neutral, because it remains the B0 baseline and the fallback if D6 is reopened.

### PostgreSQL with pgvectorscale for filtered vector search

* Good, because label-filtered DiskANN targets exactly the filtered-search problem.
* Bad, because it adds a Rust extension and a second index type to a stack that already has HNSW.
* Neutral, because S1 may still evaluate it as one of the strategies.

## More Information

* [Brainstorm section 9, architecture and stack decisions](../../brainstorm.md#9-architecture-and-stack-decisions)
* [Brainstorm Appendix C, spike plans S1 and S2](../../brainstorm.md#appendix-c-spike-plans)
* [Development plan, Phase 0](../../development_plan.md#phase-0-foundations-27-to-29-oct)
