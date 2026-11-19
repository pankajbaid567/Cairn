# Live sync (Phase 3)

How GitHub changes and access revocations reach every query path. Decisions: [ADR-0016](adr/ADR-0016-edge-webhook-receiver-and-drain-before-serve.md) (edge receiver, drain-before-serve) and [ADR-0010](adr/ADR-0010-github-permission-events-and-polling.md) (events and polling, S5 still to run live). Code: `cairn_core/webhook.py`, `apps/web/functions/_lib/webhook.ts`, `apps/api/src/cairn_api/sync.py`, `packages/connectors/src/cairn_connectors/github/`, `apps/worker/src/cairn_worker/github_sync.py`.

## The loop

1. GitHub POSTs a delivery. The **edge receiver** (Cloudflare Pages Function, `/webhooks/github`) checks the HMAC, stores a thin row, answers 202 and wakes Render. Locally the API's `/webhooks/github` does the same.
2. The row lands in `cairn.inbox`: identifiers and a class only (`acl` or `content`), never payload text.
3. The **sync runner** (a thread in `cairn-combined`) drains the inbox every 5 seconds: ACL rows first (one permission re-read covers all of them), then content rows. Each content row fetches the current PR, issue or Markdown from GitHub and imports it if the hash changed.
4. Every query path first passes the **sync barrier**. See below.

## Events that count as permission changes

`membership`, `team`, `team_add`, `organization`, `member`, `public`, `repository`, `installation`, `installation_repositories`. Content events: `pull_request`, `pull_request_review`, `pull_request_review_comment`, `issues`, `issue_comment`, `push`. Others are acknowledged (202) and not stored.

## Drain-before-serve

* On start the API queues an ACL-class poll row before serving, and the runner re-reads GitHub's access state.
* A query waits up to `CAIRN_SYNC_WAIT_S` (default 20 s) for ACL rows received **before the request** to be applied, and otherwise answers 503 `permissions_syncing` with `Retry-After`. Content rows never block.
* Because the barrier closes the moment a permission event is stored, "revocation time" (the time until no path returns the content) is the time to store the event and does not include how long the full re-read takes; **the time until correct service resumes** is what freshness measures.
* The barrier covers search, answer, decision page, timeline and the decision index. The MCP server (Phase 4) must call the same barrier.

## Failure modes and recovery

| Failure | What happens | Recovery |
|---|---|---|
| Edge receiver returns 500 or is down | GitHub marks the delivery failed | The reconciler lists recent deliveries, redelivers failures (ON CONFLICT dedupes), and diffs object lists, enqueuing missing work at backfill priority |
| API asleep when the edge stores events | The edge also wakes Render | On start the poll row and pending rows are applied before any query is answered |
| GitHub API down during a pass | The pass fails, rows stay pending, attempts and the error are recorded | Queries stay closed (503) until it succeeds or the wait expires; the next pass retries; after 10 failed attempts a row is left for reconciliation |
| Duplicate or out-of-order delivery | Deduped by delivery id; state is fetched, not replayed | none needed |
| Lost event, no redelivery window left | The scheduled permission re-read (every 15 minutes while awake) and the object diff | automatic |

Runbook: `GET /sync/health` (signed in) shows counts of pending ACL and content rows, the age of the oldest pending row and the last applied time. A growing `oldest_pending_s` with `pending_acl > 0` means the runner is down or GitHub is failing: check the `sync.pass_failed` log lines (error class only), the GitHub App installation and `CAIRN_GITHUB_*` settings, then run `make seed-local`-style reconciliation by restarting the service.

## Deletion purge

When a file, PR or issue is deleted, or a repository disappears or loses all readers' access, `purge_items` removes the item, revisions, chunks, embeddings, pending embed jobs, lineage, the claim support rows that cite it and the decision events that came from it; rebuilds the version rectangles of affected decisions from the remaining events; marks dependent summaries stale; and, on a laptop with a model cache, forgets cached vectors and rerank scores for the removed text. A claim left with no full support set is invisible to everyone.

**Audit retention after deletion (decision for plan 3.25).** Audit rows keep a hash of the question and answer, item **ids** and the outcome, never text or titles. They are append-only and are retained after a source is deleted: they record that an access happened, and they hold nothing the deleted source said. Item ids in old audit rows simply resolve to nothing.

## Freshness badges

`Live` (a GitHub source synced within a day, nothing waiting more than 5 minutes), `Delayed` (an event waited more than 5 minutes), `Stale` (never synced, or not within a day) and `Imported` (no live feed). An answer carries the weakest badge among the sources it cites, and the list per source.

## Measured, locally (2026-11-04)

20 trials each of revocation and freshness, alternating warm and asleep (runner stopped at the change, restarted after a simulated 0.5 s wake). Raw rows: `docs/evidence/phase-3/timing/sync-timing-2026-11-04.parquet`; summary: `.json` next to it.

This measures Cairn's own pipeline: a signed webhook posted to the API receiver, the runner thread draining against an in-memory GitHub, and probes on search and answer. It does **not** include GitHub's delivery latency, a real Render cold start (about a minute, FV6), or a deployment's network. The deployment re-measure is plan 6.6.

## User-run steps (need your GitHub, Cloudflare and Neon accounts)

1. **Demo organisation and App (3.2, 3.3).** Create an organisation, push the rendered repos (`payment-service`, `ledger`, `mobile-app`, `platform-infra`, `security-policies`, `kestrel-sdk`, `eng-docs`), and create a GitHub App with read-only permissions (Contents, Pull requests, Issues, Members, Administration read) subscribed to the events above. Webhook URL: `https://<pages domain>/webhooks/github`. Put the webhook secret in the Pages project secrets and `CAIRN_GITHUB_WEBHOOK_SECRET`; the private key in Render's environment and `CAIRN_GITHUB_PRIVATE_KEY` (use `\n` for newlines); `CAIRN_GITHUB_ORG`, `CAIRN_GITHUB_APP_ID`, `CAIRN_GITHUB_INSTALLATION_ID`. Map GitHub logins to personas with a JSON file named by `CAIRN_GITHUB_USER_MAP`.
2. **Edge write path.** Create the `cairn_edge` role (`CAIRN_EDGE_DB_PASSWORD`, then `cairn-db bootstrap`), set the Pages secrets `NEON_SQL_URL` (`https://<neon host>/sql`), `NEON_EDGE_CONNECTION` and `API_ORIGIN`. Verify the Function's CPU time on the largest fixture is under 10 ms; if the Neon HTTP path fails, use Hyperdrive or Cloudflare Queues (ADR-0016).
3. **Spike S5 (3.1).** Add and remove members, teams, collaborators and change repository visibility on the demo org and record which events fire and which API calls reveal state in the ADR-0010 table.
4. **Live revocation demo (3.27).** Remove Ben from the security team on GitHub and watch his results disappear; repeat with the API asleep.
