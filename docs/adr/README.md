# Architecture decision records

Cairn records its own architecture decisions in MADR 4.0 format, as a worked example of the decision memory it builds. ADRs here use four digits (ADR-0001). The decisions inside the Kestrel Pay scenario use three digits (ADR-007) and are fictional.

Start from [template.md](template.md). The decision log with ids D1 to D20 lives in [section 1 of the brainstorm](../../brainstorm.md#1-tldr-and-decision-log), and the [development plan](../../development_plan.md) says which phase writes which ADR.

## Index

| ADR | Title | Status | Date | Records |
|---|---|---|---|---|
| [ADR-0001](ADR-0001-postgres-paradedb-pgvector-store.md) | Store content, ACLs, search indexes and decisions in PostgreSQL 18 with ParadeDB and pgvector | superseded by ADR-0013 | 2026-10-27 | D5, D6, D7 |
| [ADR-0002](ADR-0002-dbos-durable-jobs.md) | Run durable jobs with DBOS Transact on PostgreSQL, in a separate database | accepted | 2026-10-27 | D10 |
| [ADR-0003](ADR-0003-bitemporal-decision-model.md) | Model decision history on two time axes with tstzrange, GiST exclusion and PostgreSQL 18 temporal keys | accepted | 2026-10-27 | D9 |
| [ADR-0004](ADR-0004-layered-authorization.md) | Enforce permissions in seven layers over mirrored ACLs, with row-level security as a backstop only | accepted | 2026-11-03 | D11, D12 |
| [ADR-0005](ADR-0005-evaluation-protocol.md) | Evaluate on three corpora and four variants with deterministic checks for permissions and time, and a calibrated judge for the rest | accepted | 2026-11-03 | D17 |
| [ADR-0006](ADR-0006-rls-first-retrieval-with-explicit-acl-predicate.md) | Run retrieval with row-level security and an explicit ACL predicate in every query | accepted | 2026-11-04 | PD1-02 (refines ADR-0004) |
| [ADR-0007](ADR-0007-workers-ai-embedder-and-reranker-defaults.md) | Embed with bge-small-en-v1.5 and rerank with bge-reranker-base, until the live S3 run decides | proposed | 2026-11-04 | D16 (refines ADR-0015) |
| [ADR-0008](ADR-0008-iterative-strict-vector-filter-strategy.md) | Filter vector search by ACL with iterative strict-order HNSW scans and ef_search 100 | accepted | 2026-11-04 | D7 (refines ADR-0013) |
| [ADR-0009](ADR-0009-decision-store-and-claim-visibility.md) | Store decisions as an event log with derived bitemporal rectangles, and show a claim only through a full support set | proposed | 2026-11-04 | Phase 2 (refines ADR-0003) |
| [ADR-0016](ADR-0016-edge-webhook-receiver-and-drain-before-serve.md) | Receive webhooks at the edge into a thin inbox, and drain permission events before any query is answered | accepted | 2026-11-04 | Phase 3 (amends ADR-0014) |
| [ADR-0017](ADR-0017-answer-workflow-as-plain-async-steps.md) | Run the seven-step answer workflow as plain async functions, not a LangGraph graph | accepted | 2026-11-04 | Phase 4 (changes D14) |
| [ADR-0010](ADR-0010-github-permission-events-and-polling.md) | Re-read GitHub's whole access state on any permission event, and poll on start and on a schedule while awake | proposed | 2026-11-04 | Phase 3 (S5, user-run) |
| [ADR-0011](ADR-0011-transactional-outbox-for-embedding-jobs.md) | Start embedding jobs from a transactional outbox, because DBOS has its own database | accepted | 2026-11-04 | D10 (refines ADR-0002) |
| [ADR-0012](ADR-0012-answer-and-judge-model-pair.md) | Answer with gemma-4-26b-a4b-it and judge with glm-4.7-flash, until the live pick decides | proposed | 2026-11-04 | D16 (refines ADR-0015) |
| [ADR-0013](ADR-0013-postgres-pgvector-fts-neon-store.md) | Store content, ACLs, search indexes and decisions in PostgreSQL 18 with pgvector and native full-text search, on Neon in production (supersedes ADR-0001) | accepted | 2026-11-03 | D5, D6, D7 |
| [ADR-0014](ADR-0014-free-tier-deployment-cloudflare-render-neon.md) | Deploy on Cloudflare Pages, one Render free web service in combined mode, and Neon free | accepted | 2026-11-03 | D18 (amends), D10 (confirms ADR-0002 on Neon) |
| [ADR-0015](ADR-0015-workers-ai-models-and-neuron-budget.md) | Embed, rerank and answer through Cloudflare Workers AI behind provider interfaces, with a neuron budget | accepted | 2026-11-03 | D16 (amends) |
| [ADR-0018](ADR-0018-aws-deployment-neon-database-bedrock-models.md) | Deploy on AWS with Neon as the database, Amazon Bedrock for models, and an app host that sleeps when idle | accepted | 2026-11-19 | D18, D16 (amends ADR-0014, ADR-0015) |
| [ADR-0019](ADR-0019-saas-tenancy-identity-and-audit-chain.md) | Multi-tenant SaaS on one database: restrictive row-level security, Cognito sign-in, roles in the database, and a hash-chained audit log with external anchors | accepted | 2026-11-19 | extends ADR-0004, ADR-0018 |

"Accepted" here means the decision is final for the MVP and can be reopened by a new ADR. The status in the file is the source of truth. `make docs-check` checks ADR-0001 to ADR-0005 and ADR-0013 to ADR-0015 and requires `status: superseded by ADR-0013` on ADR-0001 and `status: accepted` on the others. Merging them is a user step (U4 in the Phase 0 design).

### Reserved numbers

ADR-0006, ADR-0007, ADR-0008, ADR-0011 and ADR-0012 are written (Phase 1); ADR-0009 is written (Phase 2, proposed until spike S4 is measured); ADR-0010 is written (Phase 3, proposed until spike S5 is run live); ADR-0016 is written (Phase 3). ADR-0006, ADR-0007, ADR-0008, ADR-0011 and ADR-0012 belong to Phase 1. ADR-0009 and ADR-0010 belong to the spike tracker (Phase 2 and Phase 3). ADR-0013 to ADR-0015 are the free-tier retarget. ADR-0018 is the AWS deployment. ADR-0019 is the SaaS tenancy, identity and audit-chain design. The next free number is ADR-0020.

| ADR | Source | Question | Due | Refines |
|---|---|---|---|---|
| ADR-0006 | S2, RLS and EXPLAIN (Phase 1) | Do the native full-text (GIN) and pgvector indexes stay in use under FORCE RLS, or do we adopt SECURITY DEFINER retrieval functions? The 2 Nov 2026 run found the full-text index unused under RLS at 20,000 rows (ADR-0013). | 30 Oct 2026 | ADR-0013, ADR-0004 |
| ADR-0007 | S3, models (Phase 1) | Which Workers AI embedding model, dimension and reranker fit the latency target and the daily neuron budget? The defaults are `bge-small-en-v1.5` and `bge-reranker-base` (ADR-0015). | After the user-run live S3 | ADR-0015, ADR-0005 |
| ADR-0008 | S1, ACL-filtered retrieval (Phase 1) | Which vector ACL-filter strategy meets recall and latency from 1% to 50% selectivity? | 2 Nov 2026 | ADR-0013 |
| ADR-0009 | S4, extraction quality (spike tracker) | What decision granularity and extraction policy? | 3 Nov 2026 | ADR-0003 |
| ADR-0010 | S5, GitHub permission events (spike tracker) | Which events and API calls reveal access changes, and what polling interval? | 7 Nov 2026 | ADR-0004 |
| ADR-0011 | Transactional outbox (Phase 1) | Decided by the Phase 1 design: the import transaction writes an outbox row that a drainer enqueues, because DBOS has its own database. Recorded when Phase 1 builds it. | 31 Oct 2026 | ADR-0002, ADR-0014 |
| ADR-0012 | Item 1.14, answer and judge model pair (Phase 1) | Which answer and judge models, from different families, and what neuron budget? | After the user-run live pick | ADR-0015, ADR-0005 |

Spike S6 (the optional Graphiti baseline) records its result in `docs/results.md`, not in an ADR.

## Process

When to write an ADR:

* Any change to a decision D1 to D20 needs an ADR that supersedes or amends the one that recorded it.
* A choice that is hard to reverse or that later phases build on: a store, a role or permission rule, a data model, an evaluation protocol, a dependency that sets a license obligation.
* The outcome of a spike. The spike tracker in the development plan names the ADR number.

When not to: a naming choice, a library swap behind an existing interface, or anything a code review can reverse in an hour.

How to write one:

1. Copy [template.md](template.md) to `ADR-NNNN-short-title.md`, with the next free four-digit number and a lowercase hyphenated slug. Numbers are never reused, and one file exists per number.
2. Fill in the front matter: `status`, `date` (`YYYY-MM-DD`) and `decision-makers`. Use `proposed` while the ADR is under review.
3. Keep the required sections: Context and Problem Statement, Considered Options, Decision Outcome, Consequences and Confirmation. Confirmation names the concrete test, command or review that shows the decision is followed.
4. State what is still pending, and which later ADR will settle it. Label numbers as Proposed target, Estimate or Verify when they are not measured.
5. Add a row to the index above and run `make docs-check`. The checker verifies the files, front matter, sections and relative links.
6. Open a pull request. The ADR is merged when its status is `accepted`.

Supersession:

* ADRs are not edited to reverse them. To change a decision, write a new ADR and set the old one's status to `superseded by ADR-NNNN`. The new ADR names the one it replaces in its Context and Problem Statement.
* Small corrections (typos, a link, a clarified sentence, a result filled into a pending item) can be made in place, with the date updated.
* The checker holds an expected status for each ADR it checks (`superseded by ADR-0013` for ADR-0001, `accepted` for the others). When another one is superseded, update the checker's expectations in the same pull request.
* Cairn should later ingest these files as T1 evidence, since an accepted ADR is the strongest source tier (brainstorm section 6).
