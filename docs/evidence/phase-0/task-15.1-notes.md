# Task 15.1 notes: `scripts/check_docs.py` and `make docs-check`

Date: 27 Oct 2026. Dependencies and uv.lock untouched. The real documents do not exist yet (tasks 16.1 to 16.5), so the checker is tested only on sample Markdown in `tmp_path`.

## Files

| File | State |
|---|---|
| `scripts/check_docs.py` | New. Standard library only. Pure check functions over strings; `run_group` is the only filesystem layer |
| `mk/docs.mk` | New. `docs-check` runs `$(UV_RUN) python scripts/check_docs.py`; `DOCS_ONLY="adr links"` picks groups |
| `tests/scripts/test_check_docs.py` | New. 65 tests (valid and invalid samples per group, file layer, command line) |

## Command line

```text
python scripts/check_docs.py [--only GROUP]... [--root DIR]
make docs-check [DOCS_ONLY="group group"]
```

Exit 0 no findings, 1 findings, 2 bad usage or `--root` not a directory. Output is one line per finding, sorted: `path[:line]: CODE message`. A missing file is a `DOC-MISSING` finding, an undecodable file `DOC-UNREADABLE`; neither crashes.

Fenced code blocks and inline code are ignored by every check, so an example inside a document can neither satisfy nor break a rule.

## Checks per group and error codes

| Group | Code | Reported when |
|---|---|---|
| all | `DOC-MISSING`, `DOC-UNREADABLE` | the file does not exist, or cannot be read as UTF-8 |
| `adr` | `DOC-MISSING` | `docs/adr/template.md`, `docs/adr/README.md`, or no file matching `docs/adr/ADR-000N-*.md` for N = 1 to 5 |
| `adr` | `ADR-DUPLICATE` | more than one file for one ADR number |
| `adr` | `ADR-FRONT-MATTER` | no `---` block on line 1, or it is not closed |
| `adr` | `ADR-KEY` | `status`, `date` or `decision-makers` missing or empty; `status` is not `accepted`; `date` is not a real `YYYY-MM-DD` date. The message names the key |
| `adr` | `ADR-SECTION` | a heading is missing: Context and Problem Statement, Considered Options, Decision Outcome, Consequences, Confirmation (any heading level, case-insensitive, trailing colon ignored). The message names the section |
| `adr` | `ADR-KEYWORD` | ADR-0002 lacks `cairn_dbos`, `same-transaction` (or `same transaction`) or `enqueue`; ADR-0004 lacks `cairn_owner`, `cairn_app` or `foreign key(s)` |
| `threat-model` | `TM-ID-MISSING` / `TM-ID-DUPLICATE` / `TM-ID-UNKNOWN` | TM-01 to TM-14 must each be a whole table cell in exactly one table row; ids outside that set (TM-15, TM-1) are unknown |
| `threat-model` | `TM-ROW-INCOMPLETE` | the row has fewer than 4 non-empty cells (threat, test id, planned test, phase), or no cell that is a phase (`0` to `6`, optionally `Phase N`) |
| `acceptance` | `AT-ID-MISSING` / `AT-ID-DUPLICATE` / `AT-ID-UNKNOWN` | AT-US1 to AT-US9 and AT-EC1 to AT-EC5, same table-cell rule; AT-US10 is unknown, not a match for AT-US1 |
| `acceptance` | `AT-TEST-ID` | the row has no token of the form `test_<name>` |
| `prd` | `PRD-HEADING` | no heading matches one of: Problem, Personas, Jobs, User stories, Scope, Non-goals, Journeys, Metrics, Exit criteria |
| `prd` | `PRD-ID` | US1 to US9 or EC1 to EC5 is not mentioned as a whole word |
| `contributing` | `CONTRIB-SECTION` | no heading matches: setup/quick start/getting started/prerequisites; definition of done; quality gates; AI-assisted workflow; ADR process; data rules; user-owned steps |
| `links` | `LINK-BROKEN` | a relative inline or reference-style link in `docs/**/*.md` or `CONTRIBUTING.md` does not resolve to a file (directories do not count) |
| `links` | `LINK-ABSOLUTE` | a link target starts with `/` |

