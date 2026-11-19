---
status: proposed
date: 2026-11-04
decision-makers: Pankaj Baid
---

# ADR-0010: Re-read GitHub's whole access state on any permission event, and poll on start and on a schedule while awake

Spike S5 (which events fire, and which API calls reveal state the events miss) needs a real demo organisation and is user-run, so this ADR stays `proposed` and the coverage table below is filled from that run.

## Context and Problem Statement

Access in the demo organisation comes from organisation membership, team membership, repository access for teams, direct collaborators and repository visibility. Webhooks announce most of these changes, but not necessarily all, and an event can be lost. Which events does Cairn treat as permission changes, and how does it learn about the rest?

## Considered Options

* Apply each event incrementally (add or remove one membership).
* On any permission-class event, re-read the whole organisation's access state and make Cairn equal to it.
* Poll only, with no events.

## Decision Outcome

Chosen option: "re-read the whole access state on any permission-class event, and also on every start and on a schedule while awake", because a full re-read is idempotent, handles a lost or reordered event, covers changes that fire no event, and a small organisation costs a few dozen API calls. Conditional requests (`If-None-Match`) keep an unchanged pass nearly free.

Permission-class events: `membership`, `team`, `team_add`, `organization`, `member`, `public`, `repository`, `installation`, `installation_repositories`.

The pass reads organisation members, teams and their members, repositories, each repository's teams and direct collaborators, then writes principals, time-aware memberships (source `github`) and re-ACLs every GitHub item. A private repository left with no readers fails closed to a group with no members. A repository that disappeared has its items purged.

Polling interval: 5 seconds for the inbox, 15 minutes for the scheduled permission re-read and reconciliation, while the service is awake. There is no always-on poller (ADR-0014).

## Consequences

* Good: correctness does not depend on any single event arriving.
* Bad: the first pass after a long sleep reads everything; for the demo organisation this is a few dozen calls.
* Open (S5): which changes fire no event at all (for example some inherited team permissions) and therefore rely on the start and scheduled passes; the measured coverage and the rate-limit cost go in the table below once the spike has run.

| Change on GitHub | Event seen? | API call that shows it | Notes |
|---|---|---|---|
| (to be filled by the user-run S5 spike) | | | |

### Confirmation

`test_permission_sync_is_idempotent_and_follows_changes` and the revocation, visibility and new-enemy tests in `packages/connectors/tests/integration/test_github_sync.py` run the full pass against an in-memory organisation. The S5 coverage table is confirmed against a real organisation by the steps in docs/sync.md.
