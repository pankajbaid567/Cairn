# Task 3.3 notes: property test for migration planning

Date: 27 Oct 2026. Property 1 (Requirements 6.1, 6.3, 6.4, 6.5). No source file changed; no bug found in `migrate.py`.

## File

`packages/core/tests/unit/test_migrate_properties.py` (9 tests, no `max_examples` override, so dev runs 100 examples and `HYPOTHESIS_PROFILE=ci` runs 200).

## Generators

- `cases()`: n bundled files (0 to 10) built with `parse_migration`, so checksums are real sha256 values. Names come from `[a-z][a-z0-9_]{0,15}`, SQL from non-blank `st.text` (1 to 60 characters). A recorded prefix 1..k (k from 0 to n) is built from them. Both lists are shuffled with `st.permutations`.
- Defect cases reuse `cases()` with minimum n or k, then apply one defect.

## Tests

| Test | Checks |
|---|---|
| pending suffix | `plan` returns exactly k+1..n, ascending |
| order and purity | any permutation of either input gives the same answer; inputs are unchanged |
| idempotence | recording the plan and planning again gives `[]` |
| status | `pending` equals `plan`; `head` is n, `current` is k, `applied` is sorted |
| edited checksum | `ChecksumMismatch`, `.version` and message name the edited version |
| database ahead | `UnknownAppliedMigration` for a recorded version n+1..n+5 |
| bundled gap | `MigrationGap` with `source == "bundled"` and the surviving versions |
| bundled duplicate | `MigrationGap` with `source == "bundled"`, the duplicate version appears twice |
| recorded gap | `MigrationGap` with `source == "recorded"`, the hole is absent from `.versions` |

## Verification

```text
$ uv run pytest packages/core/tests/unit/test_migrate_properties.py packages/core/tests/unit/test_migrate.py
50 passed in 1.63s
$ HYPOTHESIS_PROFILE=ci uv run pytest packages/core/tests/unit/test_migrate_properties.py
9 passed in 2.85s
$ make types
mypy --strict packages/core: Success: no issues found in 13 source files
$ uv run ruff check packages/core && uv run ruff format --check packages/core
All checks passed! / 13 files already formatted
```

`make lint` currently fails with two RUF005 findings in `packages/eval/tests/unit/test_lifecycle_properties.py`, which another agent is writing. Nothing in `packages/core` is flagged.
