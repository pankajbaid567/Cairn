---
status: accepted
date: 2026-11-03
decision-makers: Pankaj Baid
---

# ADR-0013: Store content, ACLs, search indexes and decisions in PostgreSQL 18 with pgvector and native full-text search, on Neon in production

Supersedes [ADR-0001](ADR-0001-postgres-paradedb-pgvector-store.md). It restates decisions D5, D6 and D7 of the [brainstorm decision log](../../brainstorm.md#1-tldr-and-decision-log) for the free-tier retarget (decisions FT-01 to FT-08 and FT-28 of the `cairn-free-tier-retarget` spec). The deployment that hosts this store is [ADR-0014](ADR-0014-free-tier-deployment-cloudflare-render-neon.md). The history of the store it replaces is in the History subsection at the end.

## Context and Problem Statement

ADR-0001 chose one PostgreSQL 18 database that holds content, ACLs, text index, vector index and audit, so that a permission revocation touches one store. It built the text index on an extension that Neon no longer offers (see History). Neon free is the production database of the free-tier deployment (ADR-0014), and a local stack that differs from production hides bugs, so the local store has to change too.

The questions: which store and which lexical search let the same database run locally, in CI and on Neon free while keeping the single transaction boundary of ADR-0001? And how does bootstrap create the roles and databases when the admin is not a superuser, as on Neon, where `neondb_owner` is the highest role a user gets and carries `BYPASSRLS`?

## Decision Drivers

* Local, CI and production run the same major version, the same extensions and the same bootstrap code path.
* One database keeps its single transaction boundary (ADR-0001), so a revocation or a purge still touches one store.
* Neon free limits (FV0): 1 GB storage per project, 100 CU-hours a month, no superuser. Neon lists pgvector 0.8.6 for PostgreSQL 18 (FV1).
* `neondb_owner` is a member of `neon_superuser` and has `BYPASSRLS`, so RLS applies only to a custom LOGIN role without that attribute. SQL-created roles are not members of `neon_superuser` (FV3).
* The runtime roles stay NOSUPERUSER and NOBYPASSRLS, own nothing and belong to no role (ADR-0004).
* No AGPL component in the data path.
* Measure what the planner does under FORCE RLS instead of assuming it.

## Considered Options

* PostgreSQL 18 with pgvector and native full-text search, Neon in production, and a bootstrap that works with a non-superuser admin (chosen).
* Keep the Phase 0 store and find a host that allows it (a VM that runs the compose stack). Rejected in ADR-0014.
* Neon with its own replacement for the retired text extension.
* Dense vectors only, with no lexical index.
* PostgreSQL plus a separate search engine for lexical search.

## Decision Outcome

Chosen option: "PostgreSQL 18 with pgvector and native full-text search, Neon in production, managed-mode bootstrap", because it is the only option that keeps one database, runs unchanged on Neon free and on a laptop, and has no extension that a managed host can withdraw.

The decisions it records:

* D5, store (FT-01 to FT-03). One PostgreSQL 18 database. Locally the image is `pgvector/pgvector:0.8.6-pg18@sha256:2ba9ca5f2e7daa0f0e7723cba1ee9167bab54efd3640516a44ac1a928dd67e7a`, the index digest, so it works on arm64 and amd64. 0.8.6 is the pgvector version Neon lists for PostgreSQL 18; the newer 0.8.7 image is not used so that local equals production (FV1). The required extensions are `vector` (at least 0.8.0, for iterative scans) and `btree_gist`. `/readyz` returns 503 with `extensions_outdated` when `vector` is older. Compose mounts a new volume `pgdata-pgvector`; the old volume `cairn_pgdata` stays in place, is never mounted, reused or deleted by a make target, and `make db-reset` refuses any other volume name (`db-reset-guard`). The user removes the old volume (FU7).
* D6, lexical search (FT-05). A stored generated `tsvector` column with a GIN index, queried with `websearch_to_tsquery` and ranked with `ts_rank_cd`. It has no AGPL component and works on any managed PostgreSQL. `ts_rank_cd` reads no corpus statistics, which removes the side channel that corpus-wide BM25 term statistics give (FT-05, noted in the Phase 1 design). Ranking quality against BM25 is not measured here.
* D7, vector search. pgvector HNSW with iterative scans, unchanged. The filter strategy stays with spike S1 (ADR-0008).
* Bootstrap modes (FT-06 to FT-08). `cairn-db init` detects `superuser` or `managed` mode from the admin connection's `rolsuper` and `CAIRN_DB_ADMIN_MODE` can override it. Superuser mode is the Phase 0 behavior. Managed mode, built for `neondb_owner`:
  * the admin creates `cairn_owner`, `cairn_app` and `cairn_dbos` with CREATEROLE, and grants itself `SET` (INHERIT FALSE) on `cairn_owner` and `cairn_dbos` so it can create databases owned by them;
  * ALTER leaves out the SUPERUSER, REPLICATION and BYPASSRLS clauses, because a non-superuser may not name them, and bootstrap then reads all three back and refuses to continue if any is set on a runtime role (FV4);
  * `vector` and `btree_gist` are created by the admin directly in each database, because pgvector is not a trusted extension and an owner without superuser cannot create it, with or without `SET ROLE` (FV3 item 7, FV4); the revokes and grants on the database and on schema `public` run under `SET ROLE <owner>`, because run directly they only warn;
  * the admin keeps CONNECT on each database and gets CREATE on it only while an extension is created (FV4 extra grants 1 and 2);
  * the runtime roles end up NOSUPERUSER, NOBYPASSRLS, owning nothing and members of nothing, the same as in superuser mode.
* Role passwords in managed mode (OQ-1, decided by the user on 2 Nov 2026). Neon accepts no hashed password in CREATE or ALTER ROLE (FV3 item 2). `cairn-db init --allow-plaintext-password` therefore sends the role passwords as plaintext literals. The flag is accepted only in managed mode and only with `CAIRN_DB_SSLMODE=verify-full`; otherwise `cairn-db` exits 2 before it connects. Superuser mode, and managed mode without the flag, send SCRAM-SHA-256 verifiers. No statement, password or verifier is logged. The other option, setting passwords through the Neon API or console, was not chosen, and the roles must be created with SQL in any case: roles created through the console, CLI or API join `neon_superuser` and get `BYPASSRLS`, which the role test forbids.
* The `0001_baseline` in-place edit (FT-04). `0001_baseline.sql` required the old text-search extension (see History); its extension check array is now `['btree_gist', 'vector']` and nothing else in the file changed. The SHA-256 checksum moved from `5f77c7547931c0c4a563e51937905cbe37962baee0e43858f228f0c4670e3b1e` to `d718e6ff8a360be6e5a0a7df627b0b41a5fa474659c24b078dfda308cfe6efb0`. This is the only exception to forward-only migrations. A new migration cannot replace it, because `0001_baseline` itself would fail on a server without the extension, and no non-local database exists: the only database that recorded the old checksum is the retired local volume. A database that still records it stops with `ChecksumMismatch` before any migration runs.
* Local emulation of Neon (FT-08). The `freetier` compose profile runs its own server `ft-db` on tmpfs, where the image superuser creates one role, `cairn_admin` (LOGIN CREATEDB CREATEROLE BYPASSRLS, not a superuser), the shape of `neondb_owner`. Managed-mode bootstrap and the managed test matrix run against it. Because `cairn_admin` cannot create `vector`, `cairn-db emulate-managed-admin` also installs both extensions in `template1`, which emulates Neon's extension privilege.
* Spike host guard (FT-28). `cairn-s1 load` and `cairn-s2 run` refuse a database host that is not loopback or a compose service name unless `--allow-remote` is given, so one mistaken S1 load cannot fill Neon's 1 GB.

### What the S2 harness found on native full-text search

Run on 2 Nov 2026 at 20,000 rows on this image ([report](../spikes/S2/2026-11-02-report.md), one run with 3 repeats, measured on the 8 GB laptop). This is evidence for ADR-0006, which still decides RLS-first queries against SECURITY DEFINER functions.

* Under FORCE RLS as `cairn_app`, the full-text GIN index was used in 0 of 48 lexical and hybrid plans, with or without an explicit `acl && …` predicate in the query. The plans start from the ACL GIN index and apply `body_tsv @@ …` as a filter.
* The cause is leakproofness: the functions behind `@@` (`ts_match_vq`) and `&&` (`arrayoverlap`) are not leakproof (`proleakproof = f`), so PostgreSQL evaluates the policy first and cannot use either operator as an index condition. The same queries run by a superuser, with the explicit predicate, used `chunks_fts_idx` in 24 of 24 plans through a `BitmapAnd` with the ACL index, and were about 2 to 3 times faster at 20% and 50% visibility.
* Latency under RLS across 1% to 50% visibility was 0.10 to 4.8 ms for the lexical variants and up to 7.8 ms for the hybrid variants, and the gap to the superuser plans is at most about 3 ms at this size. The cost grows with the number of rows the policy admits. It was not measured above 20,000 rows or for a common term.
* HNSW with iterative scans returned 10 rows with recall at 10 of 0.99 to 1.00 at 5% to 50% visibility; with iterative scans off it returned 4 rows (recall 0.21) at 5% and 8 rows at 20%.

The 27 Oct 2026 numbers on the previous store are a different engine on different server settings and are not a like-for-like comparison.

### Consequences

* Good, because local, CI, the emulated profile and production share one image family, one extension set and one bootstrap code path.
* Good, because nothing in the data path can be withdrawn by a host or carries an AGPL obligation.
* Good, because a revocation, a deletion or a purge still touches one database.
* Good, because `ts_rank_cd` needs no corpus statistics, so rows the asker cannot see cannot shift the scores of rows they can.
* Bad, because native full-text ranking is weaker than BM25 for rare identifiers. This is unmeasured and Phase 1 evaluation (B0 against B1) is where it shows.
* Bad, because the full-text index is not used under RLS, so lexical cost under RLS grows with visibility. If it matters at scale, ADR-0006 may move retrieval into SECURITY DEFINER functions, which then makes the explicit predicate the only access check for that path.
* Bad, because managed mode sends passwords as plaintext statements over TLS (OQ-1). Neon stores them encrypted, and the exposure is limited to that statement on a `verify-full` connection.
* Bad, because the Neon-specific path (`neondb_owner` creating extensions through `neon_superuser`, the SET grants, password rules) is only proven by the user-run `make remote-init` (FU2). The emulation proves the plain PostgreSQL rules and pre-installs the extensions.
* Bad, because the `0001_baseline` edit invalidates the retired local volume's migration history, and because the pinned pgvector version has to follow Neon's.

### Confirmation

* `/readyz` lists exactly `btree_gist` and `vector` and returns 503 with `extensions_outdated` below 0.8.0: `packages/core/tests/unit/test_health.py`, `apps/api/tests/unit/test_main.py`, `tests/smoke/test_core_profile.py` (`make smoke`).
* `packages/core/tests/unit/test_migrate.py::test_a_database_with_the_previous_baseline_checksum_stops_with_checksum_mismatch` records the previous checksum; `make migrations-check` applies all migrations twice and fails unless the second run applies nothing.
* `make compose-check` and `tests/scripts/test_compose_config.py` check that `db` uses the pinned `pgvector/pgvector` image, mounts only `pgdata-pgvector`, that `make db-reset-guard DB_VOLUME=cairn_pgdata` exits non-zero, and that `ft-db` uses the same image.
* A scan test under `tests/scripts/` (its file name is in History) fails if the old extension or its vendor is named outside the history allowlist, and a second test there fails if this ADR names them outside its History subsection.
* Managed mode: `make matrix-managed` (the permission matrix against `ft-db` as `cairn_admin`, including `test_admin_bypasses_rls_and_is_not_a_runtime_role`, default deny and bootstrap idempotency), `make freetier-check`, and the unit and property tests in `packages/core/tests/unit/test_bootstrap.py` and `test_bootstrap_*_properties.py`, which cover the password form (`test_password_form_is_plaintext_only_with_the_flag_managed_mode_and_verify_full`) and the rule that no plaintext password reaches a log.
* Lexical search: `make s2-smoke` and `make s2-run`; `packages/eval/tests/unit/test_s2_queries.py` (the `websearch_to_tsquery` statement), `test_s2_analyze.py`, `test_spike_host_guard.py`.
* FU2 (`make remote-init`, `make remote-status`) confirms the Neon side.

## Pros and Cons of the Options

### PostgreSQL 18 with pgvector and native full-text search, Neon in production

* Good, because one database, one transaction boundary, and it runs on Neon free and anywhere else.
* Good, because the bootstrap code path is the same locally and on Neon.
* Neutral, because pgvector HNSW filtering is approximate and still needs iterative scans.
* Bad, because ranking is weaker than BM25 and the full-text index is unused under RLS.

### Keep the Phase 0 store and find a host that allows it

* Good, because it keeps BM25 ranking.
* Bad, because it keeps the AGPL component and rules out Neon, and every free VM option tried changed or reclaimed its allocation (ADR-0014).

### Neon with its own replacement for the retired text extension

* Good, because Neon names it in its migration guide for the retired extension (FV0).
* Bad, because it exists only on Neon, so local would differ from production. It was not evaluated beyond that.

### Dense vectors only

* Good, because it removes the lexical index.
* Bad, because dense vectors handle identifiers, error strings and ADR numbers badly (ADR-0001 driver), and B1 needs a lexical side.

### PostgreSQL plus a separate search engine

* Good, because it is mature for lexical search at scale.
* Bad, because every restricted chunk then exists in two stores and a revocation has to reach both. A JVM search service also does not fit the 512 MB limit of the free web service.

## More Information

* [Brainstorm section 9, architecture and stack decisions](../../brainstorm.md#9-architecture-and-stack-decisions)
* [S2 report, 2 Nov 2026](../spikes/S2/2026-11-02-report.md) and [task notes with the comparison](../evidence/free-tier/task-3.5-notes.md)
* Verify results: [FV1, image pin](../evidence/free-tier/verify/FV1.md), [FV3, what `neondb_owner` can do](../evidence/free-tier/verify/FV3.md), [FV4, the plain PostgreSQL probe](../evidence/free-tier/verify/FV4.md), [FV11, TLS roots](../evidence/free-tier/verify/FV11.md), [FV12, topic tokens under the simple configuration](../evidence/free-tier/verify/FV12.md)
* Revisit if: Neon moves its pgvector version, native full-text recall on C1 dev is too low for B1, or the lexical cost under RLS grows too fast at the Phase 1 corpus size.

### History

What ADR-0001 decided and why it no longer holds.

* ADR-0001 (27 Oct 2026) built the Phase 0 store on the ParadeDB image with the `pg_search` extension for BM25, pgvector for HNSW and `btree_gist` for temporal keys. ParadeDB and `pg_search` are AGPL-3.0, and ADR-0001 already noted that Neon did not support them.
* The 27 Oct 2026 S2 spike measured that store: the BM25 index was reached through a ParadeDB custom scan, and an explicit ACL predicate made the difference between tens of milliseconds and about a millisecond under RLS. Those results stay in [the 27 Oct report](../spikes/S2/2026-10-27-report.md) unchanged.
* On 19 Mar 2026 Neon blocked `pg_search` for new projects, and on 21 Sep 2026 it retired the extension from existing projects (FV0). With Neon free chosen as production, `pg_search` could not stay a required extension.
* The Phase 0 volume `cairn_pgdata` was initialized by the ParadeDB image, and its `postgresql.conf` preloads `pg_search`, which the plain pgvector image cannot load. That is why this ADR introduces a new volume and leaves the old one untouched.
* ADR-0001's status is now `superseded by ADR-0013`. Its text is not edited beyond that status and a pointer line.
* The scan that keeps the old names out of the repository is `tests/scripts/test_no_paradedb.py`; its allowlist exempts ADR-0001, this History subsection, the 27 Oct 2026 S2 report and results, and the evidence directories.
