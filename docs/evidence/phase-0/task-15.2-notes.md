# Task 15.2 notes: property test for the docs checker

Date: 27 Oct 2026. Property 18 (Requirements 18.3, 19.4). Only `tests/scripts/test_check_docs_properties.py` was added; `scripts/check_docs.py`, dependencies and `uv.lock` are untouched.

## Tests (8, Hypothesis, profile max_examples unchanged)

| Test | Generator | Asserts |
|---|---|---|
| ADR | number 1 to 5, random subsets of front matter keys, sections and (ADR-2, ADR-4) keyword phrases | one `ADR-KEY`, `ADR-SECTION`, `ADR-KEYWORD` per removed element, message names it, nothing else |
| Threat model | random subset of TM-01 to TM-14 removed | one `TM-ID-MISSING` per removed id |
| Acceptance | random subset of the 14 AT ids removed | one `AT-ID-MISSING` per removed id |
| PRD | subset of the 9 headings and subset of the 14 US/EC ids | one `PRD-HEADING` / `PRD-ID` each |
| CONTRIBUTING | subset of the 7 headings | one `CONTRIB-SECTION` each |
| Complete documents | none | no findings |
| Links | lines of 1 to 3 items: resolving, image, external, anchor, mailto, `<angle>`, code span (silent) or broken (inline, image, reference-style, optional fragment, query, title), plus fenced blocks and prose | `LINK-BROKEN` exactly at the right (line, target) pairs; `run_group` equals the sorted findings; `run` is the deduplicated list |
| Whole run | all of the above in one temporary repo | `run` is sorted, deterministic, duplicate-free, independent of group order or repeats, and the count per code matches the defects |

Decoys in every example: prose mentions of removed ids, near-miss ids (`TM-99`, `AT-US10`, `US70`), table rows where an id is only part of a cell, and fenced copies of removed headings, ids and tables. None of them may satisfy or break a rule.

Filesystem examples build a fresh `tempfile.TemporaryDirectory` in the test body and use `deadline=None`.

## Bug check

No bug found in `scripts/check_docs.py`. One generator finding: two identical links on one line give two equal `Finding`s from `check_links` and `run_group`; only `run` removes duplicates (its documented contract). The test asserts that split instead of changing the checker.

## Verification

```text
$ uv run pytest tests/scripts/test_check_docs_properties.py tests/scripts/test_check_docs.py
73 passed
$ HYPOTHESIS_PROFILE=ci uv run pytest tests/scripts/test_check_docs_properties.py
8 passed
$ make fmt-check lint types
51 files already formatted / All checks passed! / mypy strict and base: no issues
```
