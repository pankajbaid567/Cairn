# Cairn PRD v1: decision memory for engineering teams and their agents

> Status: v1, written 27 Oct 2026 in Phase 0. Owner: the Cairn developer. Source: [brainstorm.md](../brainstorm.md) sections 1, 4, 6 to 8, 16 and 19. Phased work: [development_plan.md](../development_plan.md).
>
> Labels follow the brainstorm. Proposed target: a number chosen before measuring, to be confirmed or changed by evidence. Estimate: a guess not yet measured. Verify: not yet confirmed. Illustrative: an example on synthetic Kestrel Pay data. Nothing in this PRD is implemented or measured yet; Phase 0 builds the skeleton only.

## Summary

Cairn is decision memory for engineering teams and their coding agents. It answers "why is it like this, since when, who decided, and is that still true" with cited evidence, and it uses only sources the asker can open. Decisions are versioned records with two time axes (when something was true, and when Cairn recorded it). Permissions are enforced in layers and tested with planted canary strings. The MVP ships by 20 Nov 2026 on synthetic and public data only (D20).

The decisions D1 to D20 in the brainstorm are final for the MVP. Any change to one of them needs an ADR (see [ADR-0001](adr/ADR-0001-postgres-paradedb-pgvector-store.md) to [ADR-0005](adr/ADR-0005-evaluation-protocol.md) for the first five records).

## Problem and why now

Engineering decisions are made across pull requests, design docs, chat threads and meetings. What survives is usually the code and a ticket status. The reasons, the rejected options, the approver and the date a decision stopped being true end up scattered or lost.

- People work around this by asking colleagues, which costs time on both sides.
- Coding agents cannot work around it. They know only what they are handed, and they forget between sessions.
- When an answer does come back, developers often do not trust it.

So the problem is not search. It is producing a decision history that is cited, current, time-aware and permission-correct, for people and agents alike.

Why now: coding agents are entering daily work and need the same context a senior engineer carries. Several vendors sell a context layer, but none publishes decisions as versioned, bitemporal records with tested permission behavior. That gap is what Cairn's evaluation is built to show or to disprove. The brainstorm does not claim Cairn beats existing products; only the finished system and its published results table can support that.

## Personas

All personas are fictional members of Kestrel Pay, a synthetic payments company with about 60 engineers (Illustrative). Names are first names only.

| Persona | Situation | Job to be done | What great looks like |
|---|---|---|---|
| Asha, payments engineer | Owns payment-service and gets the same "why PostgreSQL" question every month | Answer recurring why-questions once, with evidence | Sends one decision page that stays current |
| Ben, new mobile engineer | Two weeks in, needs to change a shared API | Understand constraints before touching code | A cited brief in minutes, with what is disputed and who to ask |
| Dev, engineering manager | Runs planning and owns decision hygiene | Know which decisions are stale, contradicted or unowned | A review queue with owners |
| Agents acting for a user (for example Claude Code acting for Asha) | Asked to refactor the connection pool | Load the decisions and constraints that apply before editing | Calls pre_change_brief and gets only what the user may see |
| Priya, security admin | Accountable for exposure through AI tools | Prove restricted or revoked content never reaches users or agents | Audit trail, canary matrix results and revocation timings |
| Carla, HR partner | HR notes live in a restricted channel the tool can read | Be certain HR content never appears in engineering answers | Restricted content is invisible everywhere, including summaries and counts |

Key moments:

- Before a change: is there a reason this is like this.
- During review: does this PR contradict an accepted decision.
- In onboarding week: which decisions must I know for this service.
- After an incident: when did we decide this, and was it ever reverted.
- After an access change: did access to private context actually end.

## Jobs to be done

1. When I am about to change code I did not write, I want the decisions and constraints that apply to it, so I can avoid repeating a past mistake.
2. When someone asks why something is built a certain way, I want to send one link with evidence, so I can stop re-explaining.
3. When sources disagree, I want both versions with dates and owners, so I can resolve the conflict instead of guessing.
4. When I delegate a task to a coding agent, I want it to load the context I would, limited to my access, so I can trust its plan.
5. When access changes, I want proof that restricted content is no longer reachable, so I can sign off on AI tooling.

## User stories

Each story is delivered by the phases listed in the traceability section. The acceptance test list in `docs/acceptance-tests.md` (task 16.5) gives each one a row with named tests; the pointer column names the observable result that test must show.

