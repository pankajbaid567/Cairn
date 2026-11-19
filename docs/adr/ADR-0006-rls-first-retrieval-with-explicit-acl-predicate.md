---
status: accepted
date: 2026-11-04
decision-makers: Pankaj Baid
---

# ADR-0006: Run retrieval with row-level security and an explicit ACL predicate in every query

Refines [ADR-0004](ADR-0004-layered-authorization.md) (layers L2 and L3) and [ADR-0013](ADR-0013-postgres-pgvector-fts-neon-store.md). Decision PD1-02 of the Phase 1 design. Evidence: spike S2 ([2026-10-27 report](../spikes/S2/2026-10-27-report.md), re-run on native full-text search in [task 3.5 notes](../evidence/free-tier/task-3.5-notes.md)).

## Context and Problem Statement

Retrieval must return only rows the asker may read. ADR-0004 puts the check in two places on purpose: an explicit predicate in every query (L2) and row-level security on the tables (L3), so that a missing predicate is a bug that RLS still contains. Open question 2 asked whether RLS alone keeps the full-text and vector indexes usable, or whether retrieval should run through SECURITY DEFINER functions that apply the predicate explicitly.

## Considered Options

* RLS-first: policies on every protected table plus the explicit `acl && groups` predicate in every registered query, run as the non-owner `cairn_app` role.
* SECURITY DEFINER retrieval functions owned by a role that bypasses RLS, applying only the explicit predicate.
* RLS alone, with no explicit predicate.

## Decision Outcome

Chosen option: "RLS-first with the explicit predicate", because it keeps two independent checks, runs on Neon free with no role that bypasses RLS, and the measured cost is small at the corpus size of the MVP.

* Every retrieval SQL text lives in one registry (`cairn_retrieval.registry`) as a template with an `{acl:<alias>}` placeholder per protected relation. `run_registered()` is the only way to run it.
* Policies read `nullif(current_setting('cairn.principal_groups', true), '')::int[]`, so a missing, empty or stale `''` setting matches no row (default deny). Every runtime connection sets `prepare_threshold=None`, because psycopg's auto-prepare reused a plan across principals in S2.
* `CAIRN_ACCESS_MODE=definer` stays a documented alternative and is not built.

What S2 measured (20,000 rows, `pgvector/pgvector:0.8.6-pg18`): under FORCE RLS the GIN full-text index was used in 0 of 48 lexical and hybrid plans, because the operators `@@` and `&&` are not leakproof and PostgreSQL evaluates the policy first. The explicit predicate changed nothing for `cairn_app`. Without RLS the same query used the index in 24 of 24 plans and was 2x to 3x faster at 20% and 50% visibility. The absolute difference was at most about 3 ms per query. The cost under RLS grows with the number of rows the policy admits, not with the match count, and was not measured above 20,000 rows.

## Consequences

* Good: a query that forgets the predicate still cannot read a hidden row, and the registry test proves every query carries it.
* Good: no SECURITY DEFINER code, no role that bypasses RLS in the data path, nothing special on Neon.
* Bad: the full-text index is not used under RLS, so lexical cost scales with visible rows. If a larger corpus makes that matter, reopen with a new ADR and measure again.
* Bad: the explicit predicate adds a per-query cost that RLS alone would not pay; it is the price of two layers.

## Confirmation

* `apps/api/tests/integration/test_query_registry.py` (marker `matrix`): a static scan finds no module outside `queries.py` that names a protected relation; every registered template has an `{acl:alias}` for each protected alias; `EXPLAIN` as the RLS-bypassing oracle shows an `acl` condition on every protected scan; and with the predicate stripped (test build only, refused by every service) `cairn_app` returns exactly the rows the explicit predicate returns, and none with no, empty or stale principal setting.
* `packages/core/tests/integration/test_roles.py`: `cairn_app` owns nothing, lacks BYPASSRLS and reads zero rows from an RLS table with no policy.
