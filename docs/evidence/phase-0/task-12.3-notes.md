# Task 12.3 notes: validator rules, as-of oracle, `cairn-scenario` CLI

Date: 27 Oct 2026. Dependencies and uv.lock unchanged (the `cairn-scenario` script entry needs only `uv sync --locked --all-packages`).

## Files

| File | State |
|---|---|
| `packages/eval/src/cairn_eval/scenario/violation.py` | `Violation` (frozen, ordered: rule, path, message), `pointer(*parts)` (RFC 6901, escapes `~` and `/`), `finalize` (sorted, duplicate-free) |
| `.../scenario/oracle.py` | `TRANSITIONS`, `ANNOTATIONS`, `is_lifecycle`, `state_after`, `valid_time`, `recorded_time`, `state_as_of` |
| `.../scenario/access.py` | `PERSONA_IDS`, `personas`, `team_members`, `principal_members`, `scope_exists`, `is_restricted_scope`, `readers` |
| `.../scenario/rules.py` | One pure function per rule, `SEMANTIC_RULES` (19 functions), `check_semantics`, `lifecycle_violations` |
| `.../scenario/validator.py` | `validate`, `validate_file`, `check_schema`, `build_scenario`, `summarize` |
| `.../scenario/cli.py` | `cairn-scenario validate <file> [--format text\|json]`, `main(argv) -> int` |
| `.../scenario/__init__.py` | Re-exports `validate`, `validate_file`, `Violation`, `TRANSITIONS`, `valid_time`, `recorded_time`, `state_as_of` (not `schema`, not `cli`, as before) |
| `packages/eval/pyproject.toml` | `[project.scripts] cairn-scenario = "cairn_eval.scenario.cli:main"` |
| `mk/scenario.mk` | `scenario-validate` (`SCENARIO_FILE` overridable); fails with exit 2 until 12.6 writes the file |
| `packages/eval/tests/unit/data/mini_scenario.yaml` | A small fictional scenario that passes every rule; the base for all rule tests (reusable by 12.4, 12.8, 12.9) |
| `packages/eval/tests/unit/test_validator_rules.py`, `test_oracle.py` | 121 tests |

## Pipeline

`validate(raw)` -> JSON Schema (`SCH` per error, best nested error for `anyOf`) -> stop if any -> `Scenario.model_validate` (`SCH` for model-only failures, stop if any) -> the 19 semantic rule functions -> `finalize` (sorted by rule, then pointer, then message; no duplicates). Pointers are RFC 6901; the document root is `""` (shown as `(root)` in text output).

CLI exit codes: 0 valid, 1 violations, 2 unreadable file or YAML error (or bad command line, from argparse). Text: one `RULE  /pointer  message` line per violation plus a count line. JSON: `{file, ok, violations[], summary?}`; for exit 2 `{file, ok: false, error: {message, line, column}}` on stdout, so the output stays parsable. Text mode prints exit-2 errors to stderr as `error: <file>:<line>:<col>: <message>`.

## Rules (all implemented)

| Rule | Function | Reported at |
|---|---|---|
| SCH | `check_schema`, `build_scenario` | schema error path / model `loc` |
| ID-1 | `check_ids` | `/<collection>/i/id`, `/decisions/i/claims/j/id`, `/sources/i/pr/number` |
| REF-1 | `check_references` | the referencing field |
| MEM-1 | `check_memberships` | `/memberships/i` |
| SRC-1 | `check_sources` | `/sources/i[/field]` |
| TL-1 | `check_timeline_order` | `/decisions/i/timeline/j` |
| TL-2 | `check_lifecycle` (`lifecycle_violations` per decision) | `/decisions/i/timeline/j`, or `.../timeline` when empty |
| TL-3 | `check_event_sources` | `/decisions/i/timeline/j` |
| TL-4 | `check_valid_time` | `/decisions/i/timeline/j` |
| TL-5 | `check_supersession` | `.../supersedes/j`, `.../superseded_by/j`, `.../timeline[/j]` |
| CLM-1 | `check_claims` | `/decisions/i/claims/j[/support_sets/k]`, `/decisions/i/conflicts/j` |
| TW-1 | `check_twist_questions` | `/twists/i` |
| TW-2 | `check_twist_structure` | `/twists/i` |
| CAN-1 | `check_canary_placed` | `/canaries/i` |
| CAN-2 | `check_canary_scopes` | the text field (`/sources/i/body`, `/questions/i/text`, ...) |
| CAN-3 | `check_canary_values` | `/canaries/i[/phrase]`, or the text field holding a literal token |
| CAN-4 | `check_canary_scope_exposure` | `/canaries/i/allowed_scopes/j` |
| Q-1 | `check_questions` | `/questions/i/asker`, `/questions/i/as_of/valid\|recorded` |
| Q-2 | `check_question_secrecy` | `/questions/i/expected/must_not_contain/j`, or `/canaries/i` for an uncovered family |
| Q-3 | `check_question_expectations` | `/questions/i/expected/lifecycle/<id>`, `.../must_not_cite/j` |

