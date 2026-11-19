---
status: accepted
date: 2026-11-04
decision-makers: Pankaj Baid
---

# ADR-0011: Start embedding jobs from a transactional outbox, because DBOS has its own database

Refines [ADR-0002](ADR-0002-dbos-durable-jobs.md) and [ADR-0014](ADR-0014-free-tier-deployment-cloudflare-render-neon.md). Decision PD1-07 of the Phase 1 design.

## Context and Problem Statement

Importing a revision must also cause its chunks to be embedded. DBOS keeps its state in a separate database (`cairn_dbos`) for the `cairn_dbos` role, both locally and on Neon (ADR-0002, ADR-0014), so an import transaction in `cairn` cannot enqueue a DBOS workflow in the same transaction. Enqueueing after the commit could lose the job on a crash; enqueueing before could start a job for content that never committed.

## Considered Options

* Transactional outbox: the import transaction writes an `outbox` row next to the revision; a drainer enqueues a workflow per row.
* Enqueue after commit in the importer, with a periodic repair.
* Put DBOS tables in the content database.
* Poll for revisions without embeddings and skip the queue.

## Decision Outcome

Chosen option: "transactional outbox", because the row commits or rolls back with the revision, and everything after it can be retried.

* The drainer (`cairn_worker.outbox.OutboxDrainer`) claims up to 100 pending rows with `FOR UPDATE SKIP LOCKED`, enqueues `embed_revision_workflow` with the workflow id `embed-<event_id>`, marks the rows dispatched and commits. If the process dies between enqueue and commit the rows are sent again with the same id, and DBOS runs the workflow once. Delivery is at least once and the effects are idempotent: `embed_revision` inserts only vectors that are missing.
* On every start the drainer first queues outbox rows for current chunks that lack vectors, but only when the neuron budget has room for them, because the Render service sleeps and may have missed work. In the combined process `JobsRunner` starts the drainer after DBOS launches and stops it before `DBOS.destroy`.
* The workflow stops without retrying on `budget_exhausted` and `daily_limit`, because a retry cannot succeed on the same UTC day. Chunks without vectors stay findable by full-text search.
* Only `cairn_ingest` can read or write `cairn.outbox`; `cairn_app` has no privilege on it.

## Consequences

* Good: no job is lost between the import transaction and the queue, and no job exists for content that did not commit.
* Good: works unchanged on Neon with DBOS in its own database.
* Bad: a second moving part (the drainer) and a small delay (the poll interval, 2 s by default).
* Bad: the drainer keeps Neon awake while it polls. It polls only while the service is awake, and no keep-alive is added (ADR-0014).

## Confirmation

* `apps/worker/tests/integration/test_outbox_and_embedding.py`: each row is dispatched once with its event id; a failed enqueue leaves rows pending; a row locked by another claimant is skipped; repair queues only revisions missing vectors and respects the budget; and a real DBOS queue run embeds every chunk, with a second delivery of the same ids changing nothing.
* `packages/connectors/tests/integration/test_store.py`: an import writes the outbox row in the same transaction as the revision.