| Id | Story | Acceptance pointer |
|---|---|---|
| US1 | As Ben, I ask "why does payment-service use PostgreSQL" and get a cited answer with the approval date and the implementing PR. | The answer cites the 7 Mar review notes and PR #123, shows the approval and merge dates, and every citation supports its claim. |
| US2 | As Ben, I open the decision page and see its timeline, current status and any superseding decision. | The page lists lifecycle events in order, shows the current state, and links the superseding decision when there is one. |
| US3 | As Asha, I ask what we believed on 10 March and get the as-of answer, clearly labeled. | The answer says accepted and pending, is labeled with its valid and recorded dates, and does not mention PR #123, which merged on 12 Mar. |
| US4 | As Dev, I work a review queue of candidate decisions and conflicts, and verify, edit, dispute or reject each one. | Each action changes the verification state, keeps the earlier version, and shows in later answers. |
| US5 | As Priya, I remove Ben from a team on GitHub and see the affected content disappear from search, answers, decision pages and MCP within the revocation SLO. | Ben's next query on every path returns nothing from the revoked source, and the audit view shows the measured delay. |
| US6 | As Carla, I confirm that HR canary strings never appear for any engineering persona on any path. | The canary matrix reports 0 observed leaks across personas, paths and states. |
| US7 | As Claude Code acting for Asha, I call pre_change_brief for a file path and receive decisions, constraints and owners within Asha's access. | The brief lists decisions anchored to the path, with owners and stale-doc warnings, and contains nothing outside Asha's access. |
| US8 | As Asha, I see when each source last synced and when an answer depends on stale evidence. | Answers carry a freshness line, and the sources screen shows last event, last reconciliation and lag. |
| US9 | As any user, I get "not found in sources you can access", with what was searched, instead of a guess. | An unanswerable question returns the not-found state with the searched sources, and no unsupported claim. |

## MVP scope

The MVP is the P0 set from the brainstorm feature list. Priority labels: P0 is the MVP, P1 if time allows by 20 Nov, P2 next, L later.

| Group | P0 features |
|---|---|
| Capture | GitHub App ingestion of pull requests, reviews, issues and Markdown; ADR and design-doc parser for MADR, KEP and PEP formats; Slack-export-format chat import; meeting-notes import with attendees in front matter |
| Understand | Decision detection and MADR extraction (candidates only); entity and alias resolution; code anchors from decisions to paths, services and PRs; supersession and reversion linking |
| Ask | Cited answers with conflicts, gaps and who to ask; as-of queries (true then versus believed then); decision timeline page |
| Maintain | Review queue to verify, edit, dispute or reject candidates |
| Govern | Source ACL mirroring with time-aware memberships; claim-level projection; append-only audit log of retrievals and answers; revocation timing harness |
| Agents | MCP stdio server with five read-only tools; pre_change_brief tool |

Product rules that shape the scope:

- AI proposes and humans ratify. Extraction output is always a candidate (D3).
- Contradictions are never resolved automatically. Both sides show with dates and trust tiers (D4).
- A claim is visible only if one complete set of its supporting evidence is visible, and no hidden counts are shown (D12).
- Permissions are enforced in seven layers, with row-level security as a backstop only (D11).

P1 if time allows by 20 Nov: conflict detection with trust tiers, "explain this file", query inspector, precedent search, stale badges, admin view-as (read-only, time-boxed, audited). P2 and later items are listed in the brainstorm, section 7.

Screens:

- Ask: question box, as-of control, answer card, evidence list and a not-found state that lists what was searched.
- Decision page: status and verification badges, outcome, rationale claims with citations, a timeline with an as-of slider, the supersession chain, anchors and open conflicts.
- Review queue: candidates, conflicts and unsupported decisions, with verify, edit, dispute and reject actions and an evidence diff.
- Sources and sync health: per source, last event, last reconciliation, lag, failures and a freshness badge.
- Access and audit: effective access per user, membership history, revocation timings and audit search.
- Query inspector: retrieved items, rerank scores and verification results. Filtered counts appear only in audited admin view-as.

Accessibility aims: keyboard-reachable controls, visible focus, labeled icon buttons, a timeline that also reads as a list, and status never shown by color alone, aiming at WCAG 2.2 AA patterns. Automated checks help, but full validation needs manual testing with assistive technologies and an expert review. This PRD does not claim compliance.

## Non-goals

Not in the MVP:

- Any write action on the agent path. The MCP server is read-only and adds no outbound channel.
- Live Slack, email, calendar or CRM connectors.
- Corpus-wide GraphRAG-style summaries.
- Real company data. The MVP uses synthetic and public data only (D20); real data needs a separate privacy review.
- Multi-tenant hosting, billing and SSO.
- Fine-tuned models.

Also out of scope for the claims Cairn makes: it does not prove it beats existing products, and zero observed leaks on a finite matrix is not proof of no leaks.

