# Task 17.2 notes: `make phase0-exit`
Date: 30 Oct 2026 (host clock). No dependency, lockfile, compose file, `ci.yml` or obs test file touched. `brainstorm.md`, `development_plan.md` and `source-docs.sha256` were only read (the record already existed and matches). No git command run. `make phase0-exit` and `make gates` were **not run**, by instruction (another agent was running the obs profile on Docker; 8 GB host). The first real run belongs to the final checkpoint (task 18).
## Files
| File | State |
|---|---|
| `mk/exit.mk` | New: `phase0-exit`, `phase0-exit-preview` |
| `scripts/phase0_exit.py` | New. Standard library only. Pure functions for the hash check, the RAM evidence lookup and the checklist text |
| `tests/scripts/test_phase0_exit.py` | New: 17 tests (hash check cases, one Hypothesis property, RAM evidence, checklist content, preview wording, `make -n phase0-exit` order) |
| `CONTRIBUTING.md` | `phase0-exit` is no longer marked pending: intro, definition of done, quality gates paragraph and the target table row |
| `mk/gates.mk` | Comment only: "(task 17.2)" replaced by the fragment name |
## What `make phase0-exit` does
1. For `gates`, `up-db`, `s1-smoke`, `s2-smoke`: `make <step>`, stopping at the first failure. `gates` ends with `down`, and the smoke runs need the database, so `up-db` runs in between. Then `make down`.
2. `scripts/phase0_exit.py --gates "$(GATES_ALL)"`:
   - Compares sha256 of `brainstorm.md` and `development_plan.md` with `docs/evidence/phase-0/source-docs.sha256` (missing file, missing entry, missing document, changed document and malformed lines are each reported).
   - Requires the newest `core-ram-<date>.json` and `.md` from `make ram-report`, and `result: pass` in the JSON. `ram-report` itself is not run (about 2.5 minutes).
   - Prints EG1 to EG4 with the proving commands and what is still open, U1 to U7 as NOT automated, and the open items V9, V10, V11, S2 and S5.
   - Exit 1 if either check fails.
3. `make phase0-exit-preview` runs step 2 with `--preview`: same checks and printout, but the text says gates and smoke runs were not run and prints no result for them. It needs no Docker.
Choices to know:
- The hash and RAM checks run after the long steps, as the task lists them. They are cheap, so a failure only costs the wait for the gates; `make phase0-exit-preview` gives the answer early.
- V9 is printed as open because the SHAs are recorded (`verify/V9.md`) but the workflow has not run on a GitHub runner. S2 is open until plan 1.1 (real run, ADR-0006, 30 Oct).
## Verification
| Check | Result |
|---|---|
| `make -n phase0-exit` | order: gates (18 gates), up-db, s1-smoke, s2-smoke, down, `phase0_exit.py`; no git command |
| `make phase0-exit-preview` | exit 0; both checks `[ ok ]`; checklist printed |
| `uv run pytest tests/scripts/test_phase0_exit.py` | 17 passed |
| `make fmt-check lint types test-unit docs-check` | pass: 923 passed, 49 deselected, 1 xfailed (the known canary gap from 12.2); `docs-check: ok` |
## For the final checkpoint
- Run `make env` if there is no `.env`, then `make phase0-exit` on an idle machine. Expect several minutes for the image builds.
- If `source-docs.sha256` ever has to be regenerated because the two documents were legitimately edited by their owner: `shasum -a 256 brainstorm.md development_plan.md > docs/evidence/phase-0/source-docs.sha256` (the script reads this `sha256sum`-style format).
