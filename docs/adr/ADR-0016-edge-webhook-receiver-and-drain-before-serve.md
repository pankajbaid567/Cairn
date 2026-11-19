---
status: accepted
date: 2026-11-04
decision-makers: Pankaj Baid
---

# ADR-0016: Receive webhooks at the edge into a thin inbox, and drain permission events before any query is answered

Amends [ADR-0014](ADR-0014-free-tier-deployment-cloudflare-render-neon.md). Implements plan items 3.4, 3.8, 3.28 and 3.29. The Cloudflare and Neon behaviour marked Verify below is confirmed only by the user-run deployment steps in [docs/sync.md](../sync.md).

## Context and Problem Statement

The Render free service sleeps after 15 minutes idle and takes about a minute to wake. GitHub expects a webhook answer within 10 seconds and does not retry automatically. A delivery to a sleeping service would be lost, and a lost permission event means a revoked user keeps access until the next reconciliation. How are events recorded durably while the API sleeps, and how is it guaranteed that no query is answered with permissions older than an event already received?

## Considered Options

* Deliver webhooks straight to the Render service and rely on redelivery and reconciliation.
* A Cloudflare Pages Function that verifies and records a thin row in Neon, then wakes Render; the API drains before serving.
* Cloudflare Queues as the durable buffer (free plan: 10,000 operations a day, 24 hour retention).
* Keep Render awake with a pinger.

## Decision Outcome

Chosen option: "a Pages Function that records a thin row, plus drain-before-serve", because it answers inside the 10 second limit regardless of the API's state, stores no content, and needs no always-on process.

1. **Receiver.** `POST /webhooks/github` on the Pages origin verifies HMAC-SHA256 in constant time, classifies the event (`acl` or `content`), inserts `(delivery id, event, action, class, installation, org, repo, number)` with `ON CONFLICT DO NOTHING`, answers 202, then wakes Render with `GET /healthz` after the response. Any storage failure answers 500 so GitHub marks the delivery failed and reconciliation redelivers it. The API implements the same contract for local runs, and one set of signed fixtures tests both.
2. **Write path.** Neon's SQL-over-HTTP endpoint as the new role `cairn_edge`, which holds `INSERT` on `cairn.inbox` and nothing else (no `SELECT`, `UPDATE` or `DELETE`). Fallbacks if the HTTP path fails its Verify: Hyperdrive, or Cloudflare Queues.
3. **Fetch on notify.** The inbox says only that something changed. The drainer reads current state from GitHub, so duplicates are harmless, order does not matter, and a missed event is repaired by the next read.
4. **Priority.** ACL-class rows drain first, and content rows wait while any ACL row is pending, so a revocation always lands before new content is written (the new-enemy rule). Backfill work runs at the lowest priority.
5. **Drain-before-serve.** On start the API queues a permission poll as an ACL-class row; every query path (search, answer, decision, timeline, decision index, and the MCP server when it exists) first waits, up to 20 seconds, for ACL rows received before the request to be applied, and otherwise fails closed with 503 `permissions_syncing`. Content rows never block a query.
6. **Late recheck and caches.** Before an answer leaves, the cited items are read again with a fresh snapshot (L4). Cached answers are keyed by the principal set and the ACL version, which any sync or purge bumps (L7).

## Consequences

* Good: a revocation is effective for queries as soon as its event is stored, even while the service sleeps, because queries fail closed until it is applied.
* Good: the edge holds a credential that can only append identifiers; a leaked `cairn_edge` password lets an attacker queue fake "something changed" rows, which cost a GitHub read each and nothing more.
* Bad: while a permission event is unapplied, every query path returns 503, including for users it does not affect. The wait is bounded at 20 seconds and the sync pass is small.
* Bad: the webhook secret is held at the edge (a Pages secret) and in the local environment, one more place a secret lives (threat model TM-15).
* Bad: an attacker who can post validly signed events cannot be told from GitHub; the secret is the only gate.

### Confirmation

`apps/web/functions/_lib/webhook.test.ts` and `apps/api/tests/integration/test_sync_api.py` run the same signed fixtures; `test_the_edge_role_can_insert_into_the_inbox_and_do_nothing_else` checks the role; the barrier tests check 503 on every path; `packages/connectors/tests/integration/test_github_sync.py` checks ordering, out-of-order and duplicate delivery. Cloudflare CPU time (10 ms limit), the Neon HTTP endpoint, and a real GitHub delivery are checked by the user-run steps in docs/sync.md.
