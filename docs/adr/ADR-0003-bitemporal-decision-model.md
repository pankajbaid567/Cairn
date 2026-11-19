---
status: accepted
date: 2026-10-27
decision-makers: Pankaj Baid
---

# ADR-0003: Model decision history on two time axes with tstzrange, GiST exclusion and PostgreSQL 18 temporal keys

Records decision D9 of the [brainstorm decision log](../../brainstorm.md#1-tldr-and-decision-log). The data model is described in [brainstorm section 10](../../brainstorm.md#10-data-model-and-temporal-semantics). The fuller write-up, `docs/temporal-model.md`, is plan item 2.22 and does not exist yet.

## Context and Problem Statement

Cairn must answer "what did we believe on 10 March" and "what held on 5 June, as known today" for the same decision, and it must never let an as-of answer cite a fact that was recorded after the requested time. Decisions are also backdated (a meeting on 10 June decides a vendor change effective 1 June), corrected (Cairn recorded something wrong), superseded and partially reverted.

The question: how does Cairn store decision history so that these questions have exact, testable answers, writes cannot leave two contradictory versions of one decision, and no extra service is needed?

## Decision Drivers

* Two independent axes: valid time (when it held in reality) and recorded time (when Cairn held that version), in Fowler's terms.
* Overlapping versions of one decision must be impossible, enforced by the database and not by application code.
* Backdated decisions, corrections, future-dated decisions and unknown start dates must be representable.
* No additional service on an 8 GB laptop.
* A reference implementation (an oracle) that gold labels and Phase 2 tests can both use.

## Considered Options

* Bitemporal rows in PostgreSQL with `tstzrange` columns for valid and recorded time and a GiST exclusion constraint, plus PostgreSQL 18 temporal keys on single-axis tables (chosen).
* Single-axis history only (valid time or a `created_at` column).
* Append-only event log with state derived at read time.
* XTDB, a bitemporal database.

## Decision Outcome

Chosen option: "Bitemporal rows in PostgreSQL with `tstzrange` and a GiST exclusion constraint, plus PostgreSQL 18 temporal keys on single-axis tables", because it expresses both axes in plain SQL, lets the database reject overlapping versions, and needs no service beyond the one Cairn already runs.

### The model

* Every row in `decision_versions` carries `valid_period` (true in reality) and `recorded_period` (held by Cairn), both `tstzrange`, half-open (lower bound included, upper bound excluded, open upper bound means "still").
* A GiST exclusion constraint on `(decision_id WITH =, valid_period WITH &&, recorded_period WITH &&)` rejects any write that would leave two versions of one decision overlapping on both axes. It needs the `btree_gist` extension, which the bootstrap installs. Adjacent ranges are allowed.
* Single-axis tables, such as principal memberships, use PostgreSQL 18 temporal keys, for example `PRIMARY KEY (principal_id, group_id, valid_period WITHOUT OVERLAPS)`.
* PERIOD foreign keys in PostgreSQL 18 do not support CASCADE, SET NULL, SET DEFAULT or RESTRICT. Deletions of temporal rows therefore run through workflows (purge by lineage), not through cascades.
* Times are stored in UTC and displayed in the viewer's zone. The source zone is kept for meeting notes. Relative dates ("next Thursday") are resolved against the message timestamp, and both the resolved date and the original text are kept.

### The five query types

| Question type | Valid time | Recorded time |
|---|---|---|
| True now | now | now |
| True on a date, as known today | date | now |
| Believed on a date, about that date | date | the same date |
| Believed then, about an earlier date | earlier date | then |
| Diff of understanding between two times | any | two times |

The UI states which question it answered.

### Corrections, backdating and the change taxonomy

* Backdating. When a decision is recorded later than it took effect, one transaction closes the recorded period of the old version, re-inserts the old version with its valid period ended at the effective date, and inserts the new decision with its valid period starting at the effective date. Worked example (Illustrative, Kestrel Pay): ADR-004 chose vendor X in Feb 2025. On 10 Jun 2026 a meeting decides vendor Y, effective 1 Jun, and Cairn imports the notes on 11 Jun. "Which vendor on 5 Jun, as known today" returns Y. "On 8 Jun, which vendor did we think we had on 5 Jun" returns X, because nothing about Y was recorded before 11 Jun.
* Correction. When Cairn recorded something wrong, close the wrong version's recorded period. Valid time is unchanged. A correction never rewrites history silently.
* Supersession. Close the old decision's valid period and add a `supersedes` link. Reversion sets the lifecycle to reverted and links the reverting change. Contradictions keep both sides and open a review item (D4). Drift and staleness are flags, not state changes. Evidence deletion purges chunks and embeddings, and a claim with no remaining support set becomes unsupported.
* Edge cases. Unknown start uses the earliest evidence date and is flagged approximate. A future-dated start is reported as "scheduled".

### The as-of oracle

`state_as_of` in `cairn_eval.scenario` is the reference function for decision lifecycle state (plan item 0.12, requirement 14):

* The state of a decision at a valid date and a recorded date is the latest lifecycle event by valid date among events whose recorded date is on or before the requested recorded date, or "absent" if there is none.
* An event's valid date is its effective date when present, and its event date otherwise.
* An event's recorded date is the later of its event date and its source's availability date.

The Kestrel Pay gold labels are checked against it (validator rule Q-3), and Phase 2 tests the database against the same cases:

* Valid 2026-06-05 at the scenario reference date: ADR-014 accepted, ADR-004 superseded.
* Valid 2026-06-05 at recorded 2026-06-08: ADR-014 proposed, ADR-004 implemented.
* ADR-007 at valid and recorded 2026-03-10: accepted, not yet implemented (the Q42 case).

The oracle works on dates. The database works on timestamps with time zones. Phase 2 must map one onto the other explicitly (day boundaries in UTC) and test that mapping.

Revisit if (from the decision log): corrections and backdating make the write path error-prone. Then evaluate XTDB.

### Consequences

* Good, because both axes and all five question types are plain SQL over one table.
* Good, because the database refuses overlapping versions, so a bug in a workflow cannot create two contradictory current versions.
* Good, because the oracle lets scenario labels and database behaviour be checked against one definition.
* Good, because an as-of answer can exclude any evidence recorded after the requested time, which stops answers from citing the future.
* Bad, because every write to a decision is a multi-row transaction (close, re-insert, insert), which is easier to get wrong than an in-place update. Property tests in Phase 2 are the guard.
* Bad, because GiST exclusion constraints cost more to maintain than a unique index, and `btree_gist` must be present everywhere the schema runs.
* Bad, because PERIOD foreign keys lack cascade actions, so purges and deletions need explicit workflow code.
* Bad, because date-based gold labels and timestamp-based storage can disagree at day boundaries unless the mapping is tested.

### Confirmation

* Plan item 2.14: the EXCLUDE constraint rejects overlapping versions of one decision and accepts adjacent ranges.
* Plan item 2.15: flag-vendor backdating returns vendor Y for "as known today" on 5 Jun and vendor X for "believed on 8 Jun".
* Plan item 2.16: as-of answers never cite evidence recorded after the requested recorded time.
* Now, in Phase 0: the unit tests for `state_as_of` and the validator rules TL-1 to TL-5 and Q-3 (`make test-unit`, `make scenario-validate`) check the oracle on the Kestrel Pay scenario, including the three dated cases above.
* Now, in Phase 0: the `btree_gist` extension is required by the baseline migration check and by `/readyz`. An optional test of the PostgreSQL 18 temporal-key premise (`WITHOUT OVERLAPS` on a scratch table in a test database) can be added in Phase 0 or Phase 2. It is not part of the Phase 0 task list.

## Pros and Cons of the Options

### Bitemporal `tstzrange` rows with GiST exclusion and PG18 temporal keys

* Good, because it needs no extra service and uses features PostgreSQL 18 documents.
* Good, because constraints encode the invariant.
* Bad, because the write path is more complex than in-place updates.

### Single-axis history

* Good, because it is simple to write and query.
* Bad, because it cannot tell "what held on 5 June" from "what we believed on 8 June about 5 June", which is the core as-of case and the Q42 test.

### Append-only event log with state derived at read time

* Good, because history is never rewritten.
* Bad, because every as-of query replays events, and the invariants live in application code instead of constraints.
* Neutral, because lineage already keeps immutable source revisions, so the log idea is used for evidence, not for decision state.

### XTDB

* Good, because bitemporality is built in.
* Bad, because it is another service, another query language and another place where ACLs must be enforced, against principle 1 of brainstorm section 9.
* Neutral, because it is the named fallback if the write path proves error-prone.

## More Information

* [Brainstorm section 10, data model and temporal semantics](../../brainstorm.md#10-data-model-and-temporal-semantics)
* [Brainstorm section 6, the decision record and lifecycle](../../brainstorm.md#6-product-concept)
* [Development plan, Phase 2 tests 2.14 to 2.16](../../development_plan.md)
