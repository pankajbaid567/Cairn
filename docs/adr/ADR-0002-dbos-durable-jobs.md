---
status: accepted
date: 2026-10-27
decision-makers: Pankaj Baid
---

# ADR-0002: Run durable jobs with DBOS Transact on PostgreSQL, in a separate database

Records decision D10 of the [brainstorm decision log](../../brainstorm.md#1-tldr-and-decision-log), plus Phase 0 design decision PD-07 (separate DBOS database) and deviation X7 (same-transaction enqueue is deferred).

## Context and Problem Statement

Ingestion, extraction, invalidation and reconciliation are multi-step jobs that must survive crashes, retry safely and be idempotent per external event (principle 4 of brainstorm section 9). ACL changes must run ahead of content changes (revocation p95 <= 60 s, Proposed target). Cairn already runs PostgreSQL, and an 8 GB laptop cannot also afford a broker and a result store.

The questions: what runs the jobs, where do its state tables live, and what does Phase 0 promise about enqueueing a job together with the data write that triggers it?

## Decision Drivers

* No new stateful service (no Redis, no broker) in the core profile.
* Durable, resumable workflows with a stable workflow id that can be set to a webhook delivery id, for deduplication.
* Priority queues, so ACL jobs can run before content jobs.
* The API's runtime role must not gain access to job state, and job state must not sit beside restricted content tables.
* A free, MIT-licensed library with a Python SDK.

## Considered Options

* DBOS Transact (Python) on PostgreSQL, with its system tables in a separate database `cairn_dbos` (chosen).
* DBOS Transact with its system tables in the `cairn` content database.
* Celery with Redis.
* A hand-written Postgres job queue.

## Decision Outcome

Chosen option: "DBOS Transact with its system tables in a separate database `cairn_dbos`", because it gives durable workflows and queues on the PostgreSQL we already run, and the separate database keeps job state and DBOS's own migrations away from content tables and from the API role.

What is decided:

* D10. DBOS Transact (MIT) runs durable workflows and queues. Celery, Redis and kombu are not used. `make licenses` fails if any of them appears in `uv.lock`.
* PD-07. DBOS system tables live in the database `cairn_dbos`, schema `dbos`, owned by the role `cairn_dbos`. That role owns nothing in the `cairn` content database. The roles `cairn_app` and `cairn_dbos` are separate, and each has CONNECT only on its own database. The DBOS password reaches only the db-init and worker containers.
* Phase 0 scope. The worker launches DBOS against `cairn_dbos` and applies one schedule, `cairn-heartbeat`, for a no-op workflow. No content write, inbox or enqueue path exists yet.
* The worker does not use `cairn_app`. That role is the API runtime role, owns nothing, and never touches job state.

Revisit if (from the decision log): DBOS cannot express ACL-first priority, or ingestion stays below 50 events/s (Estimate).

### What is and is not guaranteed about enqueueing

Brainstorm section 3 (row 12) and the ingestion sequence in section 9 describe the receiver inserting an inbox row and enqueueing the workflow in the same transaction. This ADR does not promise that yet.

Guaranteed in Phase 0:

* A DBOS workflow that has started is checkpointed in `cairn_dbos` and is resumed after a worker restart. The heartbeat integration test observes a SUCCESS run of `cairn-heartbeat` within three schedule intervals.
* The runtime API role cannot read or write job state, because it has no CONNECT on `cairn_dbos`.

Not guaranteed in Phase 0:

* Same-transaction enqueue. A PostgreSQL transaction cannot span two databases. With job state in `cairn_dbos`, a content write in `cairn` and the enqueue of its workflow are two commits. A crash between them can leave a committed inbox row with no workflow (a lost enqueue), or, if the order is reversed, a workflow with no row.

Deferred to Phase 3 (plan items 3.4 and 3.5, the webhook receiver and the per-delivery workflow). Candidate approaches, to be tested and recorded in a follow-up ADR or an amendment to this one:

1. Inbox as the source of truth. The receiver commits the inbox row (keyed by delivery id) in `cairn` and returns 202. A DBOS enqueue step, using the delivery id as the workflow id, runs after the commit, and the scheduled reconciler (brainstorm section 12) enqueues any inbox row that has no workflow. Workflow ids deduplicate repeated enqueues, so the result is at-least-once enqueue with idempotent effects. This needs no shared transaction.
2. Co-locate the DBOS schema in `cairn`, under a role other than `cairn_app`, so the inbox insert and the enqueue can share a transaction if DBOS supports enqueueing on the caller's connection (Verify).
3. Keep the separate database and accept approach 1 permanently.

Verify before Phase 3: whether the pinned DBOS version can enqueue on a caller-supplied transaction, and which privileges its migrations need (Phase 0 task 6.1 records the DBOS version, the extensions its migrations create and its logger behaviour).

### Consequences

* Good, because there is no Redis or broker to run, secure or fit into 4 GB.
* Good, because workflow ids can equal webhook delivery ids, which makes retries and redeliveries idempotent.
* Good, because a compromised API role cannot read or alter job state, and DBOS's own migrations never touch content tables.
* Good, because the schedule, workflow and health path are proven in Phase 0 before any real job exists.
* Bad, because the same-transaction guarantee is not available while job state is in a separate database. Phase 3 must close the lost-enqueue window with the inbox and reconciler pattern or by revisiting co-location.
* Bad, because there are two logical databases to bootstrap, back up and monitor.
* Bad, because DBOS is a younger project than Celery, so feature gaps (priority handling, throughput) are a real risk. The revisit condition above is the exit.

### Confirmation

* The worker integration test (`make test-integration`, requirement 17.7) observes at least one SUCCESS run of the schedule `cairn-heartbeat` in `cairn_dbos` within three schedule intervals, with `CAIRN_HEARTBEAT_CRON` set to every 5 seconds in CI.
* The role test confirms `cairn_app` and `cairn_dbos` are members of no role, and the bootstrap tests confirm CONNECT on `cairn_dbos` is granted to `cairn_dbos` only and revoked from PUBLIC.
* `make licenses` exits non-zero if `celery`, `redis` or `kombu` appears in `uv.lock`.
* The worker healthcheck (`python -m cairn_worker.healthcheck`) fails when the heartbeat file is missing or older than 30 seconds.
* Plan items 3.4 and 3.5 must include a test that kills the receiver between the inbox commit and the enqueue and shows the reconciler recovering the delivery. This ADR is amended or superseded when that test is designed.

## Pros and Cons of the Options

### DBOS Transact with a separate database `cairn_dbos`

* Good, because job state is isolated from content and from the API role.
* Good, because DBOS runs its own migrations without needing rights in `cairn`.
* Bad, because enqueue cannot share a transaction with a content write.

### DBOS Transact with its tables in the content database

* Good, because the inbox insert and the enqueue could share one transaction, if DBOS supports it (Verify).
* Bad, because DBOS migrations and job tables would sit in the content database, which every migration and RLS invariant test would then have to account for.
* Bad, because the owner of the DBOS tables would need rights that sit awkwardly next to the rule that `cairn_owner` owns every content table.

### Celery with Redis

* Good, because it is the most widely used Python job stack.
* Bad, because it adds two stateful services, and job state lives outside the database that holds the data.
* Bad, because durable multi-step workflows with deduplication need extra code on top.

### A hand-written Postgres job queue

* Good, because it has no dependency.
* Bad, because durable workflow semantics, retries, schedules and priorities would be built and tested by hand, in a project whose time is meant for retrieval and permissions.

## More Information

* [Brainstorm section 12, ingestion and sync](../../brainstorm.md#12-ingestion-and-sync)
* [Brainstorm section 9, architecture and stack decisions](../../brainstorm.md#9-architecture-and-stack-decisions)
* [Development plan, Phase 3](../../development_plan.md)