## Journeys

Illustrative, on Kestrel Pay data.

| Id | Journey | Persona | Delivers |
|---|---|---|---|
| J1 | Ask why. Ben asks why payment-service uses PostgreSQL, gets a cited answer card, opens ADR-007 and sees one stale wiki page flagged and Asha as the person to ask. | Ben | US1, US2, US9 |
| J2 | Time travel. Asha asks what the ledger event bus was on 10 Mar. Cairn answers Kafka (ADR-012), notes it was superseded on 15 Jul by ADR-018, and offers the "believed then" view. | Asha | US3 |
| J3 | Revocation. Ben was added to the security team for a one-week review. Priya removes him on GitHub. The ACL job runs ahead of content jobs, and Ben's next query no longer returns ADR-019 evidence. The audit view shows the timing. | Priya, Ben | US5, US6, US8 |
| J4 | Conflict. Dev opens the review queue: the API v1 deprecation date is 30 Sep in the design doc and 31 Oct in chat (ADR-015). Both sides show with tiers and dates. Dev records the resolution and its reason. | Dev | US4 |
| J5 | Agent brief. Claude Code, acting for Asha, calls pre_change_brief for db/pool.py and receives ADR-009 with its partial revert, owners and a stale-doc warning, then plans around them. | Agent for Asha | US7 |
| J6 | Ratify. Asha reviews a candidate extracted from a chat thread, edits the outcome, links the PR and marks it human-verified. It now appears in answers as verified. | Asha | US4 |

Adoption loops: every answer links a decision page and every decision page invites questions, so shared links bring new askers. Questions that hit gaps create review items, and resolved items improve later answers. Agents cite decision IDs in their plans, which sends humans to the pages.

## Metrics

All targets are Proposed targets. They are instrumented in the MVP and measured on synthetic usage until a real pilot exists.

North star: weekly cited decision answers, meaning answers or briefs that cite at least one human-verified decision and are not flagged wrong.

Guardrails:

| Guardrail | Proposed target |
|---|---|
| Permission leaks | 0 observed (finite matrix, not proof) |
| Revocation p95 | <= 60 s, source event received to unreachable on all paths |
| Wrong-answer flags | under 5% |
| Abstention | Cairn says "not found" whenever evidence is missing |

Activation: a team with 10 verified decisions and 3 distinct askers in one week.

Quality metrics, from the evaluation plan (brainstorm section 16):

| Metric | Definition | Proposed target | Corpus |
|---|---|---|---|
| Recall@5 | Gold evidence in the top 5 | >= 0.85 | C1 |
| Citation precision | Citations that support their claim | >= 0.90 | C1, C2 |
| As-of accuracy | Correct answer for the requested valid and recorded time | >= 0.85 | C1, C2 |
| Conflict surfacing | Known conflicts shown with both sides | >= 0.80 | C1 |
| Abstention | Correct "not found" on unanswerable questions | >= 0.80 | C1, C3 |
| Freshness p95 | Source event received to queryable | <= 60 s | Harness |
| Latency p95 | End to end, answer cache off | <= 5 s | C1 |
| Cost per query | Tokens and dollars per answer | Reported | All |

Median review-queue age is also tracked as a quality measure. Permissions, as-of logic and leaks use deterministic tests, never judges.

## MVP exit criteria

The MVP is done when all five are checked (brainstorm section 16). Each id is used by `docs/acceptance-tests.md`.

| Id | Criterion |
|---|---|
| EC1 | Canary matrix: 0 observed leaks across all personas, paths and states. |
| EC2 | Revocation p95 and freshness p95 <= 60 s, measured over at least 20 trials on the demo deployment. |
| EC3 | Recall@5 and as-of accuracy >= 0.85 on the C1 test split. |
| EC4 | A B0 to B3 table with confidence intervals published, failures included. |
| EC5 | Live demos of J1, J2, J3 and J5 with no manual database edits. |

Never cut when a phase slips: the permission matrix, the revocation test, as-of correctness and the eval table.

## Assumptions, risks and open questions

Assumptions:

