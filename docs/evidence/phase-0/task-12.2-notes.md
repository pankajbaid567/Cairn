# Task 12.2 notes: property test for canary tokens

Date: 27 Oct 2026. Only `packages/eval/tests/unit/test_canary_properties.py` was added. `canary.py`, dependencies and uv.lock are unchanged.

## Property 9 coverage

Seeds, ids: non-empty Unicode text (lone surrogates excluded, Hypothesis default for `text()`), mixed with strings that contain the unit separator `\x1f`. Families: `CANARY_FAMILIES`.

| Test | Checks |
|---|---|
| `test_token_is_deterministic` | repeated calls agree |
| `test_token_is_well_formed` | `CANARY_RE`, prefix `KP-<FAMILY>-`, exactly 16 chars of `[A-Z2-7]` |
| `test_token_equals_the_documented_derivation` | independent SHA-256 and base32 derivation |
| `test_distinct_ids_under_one_seed_give_distinct_tokens` | sets of 2 to 30 ids, tokens pairwise distinct |
| `test_distinct_seeds_give_distinct_tokens_for_one_id` | sets of seeds, same id |
| `test_distinct_seed_and_id_pairs_give_distinct_tokens` | pairs with separator-free seeds |
| `test_field_boundary_shifts_do_not_collide` | `xfail(strict=True)`, see below |
| `test_field_boundary_collision_is_reproducible` | pins the concrete collision |
| `test_invalid_families_raise_value_error` | uppercase, capitalized, empty, arbitrary non-member text |

## Finding: field-boundary ambiguity in `canary_token` (not fixed)

The digest material is `"cairn-canary" 0x1F seed 0x1F id` with no escaping, so a separator inside either field is ambiguous:

    canary_token("a\x1fb", "c", f) == canary_token("a", "b\x1fc", f)

Impact today: none in practice. Canary ids match `^CAN-[A-Z]+-\d{2}$` and the scenario seed is `kestrel-pay-v1`, neither contains `\x1f`. Any unambiguous encoding (length prefixes, escaping) changes the material for every input, so every committed token would change, and design §11 documents this derivation. So the derivation was left alone.

The gap is recorded as a strict xfail: when it is fixed, the xfail turns into a failure and must be removed. Options for a decision: (a) accept and document, (b) make `canary_token` raise `ValueError` if seed or id contains `\x1f` (tokens for valid inputs unchanged), (c) change the encoding and update the design before any scenario is committed. Option (b) is the smallest change that keeps tokens stable.

Also noted: a lone surrogate in seed or id raises `UnicodeEncodeError` (not `ValueError` from the family check). Unreachable from YAML files that parse to valid text, so left as is.

## Verification

| Command | Result |
|---|---|
| `uv run pytest packages/eval/tests/unit/test_canary_properties.py packages/eval/tests/unit/test_canary.py` | 22 passed, 1 xfailed |
| `make fmt-check lint types` | all clean (ruff format, ruff check, mypy strict and base) |
