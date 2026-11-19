---
status: proposed
date: 2026-11-04
decision-makers: Pankaj Baid
---

# ADR-0009: Store decisions as an event log with derived bitemporal rectangles, and show a claim only through a full support set

Refines [ADR-0003](ADR-0003-bitemporal-decision-model.md). Status is `proposed` until spike S4 (extraction quality, user-run with Workers AI) is measured; the storage and visibility parts below are implemented and tested.

## Context and Problem Statement

A decision has a lifecycle (proposed, accepted, implemented, superseded, reverted), learned from sources that arrive late and are sometimes corrected. Questions ask what held on a date and what Cairn knew on another date. Claims about a decision rest on evidence with different readers, so a summary must not reveal what a reader may not see.

## Decision Outcome

1. **Append-only `decision_events`, derived `decision_versions`.** Events record what a source said (valid time, recorded time, source, ACL copy). Versions are rectangles `valid_period x recorded_period` with `EXCLUDE USING gist (decision_id =, valid_period &&, recorded_period &&)`. Each write closes the recorded period of the one row containing the new valid time and inserts the old and new pieces. Supersession, reversion, backdating and unknown start dates are the same move; a correction closes a wrong row and re-inserts it. Nothing is rewritten.
2. **Day mapping.** Events are stored at 00:00 UTC; "as of D" uses the last instant of D. Recorded times of one decision must be non-decreasing (events replay in recorded order).
3. **Claim visibility (L5).** `claim_support` stores one row per item of each support set, with a copy of the item ACL. `cairn.claim_visible(claim)` is true when some support set has every item readable by the caller. Decisions are visible when some event ACL overlaps the caller. Both are `SECURITY DEFINER` functions used by RLS policies and by the query registry placeholders `{dvis:alias}` and `{cvis:alias}`.
4. **Summaries per principal set.** A summary is rebuilt from visible inputs only and cached by `(artifact, principal-set hash)`; lineage rows mark it stale when an input changes, is deleted or changes ACL.
5. **Candidates stay candidates.** LLM extraction writes `verification = 'candidate'`, with evidence spans checked against the revision text, cached by revision hash, model and prompt version.

## Consequences

* Good: as-of queries are one indexed range lookup; the test sweeps every date pair against the scenario oracle.
* Good: a hidden claim cannot leak through a summary, a count or a graph edge.
* Bad: out-of-order recorded writes are refused (`OutOfOrder`); a late source is handled by recording it with its own (later) recorded time, not by editing history.
* Bad: visibility functions run per row; acceptable at this scale, to be re-measured if decision counts grow.
