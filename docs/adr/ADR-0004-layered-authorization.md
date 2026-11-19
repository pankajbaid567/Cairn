---
status: accepted
date: 2026-11-03
decision-makers: Pankaj Baid
---

# ADR-0004: Enforce permissions in seven layers over mirrored ACLs, with row-level security as a backstop only

Records decisions D11 and D12 of the [brainstorm decision log](../../brainstorm.md#1-tldr-and-decision-log), and the database role model (Phase 0 design decision PD-06) that makes the RLS backstop real. The permission model is described in [brainstorm section 11](../../brainstorm.md#11-permission-model).

## Context and Problem Statement

Goal: a user or agent never receives content, derived claims, counts or other signals from a source they cannot open at query time, and loses access within the revocation SLO (p95 <= 60 s, Proposed target) after the source revokes it.

The hard parts are these. Sources have their own permission models (GitHub repos and teams, chat channels, meeting attendees). Derived content, such as a claim or a summary, can leak what its inputs say. Access changes while queries are in flight. PostgreSQL row-level security (RLS) is easy to bypass by accident: table owners bypass it, superusers and BYPASSRLS roles always bypass it, and referential-integrity checks run without it.

The questions: where is permission enforced, what does a derived claim inherit, and which database roles may the runtime use so that RLS stays a real backstop?

## Decision Drivers

* Fail safe: a missing principal, a missing setting or a missing predicate returns nothing.
* More than one independent layer, because any single layer can have a bug.
* Derived artifacts must inherit the strictest inputs, with no hidden counts and no hints.
* Access is evaluated at query time, never at the as-of time. An as-of question about August cannot bring back access that ended in September.
* Tests must be able to prove each layer: loud failures are preferred to silent filtering.
* No external authorization service for the MVP.

## Considered Options

* Mirrored source ACLs with time-aware memberships, enforced in seven layers plus audit, with RLS as a backstop (chosen).
* RLS only, as the primary enforcement.
* Application-level filtering only, with no database backstop.
* An external relationship-based authorization service (OpenFGA) from the start.

## Decision Outcome

Chosen option: "Mirrored source ACLs with time-aware memberships, enforced in seven layers plus audit, with RLS as a backstop", because independent layers catch each other's bugs, ACL data stays in the same database as the content it protects, and the backstop is verified by tests that fail loudly.

### D11: mirrored ACLs and the layers

Cairn mirrors each source's permission model into `principals`, `memberships` (with `valid_period`), `item_acls` and `acl_versions`. Memberships keep history for audit and for revocation tests, not to grant access. Enforcement has seven layers plus an audit record:

| Layer | Control | Fails safe because |
|---|---|---|
| L1 Principal snapshot | Resolve the user or delegated agent to principals and an ACL version at query start | Unknown memberships resolve to no groups |
| L2 ACL predicate | Every retrieval query joins `item_acls` for the snapshot | A test fails any query path without the predicate |
| L3 RLS backstop | FORCE ROW LEVEL SECURITY, a non-owner role without BYPASSRLS, principals set with `SET LOCAL`, default deny | A missing setting returns zero rows |
| L4 Late recheck | Recheck every packed item against current ACLs just before composing | Drops items revoked mid-query |
| L5 Claim-level projection | Show a claim only if one full support set is visible | Hidden support is never counted or hinted at |
| L6 Output guard | Scan the answer for canary strings and citations outside the visible set | Blocks and logs |
| L7 ACL-keyed caches | Cache keys include the principal-set hash and the ACL version | Revocation bumps the version |
| Audit | Append-only record of question, snapshot, items returned and answer hash | Feeds the canary matrix |

Ordering rules for changes in flight: ACL jobs run in a priority queue ahead of content jobs. Content for a scope waits for pending ACL jobs on that scope, and each query records the ACL version it used (the new-enemy rule, after Zanzibar). Deletions purge chunks, embeddings and cache entries through lineage. Embeddings are treated as sensitive as the text.

### D12: claim-level projection

A claim is visible if and only if at least one complete support set of it is visible to the asker. Precomputed summaries are shown only if every input is visible, and are otherwise regenerated for the asker's principal set and cached under that key. There are no hidden counts, no "1 result hidden" and no different wording for not-found. The standard reply, "not found in sources you can access", is identical whether or not hidden evidence exists. Reviewer edits never widen visibility.

### The role model

The runtime must never be able to bypass RLS. Four roles exist in the one PostgreSQL cluster, created and re-asserted by `cairn-db bootstrap` on every run:

| Role | Attributes | Owns | Used by |
|---|---|---|---|
| `postgres` | the image's superuser | the cluster | db-init bootstrap step and test fixtures only |
| `cairn_owner` | LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS | database `cairn`, schemas `cairn` and `cairn_meta`, every table | db-init migrate step and the CI migration gate, never the API |
| `cairn_app` | LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS NOINHERIT | nothing | the API at runtime |
| `cairn_dbos` | LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS | database `cairn_dbos` and its `dbos` schema | the worker's DBOS system database (see ADR-0002) |

Rules:

* `cairn_owner` owns every table in the `cairn` schema. `cairn_app` owns no relation, schema, function or type, and receives explicit grants from each migration. There are no default privileges.
* `cairn_app` and `cairn_dbos` are members of no role. Bootstrap exits non-zero and names the membership if it finds one, so the runtime cannot `SET ROLE` to a bypassing role.
* Every table in the `cairn` schema must have row-level security enabled and forced (`relrowsecurity` and `relforcerowsecurity`). A table with FORCE and no policy returns zero rows to the app role and to the owner.
* CONNECT on `cairn` and `cairn_dbos` is revoked from PUBLIC and granted only to the intended roles.
* The superuser password reaches only the database and db-init containers. The app password reaches only db-init and the API. The DBOS password reaches only db-init and the worker. Passwords reach PostgreSQL as client-computed SCRAM-SHA-256 verifiers and never appear in logs.
* `/readyz` reports `role_unsafe` and returns 503 if the connected role is a superuser or has BYPASSRLS.
* Proposed for Phase 1 (open question OQ-2 in the Phase 0 design): content writes by the worker use a separate `cairn_ingest` role, so the API's role never gains write grants. This is not decided here.

### The foreign-key rule

Referential-integrity checks run without regard to RLS and can act as a covert channel: an insert or delete that fails on a foreign key reveals that a hidden row exists. Rule: no foreign keys from less restricted tables into more restricted tables. Where a reference to a restricted row is needed, store the identifier without a constraint and validate it in a workflow. PERIOD foreign keys already lack cascade actions (ADR-0003), which pushes deletions into workflows too. Phase 0 has no foreign keys. The test for threat 11 (RLS bypass) lands in Phase 1.

### The S2 fallback

PostgreSQL runs policy conditions before user conditions that are not LEAKPROOF, which may stop the full-text and pgvector indexes from being used under RLS (brainstorm risk 2). Spike S2 checks this with EXPLAIN. The 2 Nov 2026 run on native full-text search found the full-text index unused under RLS at 20,000 rows (ADR-0013). If it fails, the fallback is SECURITY DEFINER retrieval functions that take the principal snapshot, apply the ACL join explicitly and are the only grant the app role has. RLS stays on for every other path. This choice, RLS-first queries or SECURITY DEFINER functions, is pending and is recorded in ADR-0006 when S2 finishes (due 30 Oct 2026). The layers above hold either way.

### Authorization service

OpenFGA or a similar service is deferred (D11). Revisit if more than two sources with nested group semantics are added, and then model them in OpenFGA. D12 revisit if regeneration pushes cost per query over budget, or testers find partial answers misleading.

### Consequences

* Good, because a bug in one layer is caught by another, and the backstop does not depend on application code being right.
* Good, because the RLS design fails loud: with `row_security` off, a query that would be filtered raises an error, so tests notice.
* Good, because the role model removes the common bypass paths (owner, superuser, BYPASSRLS, role membership) from the runtime.
* Good, because claim-level projection and the single not-found template avoid existence leaks through counts or wording.
* Bad, because seven layers cost latency and engineering effort. Regenerating summaries per principal set costs LLM calls (brainstorm risk 7).
* Bad, because mirrored ACLs lag their sources. The revocation SLO, the priority queue and the late recheck exist to bound that lag.
* Bad, because RLS may force a different retrieval shape if S2 shows indexes are ignored.
* Bad, because the no-foreign-key rule gives up some database-enforced integrity for restricted references.
* Bad, because a client agent can still leak what Cairn legitimately returns through its other tools. Zero observed leaks on a finite matrix is not proof.

### Confirmation

Active in Phase 0:

* Role test (plan item 0.17, `make matrix`, threat TM-11): `cairn_app` lacks SUPERUSER, BYPASSRLS, CREATEROLE, CREATEDB and REPLICATION, owns no relation, schema, function or type, is a member of no role, reads zero rows from a table with FORCE RLS and no policy, cannot disable RLS on it, and gets an error with `row_security` set to off. `cairn_owner` also reads zero rows, and the superuser reads every row (which is why runtime never uses it).
* Schema invariant test: it fails if any table in schema `cairn` lacks enabled and forced RLS.
* Bootstrap tests: a second run changes nothing, membership in any role makes bootstrap exit non-zero, and password text never appears in output.
* `/readyz` returns `role_unsafe` for an unsafe role.

Planned in later phases:

* Plan item 1.20: a query registry test fails if any retrieval SQL path lacks the ACL predicate.
* Plan item 1.21: with the L2 predicate removed in a test build, the app role still sees only allowed rows, and sees none with no principal setting.
* Plan items 1.22 and 1.23: canary matrix v1, and an identical not-found reply with and without hidden matches.
* Plan item 2.17: Ben sees ADR-007's status and claims C1 and C3, never C2, and the regenerated summary has no trace of C2 and no hidden count.
* The full canary matrix of about 390 assertions (Estimate): 6 personas by 13 paths by 5 states, with 0 observed leaks (Proposed target), and a revocation timing harness with p95 <= 60 s.
* ADR-0006, from spike S2, and ADR-0010, from spike S5 (GitHub permission events), refine this ADR with measured results.

## Pros and Cons of the Options

### Mirrored ACLs in seven layers plus audit, RLS as backstop

* Good, because defence in depth with a test per layer.
* Good, because ACL data sits next to content in one transaction boundary.
* Bad, because more moving parts and a mirroring lag.

### RLS only

* Good, because policy lives in the database.
* Bad, because one bypass (owner, BYPASSRLS, a covert-channel foreign key) defeats it, and it does not cover derived claims, caches or outputs.
* Bad, because index use under RLS is unproven until S2.

### Application-level filtering only

* Good, because it is flexible and easy to change.
* Bad, because a single missed predicate leaks, with nothing behind it.

### OpenFGA from the start

* Good, because relationship-based models handle nested groups well, and it supports search-with-permissions strategies.
* Bad, because it adds a service and a second copy of the permission graph that must stay in sync with the mirrored ACLs and with revocation timing.
* Neutral, because it stays the named next step if nested group semantics grow.

## More Information

* [Brainstorm section 11, permission model](../../brainstorm.md#11-permission-model)
* [Brainstorm section 15, threat model](../../brainstorm.md#15-threat-model)
* [Brainstorm Appendix C, spike S2](../../brainstorm.md#appendix-c-spike-plans)
* The Phase 0 threat model, `docs/threat-model.md`, assigns the test ids TM-01 to TM-14 (plan item 0.21).
