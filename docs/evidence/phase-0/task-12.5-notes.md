# Task 12.5 notes: property test for the as-of oracle

Date: 27 Oct 2026. Property 13, requirements 14.1 to 14.3. One file added: `packages/eval/tests/unit/test_oracle_properties.py` (10 tests: 7 properties, 3 Kestrel Pay examples). No oracle, model or dependency changes. **No oracle bug found.**

## Naive replay

Written in the test on plain `EventSpec`/`SourceSpec` dataclasses, sharing no code with `oracle.py`: valid time = `effective or on`; recorded time = `max(on, source available or date)`; considered = lifecycle events with both times within the as-of dates; answer = last of `sorted((valid time, index))`, `reopened` mapped to `proposed`, `absent` when the set is empty. `build()` turns a spec into a real `Scenario` (empty people/teams/etc., N doc sources, one decision) for the oracle.

## Generators

- `worlds()`: 1 to 4 sources (date 0..40 days after 2026-01-01, available `None` or 0..30 days later) and 0 to 8 events in arbitrary order (not a legal walk). Event name from the 9 lifecycle names (double weight) and 3 annotations; `on` 0..40; `effective` `None` or `on` shifted by -20..+20 days; random source. Narrow ranges make ties and boundary hits common.
- `as_of_dates`: -5..75 days from the base, so dates fall before, inside and after the events.
- `invisible_events()`: a lifecycle event valid by `valid` (effective = valid) but recorded after `recorded`, either through a new source available late or through `on` after `recorded`; inserted at a random position.

## Properties

| Test | Checks |
|---|---|
| `test_oracle_agrees_with_the_naive_replay` | 14.1/14.2: oracle equals replay of the considered set |
| `test_raising_the_recorded_date_only_adds_events` | 14.3: considered set grows, both answers equal replays, equal sets give equal answers, non-absent stays non-absent, absent iff set empty |
| `test_raising_the_valid_date_only_adds_events` | the same on the valid axis |
| `test_annotation_events_never_change_the_answer` | removing annotations leaves the answer unchanged |
| `test_an_event_recorded_after_the_as_of_date_is_invisible` | appending/inserting such an event leaves the answer unchanged; it is considered once recorded reaches it |
| `test_before_every_event_the_state_is_absent` | valid and recorded before every event, or either one alone, gives `absent` |
| `test_after_every_event_the_state_is_that_of_the_last_event` | all lifecycle events considered; answer equals the last by (valid time, index) |
| three `test_kestrel_*` | requirements 14.4 to 14.6 on `scenarios/kestrel_pay/scenario.yaml` |

## Verification

| Command | Result |
|---|---|
| `uv run pytest packages/eval/tests/unit/test_oracle_properties.py packages/eval/tests/unit/test_oracle.py` | 29 passed |
| `HYPOTHESIS_PROFILE=ci uv run pytest .../test_oracle_properties.py` | 10 passed |
| `make fmt-check lint types` | 57 files formatted; all checks passed; mypy strict 15 files, base 42 files, no issues |