Links ignored: `http(s):`, `mailto:` and any other scheme, `//host`, and same-page `#anchors`. Fragments and query strings are stripped before resolving; `%20` style escapes are decoded; `<...>` targets and titles are handled.

## Deviations from the design

- Added beyond design §16, all from the requirements: `docs/adr/template.md` and `docs/adr/README.md` must exist (Requirement 18.6); PRD must mention US1 to US9 and EC1 to EC5 (18.1); CONTRIBUTING must have the setup, quality gates, ADR process, data rules and user-owned steps sections as well as definition of done and AI workflow (19.3); the threat-model row must carry a planned test and a phase, not only the id (19.1); `ADR-DUPLICATE` and `*-ID-UNKNOWN` guard against stray files and ids.
- "TM ids exactly once" is checked on table rows, not on raw text, so prose such as "see TM-11" in the threat model is allowed. Same rule for AT ids.
- The ADR `status` must be exactly `accepted`, as the design says, so a draft ADR fails `make docs-check` until it is accepted.
- `DOCS_ONLY` in `mk/docs.mk` is an extra; `make docs-check` alone runs everything.
- The `links` group also scans `docs/evidence/**` and `docs/spikes/**`, since the requirement says "every relative link in docs".

## Verification

Resumed run: all four files were found complete and kept unchanged (no truncated functions, every group present, notes matched the code). Results:

```text
$ uv run pytest tests/scripts/test_check_docs.py
65 passed
$ make types
mypy --strict packages/core: no issues in 6 source files
mypy base: no issues in 26 source files
$ make fmt-check lint
32 files already formatted / All checks passed!
$ make docs-check DOCS_ONLY=links
docs-check: ok (links)
$ make docs-check          # real repo, docs not written yet
docs-check: 11 problem(s) ...  (all DOC-MISSING), exit 1
$ uv lock --check
Resolved 118 packages
```

`make docs-check` is expected to fail on the real repo until tasks 16.1 to 16.5 exist.

## Notes for tasks 16.1 to 16.5

- 16.1 `docs/prd.md`: headings (any level) containing: "problem", "personas", "jobs", "user stories", "scope" (for example "MVP scope"), "non-goals", "journeys", "metrics", "exit criteria". Mention US1 to US9 and EC1 to EC5 literally (not `US-1`).
- 16.2 and 16.3 `docs/adr/`: `template.md` and `README.md` plus `ADR-0001-<slug>.md` to `ADR-0005-<slug>.md`, one file per number. Each starts on line 1 with `---`, keys `status: accepted`, `date: 2026-10-NN`, `decision-makers: ...`, then `---`. Headings: `## Context and Problem Statement`, `## Considered Options`, `## Decision Outcome`, `## Consequences`, and `### Confirmation` (or `##`). Do not put the template's placeholder text in ADRs (the template itself is not checked for front matter).
  - ADR-0002 must contain `cairn_dbos`, the words "same-transaction" (hyphen or space) and "enqueue".
  - ADR-0004 must contain `cairn_owner`, `cairn_app` and "foreign key" or "foreign keys".
- 16.4 `docs/threat-model.md`: a table with one row per threat, for example `| Threat | Test id | Planned test | Phase |`. Put `TM-01` to `TM-14` as the whole content of one cell in exactly one row each. The planned test name and a phase cell (`1`, or `Phase 1`) are required. Do not define a TM id in a second table (a summary table that repeats ids would be a duplicate).
- 16.5 `docs/acceptance-tests.md`: one table row per `AT-US1` to `AT-US9` and `AT-EC1` to `AT-EC5` (id as a whole cell), each row with at least one `test_<name>` token (code ticks are fine). Do not repeat an id as a cell in another table.
- `CONTRIBUTING.md`: headings containing "quick start" (or setup, getting started, prerequisites), "definition of done", "quality gates", an AI heading with "workflow" or "assisted", "ADR process" (or "decision records"), "data rules", "user-owned steps". Every relative link in it and in `docs/` must point to an existing file, not a directory.
- Table cells are compared after removing asterisks and backticks, so `**TM-03**` and `` `TM-03` `` both count.
- Run one group while the others are missing: `make docs-check DOCS_ONLY=prd` or `uv run python scripts/check_docs.py --only adr`.