## Oracle API

```python
TRANSITIONS: Mapping[str | None, frozenset[str]]       # design §11 table; no "reopened" key
valid_time(e) -> date                                  # e.effective or e.on
recorded_time(e, s) -> date                            # max(e.on, source.available_or_date); KeyError if source missing
state_as_of(d, valid, recorded, s) -> LifecycleState   # "absent" if nothing qualifies; KeyError if a lifecycle event's source is missing
```

`state_as_of`: lifecycle events only; keep those with valid time <= `valid` and recorded time <= `recorded`; latest by (valid time, list index) wins; `reopened` maps to `proposed`. Requirements 14.4 to 14.6 are unit tests (ADR-014/ADR-004 backdating at the reference date and at recorded 2026-06-08; ADR-007 on 2026-03-10).

## Decisions where the design left room (check these when writing the real scenario)

- **Reference-date bound on sources (SRC-1):** also `date <= reference_date` and `last_edited >= date`; `pr.merged_at` date `>= date`; `pull_request` needs a `pr` block, `issue` an `issue` block (and not on other kinds); a merged PR needs `merged_at`, a non-merged one must not have it.
- **Location vs kind (SRC-1):** `chat_thread` has exactly a `channel`; `meeting_note` exactly a `meeting`; every other kind exactly a `repo`. `path` needs a `repo`.
- **REF-1 on `anchors.prs`:** `repo#N` must resolve to a `pull_request` source in that repo with that number. So every PR an ADR anchors needs its own source.
- **TL-3:** an event dated before its source's `date` is a violation (`on >= source.date`). An `implemented` event backed by a `pull_request` source must be on the date of `merged_at` as written in the file (its own UTC offset) and the PR must be merged.
- **TL-4 valid-time order:** consecutive lifecycle events must have non-decreasing valid time in list order. A broken `on` order therefore often reports TL-4 as well.
- **TL-5:** besides symmetry and the date equality it requires a `superseded` event on the old decision (and no `superseded` event without a superseding decision), `by` on that event (if set) to name a decision that lists the old one in `supersedes`, and an `accepted` event on the newer decision (the last one is used).
- **TW-2 (five kinds, "at least one qualifying entity" semantics for all of them):**
  - `deleted_source`: a named source has `deleted`.
  - `stale_doc`: a named doc, wiki or adr_file has `last_edited`, and another named source dated after it is cited at tier T1 by some claim support set or conflict side. **Sources have no tier of their own, so a T1 claim or conflict side must cite the contradicting source.**
  - `team_move`: a named person has a membership that ends the day a membership in another team starts (`a.to == b.from`).
  - `injected_instruction`: a named source is an `issue` in a public repo.
  - `restricted_decision`: at least one named decision, and every source cited by its timeline or claim support sets has a restricted scope (private repo, private channel, meeting).
  - The other eleven kinds have no extra structure checks in v1 (`_TW2_CHECKS` is the extension point).
