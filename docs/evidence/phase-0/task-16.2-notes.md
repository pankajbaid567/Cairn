# Task 16.2 notes: ADR template, index and ADR-0001 to ADR-0005

Date: 27 Oct 2026. Only files under `docs/adr/` and this notes file were created. No code, `brainstorm.md`, `development_plan.md`, dependencies or `uv.lock` touched. No git commands run. Merging the ADRs is user step U4.

## Files

| File | Decision recorded |
|---|---|
| `docs/adr/template.md` | MADR 4.0.0 template with placeholders; notes which headings the checker requires. Not checked for front matter |
| `docs/adr/README.md` | Index table, reserved numbers ADR-0006 to ADR-0010, ADR process, supersession rules |
| `docs/adr/ADR-0001-postgres-paradedb-pgvector-store.md` | D5 to D7: one PostgreSQL 18 database from the ParadeDB image with pg_search BM25 and pgvector HNSW; AGPL-3.0 note; not on Neon |
| `docs/adr/ADR-0002-dbos-durable-jobs.md` | D10, PD-07, X7: DBOS Transact, no Celery or Redis, system tables in separate database `cairn_dbos`; same-transaction enqueue not guaranteed, deferred to Phase 3 |
| `docs/adr/ADR-0003-bitemporal-decision-model.md` | D9: `valid_period` and `recorded_period` tstzrange, GiST EXCLUDE, PG18 temporal keys for single-axis tables, corrections and backdating, the `state_as_of` oracle |
| `docs/adr/ADR-0004-layered-authorization.md` | D11, D12, PD-06: mirrored ACLs, layers L1 to L7 plus audit, claim-level projection, four-role model, FORCE RLS, no foreign keys into restricted tables |
| `docs/adr/ADR-0005-evaluation-protocol.md` | D17 (and the D16 pinning and judge rules): C1 to C3, B0 to B3, deterministic checks for permissions and time, calibrated judge from another family, canary matrix, ACL-filtered vector study |

All five ADRs have front matter `status: accepted`, `date: 2026-10-27`, `decision-makers: Pankaj Baid`, and the headings Context and Problem Statement, Considered Options, Decision Outcome, Consequences and Confirmation (Consequences and Confirmation are `###` under Decision Outcome, as in MADR 4.0). Each states a revisit condition taken from the decision log (ADR-0004 gives both the D11 and D12 conditions; ADR-0003 gives D9's).

## Marked pending

| Pending item | Where | Settled by |
|---|---|---|
| RLS-first queries or SECURITY DEFINER retrieval functions; index use under FORCE RLS; ACL predicate pushdown into ParadeDB | ADR-0001, ADR-0004 | ADR-0006 (S2, due 30 Oct) |
| Local embedding model, dimension, reranker | ADR-0001, ADR-0005 | ADR-0007 (S3, 30 Oct) |
| Vector ACL-filter strategy | ADR-0001, ADR-0005 | ADR-0008 (S1, due 2 Nov) |
| `int[]` ACL column in the BM25 index; exact ParadeDB tag and digest; pgvector at least 0.8.0 | ADR-0001 | S2 harness and task 4.1 (Verify V1, V5, V6) |
| Same-transaction enqueue of a job with its content write; DBOS enqueue on a caller transaction | ADR-0002 | Phase 3 items 3.4 and 3.5; Verify V2 (task 6.1) |
| Separate `cairn_ingest` write role | ADR-0004 | Proposed for Phase 1 (OQ-2), not decided |
| Date-to-timestamp mapping for the oracle | ADR-0003 | Phase 2 |
| C2 and C3 redistribution terms | ADR-0005 | Verify, open question 10 |

## Where the text differs from the design or brainstorm

- ADR-0002 states exactly what is lost with a separate database: a content commit in `cairn` and an enqueue in `cairn_dbos` are two commits, so a crash between them can lose an enqueue. The design only says "revisited in Phase 3". The three candidate approaches (inbox plus reconciler, co-location, accept the inbox pattern permanently) are mine and are labelled as candidates to test, not decisions. This also means brainstorm section 3 row 12 and the section 9 ingestion sequence ("insert inbox row and enqueue in the same transaction") do not hold in Phase 0.
- ADR-0003 adds a caveat the design does not state: the oracle works on dates and the database on timestamps, so Phase 2 must test the mapping. It also lists the PG18 temporal-key premise test as optional and not on the Phase 0 task list, since the design calls it optional and no task schedules it.
- ADR-0004 says `/readyz` reports `role_unsafe` and bootstrap fails on role membership, both from the requirements, and notes that Phase 0 has no foreign keys, so the foreign-key part of the TM-11 test lands in Phase 1. The design (§5) says the same; the Phase 0 role test covers only the role side.
- ADR-0005 records the D16 pinning and judge rules together with D17, since the brainstorm splits them across both decisions. It says Phase 0 starts with about 40 gold questions (design §11), versus about 150 at freeze.
- ADR-0001 refers to the repo license question (OQ-1) in the AGPL note. It does not decide it.
- `docs/threat-model.md`, `docs/temporal-model.md` and `docs/results.md` are named as plain text, not links, because they do not exist yet.
- The README states that the checker's `status: accepted` rule means a superseded ADR needs a checker update. That follows from the notes of task 15.1.

## Verification

```text
$ uv run python scripts/check_docs.py --only adr
docs-check: ok (adr)
$ uv run python scripts/check_docs.py --only links
docs-check: ok (links)
```

Manual checks: no em dashes, en dashes or exclamation marks in prose (the only `!` characters are in the template's HTML comments); no "ACME"; ADR-0002 contains `cairn_dbos`, "same-transaction" and "enqueue"; ADR-0004 contains `cairn_owner`, `cairn_app` and "foreign key". Links point to `../../brainstorm.md`, `../../development_plan.md` and sibling files only.

## Remaining user steps

- U4: review, commit and merge ADR-0001 to ADR-0005.
- Add the S2, S3 and S1 results to ADR-0006 to ADR-0008 as those spikes finish (plan items 1.1, 1.2, 1.17).
