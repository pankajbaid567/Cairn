# Temporal model (Phase 2)

How Cairn answers "what held on date V, as Cairn knew it on date R". Decision: [ADR-0003](adr/ADR-0003-bitemporal-decision-model.md), storage and visibility: [ADR-0009](adr/ADR-0009-decision-store-and-claim-visibility.md). Code: `cairn_connectors/decisions/store.py` (writes), `cairn_retrieval/decisions/service.py` (reads), migration `0007_decisions.sql`.

## Two time axes

* **Valid time**: when the status held in reality. An event's valid time is its `effective` date if it has one, else its own date. A decision superseded on 10 June "effective 1 June" is superseded from 1 June.
* **Recorded time**: when Cairn could first know it: the later of the event date and the date its source became available.

## Tables

* `decision_events`: append-only log. Never changed except the ACL copy.
* `decision_versions`: derived rectangles `valid_period x recorded_period`, with a GiST exclusion constraint, so a decision never has two statuses at one (valid, recorded) point.

## Write rule

For a lifecycle event at valid time `v` recorded at `r`: find the open row (recorded period still open) whose valid period contains `v`; close its recorded period at `r`; re-insert its part before `v` with the old status; insert the new status from `v` to the old row's end. A write that would not change the status does nothing. Annotation events (`discussed`, `amended`, `partially_reverted`) are logged and do not change state. `reopened` leads back to `proposed`.

Special cases are the same move: backdating (a valid time before later rows), an unknown start date (a row opened at the earliest known time), supersession (`superseded` with `by_key` and a `supersedes` link) and reversion. A **correction** closes the wrong row's recorded period and re-inserts it with the right status, valid time unchanged; asking "as of" an earlier recorded time still returns the old belief.

Recorded times of one decision must not go backwards: `OutOfOrder` is raised, and equal times are moved forward by one microsecond. Replay events sorted by recorded time.

## Day mapping

Event dates are days. An event dated D is stored at 00:00 UTC of D. A query "as of date D" uses 23:59:59.999999 UTC of D, so it includes everything on that day, as the scenario oracle does. A query without a date means now.

## Reading

`state_as_of(key, valid, recorded)` returns the status, `"absent"` if nothing was recorded by then, or `None` if the decision is not visible to the caller. `timeline(key, recorded)` lists events recorded by then, in valid order. `expand(key, hops)` follows links 1 to 3 hops and re-checks visibility of both ends at each hop. Evidence for as-of answers uses the `*_asof` registered queries, which drop revisions updated after the recorded time.

## Visibility

A decision is visible if some event ACL overlaps the caller's groups. A claim is visible only if some whole support set is readable (`claim_visible`). Summaries are rebuilt from visible inputs and cached per principal set; lineage rows mark them stale when an input changes or is deleted. Evidence that was never imported (deleted or out-of-scope sources) leaves a claim unsupported, so nobody sees it.

## The five query types

1. **As known now, at a valid time**: `valid=D` (recorded defaults to now). "What was the status of ADR-012 on 10 March?"
2. **As known then, at a valid time**: `valid=D&recorded=R`. "On 8 June, what did we believe held on 5 June?" (the flag-vendor backdating case).
3. **Current**: neither parameter. The status now, as now known.
4. **Timeline**: every event the caller may see, in valid order, optionally only those recorded by `R`.
5. **Evidence as of**: `recorded=R` on search and answer. Only current chunks of revisions dated on or before `R` are retrieved, and nothing newer is ever cited. A document edited after `R` is left out rather than rolled back to an older revision (older revisions are not kept as chunks).

## Change taxonomy

| Change | What happens |
|---|---|
| New lifecycle event | The open row at its valid time is closed and re-split (write rule above). |
| Annotation (`discussed`, `amended`, `partially_reverted`) | Logged on the timeline, no state change. ADR-009's partial revert leaves it `implemented`. |
| Supersession | `superseded` event with `by_key`, plus a `supersedes` link from the new to the old decision. |
| Backdating | An event whose valid time precedes later rows: the earlier part of valid time is split out, old beliefs stay readable at earlier recorded times. |
| Correction | `correct_version`: the wrong row's recorded period is closed and it is re-inserted with the right status. |
| Source edited | Lineage marks dependent summaries stale; the next read rebuilds them. |
| Source ACL changed | ACL copies on events, versions and claim support are refreshed and dependent summaries marked stale. |
| Source deleted | Phase 3 purge: the claim loses that support set and is hidden if no full set remains. |

## Edge cases

* **Hidden events.** The read path derives status from the events the caller may see. A reader who cannot see the event that made a decision `implemented` sees it `accepted`, and never learns when a hidden event happened. The stored rectangles describe all events and are not exposed.
* **No visible event.** The decision does not exist for that reader (404, same as a missing key).
* **Ties.** Two events at one valid time: the later sequence number wins.
* **Equal recorded times.** Moved forward by one microsecond so rectangles never collide.
* **Unknown start date.** A decision known only from a later source starts at the earliest known valid time; nothing is guessed before it.
* **Membership changes.** Visibility follows memberships valid now: Ben joined payments on 1 September 2026 and so can read the private incident channel today but not before.

## Verification

`apps/api/tests/integration/test_decisions.py` replays the Kestrel Pay decisions and compares the database with `state_as_of` of the scenario oracle for every pair of dates around each event.
