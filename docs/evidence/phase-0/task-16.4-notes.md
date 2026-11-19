# Task 16.4 notes: `docs/acceptance-tests.md`

Date: 27 Oct 2026. No code, dependencies or lock files touched. `brainstorm.md` and `development_plan.md` were read only.

## Files

| File | State |
|---|---|
| `docs/acceptance-tests.md` | New. Intro, status values, user story table, exit criteria table, maintenance rules |

## Structure

- Two tables with the same columns: id, statement, named tests, plan items, phase, status, Phase 0 groundwork. AT-US1 to AT-US9 are in section 3, AT-EC1 to AT-EC5 in section 4. No id appears as a whole cell anywhere else.
- Status values: planned, active in Phase 0, implemented, passing. All 14 rows are planned. "Passing" needs a run log, results file or recording in `docs/evidence/` with commit hash, date and command.
- Plan item numbers were copied from `development_plan.md` (1.11, 1.19, 1.22, 1.23, 1.27, 2.3, 2.9, 2.13 to 2.16, 2.18, 2.20, 2.21, 2.24, 3.6 to 3.8, 3.12 to 3.14, 3.18, 3.19, 3.22, 3.23, 3.27, 4.1 to 4.6, 4.8, 4.9, 4.13 to 4.15, 4.17, 4.23, 5.1, 5.4, 5.9, 5.10, 5.12, 5.14, 5.15, 6.6, 6.12 to 6.16).

## Test names

All names in the "Named tests" column are Proposed. Names shared with the threat model (`test_canary_matrix_search_paths`, `test_not_found_reply_identical_with_and_without_hidden`, `test_revocation_timing_p95`, `test_mcp_token_scope`) are reused, so one test serves both lists.

The "Phase 0 groundwork" column lists tests that exist in the repo today (checked by grep): `test_adr007_archaeology_question`, `test_adr014_backdating_at_the_reference_date` (`packages/eval/tests/unit/test_oracle.py`), `test_token_matches_the_documented_derivation_and_format` (`test_canary.py`) and `test_readers_of_each_scope_kind` (`test_validator_rules.py`). They test scenario tooling, not the product, so no row is marked "active in Phase 0".

## Judgement calls

- Phase column is the phase in which the full story can be tested end to end: AT-US1 is 4 (PR ingestion is Phase 3, the answer card Phase 4), AT-US6 and AT-US9 are 1 (first version), AT-US5 is 3 with its MCP leg in 4.15. Exit criteria rows are 6.
- Targets in the EC rows are marked Proposed targets, not results. The full canary matrix size (about 390) is marked Estimate.
- `docs/prd.md` is mentioned as plain text, because it is written in parallel. Links go only to `../brainstorm.md`, `../development_plan.md` and `threat-model.md`.
- `make docs-check` does not verify plan numbers or test name existence. The document says so and leaves that to review.

## Verification

```text
$ uv run python scripts/check_docs.py --only acceptance
docs-check: ok (acceptance)      exit 0
$ uv run python scripts/check_docs.py --only links
docs-check: ok (links)           exit 0
```

No em dashes and no exclamation marks in the document (grep count 0 for both).