- **CAN-3 additions to the design:** canary id prefix must match the family (`HR`->hr, `PARTNER`->partner, `SEC`->security, `INC`->incident), and any `KP-<FAMILY>-<16 base32>` literal anywhere in the file is a violation (this covers the generated tokens and hand-typed look-alikes).
- **CAN-2:** placeholders and phrase canaries count as placements only in source `title`, `body` and `messages[].text`. Outside `sources` and `canaries`, any string holding a placeholder or phrase is a violation, so a gold question cannot quote a canary.
- **CAN-4 personas:** `PERSONA_IDS = (asha, ben, dev, priya, carla)`. If a scenario defines none of them, every person counts (so tiny test scenarios work). Readers: public repo = everyone; public channel = every non-external person; private repo or channel and meetings as in `access.py`; team members are time-aware, half-open.
- **Q-1:** as-of dates must not be after the reference date (no lower bound).
- **Q-2:** evaluated on the reference date (not the question's as-of). "Each canary family has a question" applies to families that the scenario defines, and only counts a question whose asker cannot read the canary.
- **Q-3 `must_not_cite`:** violation when `source.available_or_date <= as_of.recorded`. With no `as_of`, recorded is the reference date, so `must_not_cite` without `as_of` always fails.

## Notes for later tasks

- **12.6 (scenario.yaml):**
  - Design §11 excerpt has `pr-payment-service-123` with `date: 2026-03-10` and no `available`, but Q042 (recorded 2026-03-10) lists it in `must_not_cite`. That fails Q-3. Set `available: 2026-03-12` (the merge date) on the PR source.
  - TL-3 requires ADR-007's `implemented` event on the PR's `merged_at` date (12 Mar).
  - ADR-004 needs `superseded_by: [ADR-014]` and ADR-014 `supersedes: [ADR-004]`; ADR-004's `superseded` event (`on` 2026-06-10, `effective` 2026-06-01, `by: ADR-014`) must have the same valid time as ADR-014's accepted event. The twist `TW-ADR014-BACKDATED` must list both decisions, or TL-4 fires.
  - `TW-WIKI-STALE`: `sources` = the wiki page (with `last_edited`) plus a later source that a T1 claim or conflict side cites.
  - `TW-BEN-TEAM-MOVE`: Ben's mobile membership `to: 2026-09-01` and payments `from: 2026-09-01`.
  - Every canary needs a question whose asker cannot read it (Q-2), a placement (CAN-1) and an allowed scope that leaves out a persona on 2026-10-27 (CAN-4). Ben is in payments from 1 Sep, so he can read `incidents-payments`; use Priya or Carla as the incident-canary asker.
  - Ben's open security membership from 2026-10-20 (J3) makes him a reader of `repo:security-policies` on the reference date. A security-canary question must not be asked by Ben.
  - `mini_scenario.yaml` is a working example of every construct.
- **12.7 (broken fixtures):**
  - `inconsistent-date` (ADR-007 implemented moved to 2026-03-02) reports TL-1, TL-3 (before the PR date and not the merge date) and TL-4 (valid-time order differs). Use `allow_rules: [TL-1, TL-3, TL-4]`.
  - `twist-without-questions` reports only TW-1 if the new twist's kind has no TW-2 check (`partial_revert` has none).
  - `unplaced-canary` reports CAN-1 only if the new canary id prefix matches its family (`CAN-SEC-09` with `security`), or CAN-3 fires too.
  - Pass the patched dict to `validate(raw)`; it is pure and takes raw data.
- **12.4:** `lifecycle_violations(decision, "/decisions/0")` is the TL-2 core and takes a hand-built `Decision`. `TRANSITIONS` and `state_after` are the walk tables.
- **12.5:** `state_as_of` is the function under test; a naive replay needs `valid_time` and `recorded_time`. A `Scenario` is needed for source availability; `mini_scenario.yaml` plus Hypothesis-chosen `effective`/`available`/`on` values works.
- **12.8:** `RULE_CASES` in `test_validator_rules.py` is a ready catalog of single-defect mutations (operation tuples on the raw dict, one per rule). The `apply` helper there is simple enough to copy.
- **12.9:** `validate` is pure and the result is `sorted(set(...))`. Renaming an id consistently (string replace on the YAML text) keeps `mini_scenario.yaml` valid; there is a test for it.

## Deviations from the design

| Design | Done here | Why |
|---|---|---|
| Rule behavior in §12 for TW-2, SRC-1, TL-5, CAN-3, Q-1 to Q-3 is a one-line summary | The detailed readings listed above | The text under-specifies them; each reading is cheap to change in one function |
| CAN-3 lists format, uniqueness, phrase rules and "no generated token literal" | Adds the id-prefix vs family check and flags any token-shaped literal | The 12.1 notes asked for a prefix check; a look-alike literal is a hand-typed canary |
| `cairn-scenario` via click or argparse (not stated) | argparse | Exit codes stay under my control; usage errors exit 2 as well |
| `schema` module stays out of the package import (12.1 note) | `validator.py` imports `generate_schema` inside `_schema_validator`'s cached function | Keeps `python -m cairn_eval.scenario.schema` free of the runpy warning |

## Verification (27 Oct 2026)

| Command | Result |
|---|---|
| `uv run pytest packages/eval/tests/unit/test_validator_rules.py packages/eval/tests/unit/test_oracle.py` | 121 passed |
| `uv run pytest packages/eval` | 187 passed |
| `make types` | strict: no issues in 9 files; base: no issues in 37 files |
| `make fmt-check lint` | 46 files formatted; all checks passed |
| `python -W error -m cairn_eval.scenario.schema --check` | exit 0, no warning |
| `make scenario-validate` | exit 2 (`scenarios/kestrel_pay/scenario.yaml` missing, expected until 12.6) |
| `make scenario-validate SCENARIO_FILE=packages/eval/tests/unit/data/mini_scenario.yaml` | OK |

Another agent's files were not touched. One stray read-only `git status` was typed in a command by mistake (output discarded, nothing changed).
