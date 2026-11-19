# Task 12.1 notes: scenario models, schema, loader, canary generator

Date: 27 Oct 2026. Dependencies and uv.lock unchanged.

## Files (all under `packages/eval/`)

| File | State |
|---|---|
| `src/cairn_eval/scenario/model.py` | Pydantic v2 models of scenario YAML v1, vocabularies as `Literal` types and runtime tuples, id patterns |
| `src/cairn_eval/scenario/loader.py` | `ScenarioLoader` (timestamp-free, YAML 1.2 booleans, duplicate keys rejected), `load_scenario`, `load_scenario_text`, `ScenarioLoadError` |
| `src/cairn_eval/scenario/canary.py` | `canary_token`, `CANARY_FAMILIES`, `CANARY_RE`, `PLACEHOLDER_RE` |
| `src/cairn_eval/scenario/schema.py` | `generate_schema`, `render_schema`, `schema_path`, `main` (`python -m cairn_eval.scenario.schema [--check]`) |
| `src/cairn_eval/scenario/scenario-v1.schema.json` | Generated, committed. Never edit by hand |
| `src/cairn_eval/scenario/__init__.py` | Re-exports `Scenario`, loader functions, `canary_token`, `CANARY_RE`, `CANARY_FAMILIES`, `PLACEHOLDER_RE`. `schema` is not re-exported, so `python -m` runs without a runpy warning |
| `tests/unit/test_scenario_model.py`, `tests/unit/test_canary.py` | Loader, model, schema and canary tests |

## Models

`Scenario` (root) with `Meta`, `Person`, `Team`, `Membership`, `PrincipalRef`, `AccessGrant`, `Repo`, `Channel`, `Meeting`, `Location`, `Message`, `PullRequest`, `Issue`, `Source`, `DecisionOption`, `Anchors`, `TimelineEvent`, `Claim`, `ConflictSide`, `Conflict`, `Decision`, `Twist`, `Canary`, `AsOf`, `Expected`, `Question`. Base class `Model` is frozen, `extra="forbid"`, `validate_by_name=True`.

Helpers for 12.3: `Source.scope` (`repo:<id>`, `channel:<id>` or `meeting:<source id>`, from the location), `Source.available_or_date`, `Scenario.source(id)` (raises `KeyError`).

## Canary format

`KP-<FAMILY>-<16 base32 chars>`, `CANARY_RE = ^KP-(HR|PARTNER|SECURITY|INCIDENT)-[A-Z2-7]{16}$`.
`canary_token(seed, canary_id, family)` = `"KP-" + family.upper() + "-" + b32encode(sha256("cairn-canary\x1f" + seed + "\x1f" + canary_id))[:16]`. `family` must be lowercase and in `CANARY_FAMILIES`, else `ValueError`. The family is not part of the digest (as in the design). Scenario text refers to tokens as `{{canary:ID}}` (`PLACEHOLDER_RE`, group 1 is the id).

## Deviations and additions

| Design | Done here | Why |
|---|---|---|
| "timestamp-free SafeLoader" | Also YAML 1.2 booleans (only true/false) and duplicate-key errors | The design's timeline events use an unquoted `on:` key. PyYAML (YAML 1.1) turns `on` into `True`, so the key would never match. Duplicate keys would silently drop data |
| Data Models list a subset of models | Added `Issue`, `Conflict`, `ConflictSide`, `PrincipalRef`, `AccessGrant`, `Meeting`, `Location`, `Anchors`, `DecisionOption`, `AsOf`, `Expected`, `Meta` and others | Needed by the §11 excerpt and rules CLM-1 (conflicts), SRC-1 (issues, locations), TW-2 |
| Reference fields | Plain non-empty string (`Ref`), no id pattern | A dangling reference is then REF-1 with a pointer, not an SCH pattern error. Defining ids and `ScopeRef`, `Asker`, `PrRef` keep their patterns |
| Schema location | `packages/eval/src/cairn_eval/scenario/scenario-v1.schema.json` | Matches the `yaml-language-server` comment in design §11 |
| Date handling | `WithJsonSchema` pins `type: string`, `format`, and an exact `pattern` on `Date` and `Timestamp` | `date.fromisoformat` accepts `20260307`; the pattern keeps the file format strict. `date-time` is not format-checked by jsonschema here (task 1.1 note), so the pattern and the model do it |

## Notes for later tasks

- 12.2: `canary_token` is pure. Property 9 can check determinism, the regex, the independent derivation (see `test_canary.py::_expected`) and pairwise distinctness over distinct ids.
- 12.3:
  - Pipeline: `load_scenario` (raw data) -> JSON Schema (`Draft202012Validator(generate_schema(), format_checker=FormatChecker())`) -> `Scenario.model_validate`. Report model-only failures (the "exactly one of team or person" check in `PrincipalRef`, the phrase/kind check in `Canary`) as SCH.
  - `ScenarioLoadError` carries `source`, `line`, `column` (1-based) for exit status 2.
  - `TRANSITIONS`, `valid_time`, `recorded_time` and `state_as_of` are not in the model module; add them in the validator package. The event and state vocabularies are importable: `LIFECYCLE_EVENTS`, `ANNOTATION_EVENTS`, `LIFECYCLE_STATES`, `TWIST_KINDS`.
  - Canary id prefix vs family (`CAN-SEC-` with `security`) is not enforced by the model; CAN-3 or a new check may want it.
  - `Twist.decisions`, `Twist.people` and `Twist.sources` are the fields TW-2 inspects. `Decision.conflicts` holds the CLM-1 conflicts, with `sides[].value` strings.
  - `Expected.lifecycle` maps decision id to a `LifecycleState` (includes `absent`).
  - Mutations that rename a reference keep passing the schema, because references have no pattern. Mutations of defining ids (for example an `ADR-7` id) are SCH errors.
- 12.6: write `scenario.yaml` with `# yaml-language-server: $schema=../../packages/eval/src/cairn_eval/scenario/scenario-v1.schema.json`. Required top-level keys: all of `schema_version`, `meta`, `people`, `teams`, `memberships`, `repos`, `channels`, `sources`, `decisions`, `twists`, `canaries`, `questions` (use `[]` for empty ones). Timestamps need a zone (`2026-03-12T15:04:00Z`). Quote `value:` strings in conflicts if they look like dates (the loader keeps them strings either way). Unknown keys are errors. A PR source uses `pr:`, an issue source uses `issue: {number: 57}`. `meeting_note` sources use `location: {meeting: {attendees: [...], share_with: [...]}}`.
- After any model change: `uv run python -m cairn_eval.scenario.schema` and commit the file.

## Verification (27 Oct 2026)

| Command | Result |
|---|---|
| `uv run pytest packages/eval/tests/unit/test_scenario_model.py packages/eval/tests/unit/test_canary.py` | 64 passed |
| `make types` | strict: no issues in 6 files; base: no issues in 26 files |
| `ruff check packages/eval`, `ruff format --check packages/eval` | clean |
| `python -m cairn_eval.scenario.schema --check` | exit 0 |

The shell tool dropped many calls during this task (empty output, exit -1) and recovered after retries; nothing was run partially.