- All data is synthetic or public (D20). Names are fictional and URLs use reserved `.example` domains.
- One tenant. Multi-tenant hosting is out of scope.
- The stack is PostgreSQL 18 with pgvector and native full-text search (Neon in production), DBOS for jobs, FastAPI, a React web app and a read-only MCP server, with embeddings, reranking and answers from Cloudflare Workers AI behind provider interfaces (D5, D6, D7, D10, D15, D16, D19). The demo runs on free tiers: Cloudflare Pages, one Render free web service in combined mode and Neon free (D18, [ADR-0013](adr/ADR-0013-postgres-pgvector-fts-neon-store.md), [ADR-0014](adr/ADR-0014-free-tier-deployment-cloudflare-render-neon.md), [ADR-0015](adr/ADR-0015-workers-ai-models-and-neuron-budget.md)). Phase 0 verifies the base of this stack; later phases build on it.
- The demo runs locally with a recorded video, or on a free-tier host if capacity allows (Verify).
- The Docker Compose core profile fits the laptop. Estimate: about 4 GB; the ceiling is 6 GB.

Top risks (full table in brainstorm section 19):

| Risk | Mitigation | Trigger |
|---|---|---|
| Extraction precision too low | Candidates only, review queue, ADR files as T1 anchors | S4 precision below 0.7 |
| Indexes ignored under row-level security | Spike S2, then SECURITY DEFINER retrieval functions | EXPLAIN shows sequential scans |
| Filtered vector recall collapses | Iterative scans, partitioning | Recall@10 ratio below 0.95 |
| GitHub permission events incomplete | Scheduled permission polling | S5 finds gaps |
| Revocation SLO missed | Priority queue, late recheck | p95 above 60 s |
| Scope creep | Cut ladder, non-goals | A phase slips by a day |
| Synthetic data too clean | Noise stage, real C2 corpus | Near-perfect scores |
| Hallucinated dates | Date verification against valid periods | As-of errors in evaluation |

Open questions, with decide-by dates (full list in brainstorm section 19):

| # | Question | Decide by |
|---|---|---|
| 1 | Final ACL-filter strategy for vectors | 2 Nov |
| 2 | RLS-first or SECURITY DEFINER retrieval functions | 30 Oct |
| 3 | Embedding model and dimension, and reranker model | 30 Oct |
| 4 | Answer model and judge model pair | 1 Nov |
| 5 | Decision granularity: one ADR or one claim per record | 3 Nov |
| 6 | Default as-of view in the UI | 11 Nov |
| 7 | GitHub org plan needed for private repos and teams | 29 Oct |
| 8 | Remote MCP for the demo or not | 11 Nov |
| 9 | Audit retention after a source deletion | 7 Nov |

## Traceability

Phases are from the brainstorm roadmap: Phase 1 evidence search, Phase 2 decision memory, Phase 3 live sync and permissions, Phase 4 experience and agents, Phase 5 evaluation, Phase 6 ship. Phase 0 (this phase) delivers the PRD, ADRs, threat model, scenario and skeleton that all of them build on. The acceptance test list in `docs/acceptance-tests.md` holds the named tests; it is written in task 16.5.

| Id | First delivered | Also touched | Measured or checked in |
|---|---|---|---|
| US1 | Phase 1 (cited answers) | Phase 2 (decision records), Phase 4 (answer workflow and UI) | Phase 5 (C1 citation precision) |
| US2 | Phase 2 (timeline) | Phase 4 (decision page) | Phase 6 demo |
| US3 | Phase 2 (as-of queries) | Phase 4 (as-of control) | Phase 5 (as-of accuracy) |
| US4 | Phase 4 (review queue) | Phase 2 (candidate states) | Phase 6 demo |
| US5 | Phase 3 (permission sync, revocation) | Phase 4 (MCP path) | Phase 3 timing harness, Phase 6 |
| US6 | Phase 1 (canary matrix on search and answer) | Phases 2 to 4 (derived content, MCP) | Phase 6 full matrix |
| US7 | Phase 4 (MCP, pre_change_brief) | Phase 3 (ACL layers) | Phase 6 demo |
| US8 | Phase 3 (sync state) | Phase 4 (sources screen) | Phase 6 demo |
| US9 | Phase 1 (not-found state) | Phase 4 (verification, abstention) | Phase 5 (abstention) |
| EC1 | Phase 1 (search and answer paths) | Phases 2 to 4 (more paths) | Phase 6 |
| EC2 | Phase 3 (timing harness) | Phase 1 (freshness path) | Phase 6 (20 trials on the demo deployment) |
| EC3 | Phase 1 (Recall@5 baseline) | Phase 2 (as-of accuracy on the dev split) | Phase 5 (C1 test split) |
| EC4 | Phase 5 (B0 to B3 table) | Phase 1 (B0 and B1 harness) | Phase 5 |
| EC5 | Phase 4 (J5 agent brief) | Phases 2 and 3 (J1, J2, J3) | Phase 6 |

Phase 0 has no user-facing story. Its own exit gate is in the development plan: CI green, ADRs merged and the scenario validating.
