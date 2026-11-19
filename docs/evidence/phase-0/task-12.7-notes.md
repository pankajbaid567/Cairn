# Task 12.7 notes: broken scenario fixtures

Date: 27 Oct 2026. No model, rule, validator or dependency changes. Added three fixtures under `scenarios/kestrel_pay/fixtures/broken/` and `packages/eval/tests/unit/test_broken_fixtures.py` (14 tests).

## Fixtures

Each file has `description`, `expect_rules`, `allow_rules` and an RFC 6902 `patch` that starts with `test` ops on the values it touches. Allowed rules for a fixture are `expect_rules` plus `allow_rules`.

| Fixture | Patch | Rules reported (re-derived) |
|---|---|---|
| `twist-without-questions` | tests `/decisions/3/id` is ADR-009, adds twist `TW-FIXTURE-ORPHAN` (partial_revert, ADR-009) | TW-1 |
| `inconsistent-date` | tests `/decisions/1/id`, `.../timeline/3/event` and `.../timeline/3/on` (2026-03-12), replaces the date with 2026-03-02 | TL-1, TL-3, TL-4, Q-3 |
| `unplaced-canary` | tests `/meta/id`, adds canary `CAN-SEC-09` (security, `repo:security-policies`) | CAN-1 |

Q-3 for `inconsistent-date` is a side effect: the Q042 and Q043 lifecycle labels no longer match the oracle once ADR-007's `implemented` event moves before its proposal. It is listed in both `expect_rules` and `allow_rules`, so the fixture pins it. This differs from the design excerpt (`expect_rules: [TL-1]`), which predates the 12.6 data.

## Tests

For each fixture: the file shape, expected rules reported and none outside the allowed set, the CLI exits 1 on the patched file (JSON report checked against the same sets), and a stale-guard check (the first `test` op fails with `JsonPatchTestFailed` after its target is changed). Also: the unpatched scenario has zero violations, and exactly three fixtures exist.

## Run a fixture by hand

The CLI validates scenario files, not patches. One-liner that patches, writes a temp file and validates it:

```bash
uv run python - <<'EOF'
import yaml, jsonpatch
from cairn_eval.scenario.loader import load_scenario
fixture = yaml.safe_load(open("scenarios/kestrel_pay/fixtures/broken/inconsistent-date.patch.yaml"))
patched = jsonpatch.apply_patch(load_scenario("scenarios/kestrel_pay/scenario.yaml"), fixture["patch"])
yaml.safe_dump(patched, open("/tmp/patched.yaml", "w"), sort_keys=False)
EOF
uv run cairn-scenario validate /tmp/patched.yaml    # exit 1, lists the violations
```

No helper script was added.

## Things to know

- Fixture values for dates must be quoted strings (`"2026-03-02"`), because `yaml.safe_load` would otherwise turn them into dates and the `test` op against the loader's string would fail.
- The patches index decisions by position (`/decisions/1` is ADR-007, `/decisions/3` is ADR-009); the leading `test` ops make a reorder fail loudly.
- `jsonpatch` and `jsonpointer` have no type stubs; the mypy override in the root `pyproject.toml` already covers them.

## Verification

| Command | Result |
|---|---|
| `uv run pytest packages/eval/tests/unit/test_broken_fixtures.py` | 14 passed |
| `make fmt-check lint types` | 58 files formatted; ruff passed; mypy strict 15 files and base 43 files, no issues |
