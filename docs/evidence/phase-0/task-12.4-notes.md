# Task 12.4 notes: lifecycle state machine property test

Property 10 (requirement 13.10). New file: `packages/eval/tests/unit/test_lifecycle_properties.py` (8 tests). No source, dependency or `uv.lock` changes.

## Generators

- `lifecycle_walks(min_size, max_size, to_terminal)`: a legal walk of `TRANSITIONS` from the start; `to_terminal` runs until no move is left.
- `annotate`: inserts 0 to 2 annotation events after each lifecycle event, never before the first.
- `walks_with_one_illegal_step`: a legal walk (possibly empty or terminal), one lifecycle event the current state forbids, trailing annotations; returns the timeline and the illegal index.
- `arbitrary_sequences`: either uniform random events or an annotated legal walk with 1 to 3 replaced, inserted or deleted events.

## Tests

| Test | Checks |
|---|---|
| legal walks with annotations | no violation |
| one illegal step | exactly one TL-2 violation, at that index |
| lifecycle event after a terminal state | reported at that index |
| `reopened` when the state is not `reverted` | reported at that index |
| empty or annotation-only timeline | one violation at `/timeline` |
| annotations before `proposed` | one violation per leading annotation |
| arbitrary sequences vs a naive reference with its own transition table | same violating paths |
| determinism and no mutation | equal results on two calls; `model_dump` unchanged |

## Findings

- No bug in the rules. First run failed only because the "legal walk" strategy could produce an empty timeline, which TL-2 reports by design; the walk is now at least one event long.
- Behavior worth knowing: after an illegal lifecycle event, `lifecycle_violations` still moves to that event's state, so a later event can be legal relative to it (one defect gives one violation).
- When a timeline has no lifecycle event at all, only one violation is reported (at `/timeline`); annotation events are not reported individually.

## Verification

| Command | Result |
|---|---|
| `uv run pytest packages/eval/tests/unit/test_lifecycle_properties.py packages/eval/tests/unit/test_validator_rules.py` | 110 passed |
| `make fmt-check lint types` | formatted; all checks passed; mypy strict and base clean |
