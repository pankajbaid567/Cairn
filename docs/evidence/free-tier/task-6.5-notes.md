# Task 6.5: freetier-check in the gates, CI and the exit gate
Date: 3 Nov 2026 (host clock). Requirements 11.7, 11.8, 16.11, 21.1, 21.2, 21.3; design §17; decision FT-19. No dependency was added; `.env` was not touched. No git command was meant to run (see "Deviations").

**Result: PASS.** `make actionlint`, `tests/scripts/test_ci_workflow.py` and `tests/scripts/test_phase0_exit.py` pass; `make -n gates` lists `freetier-check` last; `make down && make freetier-check` passed end to end (about 8 minutes); `make phase0-exit-preview` exits 0 with all three checks `[ ok ]`. `make fmt-check lint types test-unit docs-check` pass (1371 passed, 1 xfailed). Core was stopped for the run and is up again (`make up`: db, api, worker, web healthy; `make db-status`: head 1, pending 0). `make gates` and `make phase0-exit` were not run (task 14.1).

## Files
| File | Change |
|---|---|
| `mk/gates.mk` | `GATES_FREETIER := freetier-check`; `GATES_ALL := $(GATES_STATIC) $(GATES_STACK) down $(GATES_FREETIER)`; header comment says why the order matters and that `workers-ai-live-check` is no gate |
| `mk/exit.mk` | Comments: EG1 to EG5, FU1 to FU9, the core db-image and free-tier evidence checks, the longer `gates` |
| `mk/freetier.mk` | New target `freetier-logs` (`docker compose --profile freetier logs --no-color --tail $(LOG_TAIL)`), so the CI step stays a single make call. `LOG_TAIL` comes from `mk/stack.mk` |
| `.github/workflows/ci.yml` | Integration job after `make smoke`: `make down`, `make freetier-check`; on failure `make logs LOG_TAIL=1000 > compose-logs.txt` (as before) and `make freetier-logs LOG_TAIL=1000 > freetier-logs.txt`; the existing upload step now uploads both files (same SHA-pinned action). `timeout-minutes` of the job 30 to 45 for the extra 8 to 10 minutes. Every `uses:` is unchanged |
| `scripts/phase0_exit.py` | See below |
| `tests/scripts/test_ci_workflow.py` | Integration step order (`smoke`, `down`, `freetier-check`), the log steps and artifact paths, `workers-ai-live-check` absent from the workflow and from every `GATES_*` line, and the `make -n gates` order |
| `tests/scripts/test_phase0_exit.py` | Cases with temporary evidence directories (below) |
| `CONTRIBUTING.md` | Quality gates table (new free-tier row and a sentence on the order), `freetier-*` row (`freetier-logs`, duration), `gates` row, `phase0-exit` row |
| `docs/evidence/free-tier/README.md`, `freetier-ram-2026-11-03.{md,json}` | Index row; the RAM report of this date was rewritten by the run (same date, so same file name) |

## phase0_exit.py
- `find_freetier_evidence(dir)`: the newest `freetier-ram-<date>.json` by the date in its name; no file, unreadable JSON or `result` other than `pass` is a problem. A newer failed file is not hidden by an older pass. The markdown file is not required (Requirement 21.1 names the JSON).
- `find_ram_evidence`: also reads `images` and requires the `db` container (`<project>-db-<n>`) reference to start with `pgvector/pgvector`. A report with another image, or none, is a problem ("run make ram-report"). `RamEvidence` gained `db_image` (default `None`, so existing three-argument constructions still work).
- `GATE_LINES` gained EG5 (`make freetier-check`); `FU_STEPS` FU1 to FU9 with the plan items 0.46 to 0.54 in the bracket, printed right after U7 in the same section as NOT automated; `USER_STEPS` is still U1 to U7. The closing line names FU1 to FU6, FU7, optional FU8, and U5 with FU9.
- `RUN_TARGETS = ("gates", "freetier-check", "s1-smoke", "s2-smoke")`; `freetier-check` is the last gate of `gates`, so it has passed when the others did.
- `render_checklist` takes `freetier` and `freetier_problems` (required keywords); `run_checks` takes `freetier_dir` (default `docs/evidence/free-tier`). Exit 1 when any of the three checks fails.
- New/changed tests: newest-report selection and problems for both evidence kinds, the pgvector image rule (earlier image, no db container, nonsense `images`, newest report decides), FU1 to FU9 after U7 and mapped to plan items 0.46 to 0.54, EG5 and the free-tier lines in the printout, and `run_checks` exit status with temporary core and free-tier directories. The tests use a placeholder image name for the earlier image because `test_no_paradedb.py` scans `scripts` and `tests`.

## The real run
`make down`, then `make freetier-check` (foreground steps: `freetier-up` about 2.5 minutes, `freetier-smoke` 12 checks, `matrix-managed` 13 passed 1 skipped, `freetier-ram` with its own 2.5 minute bring-up and 2 minutes of sampling, `freetier-down`). Result: PASS. Idle peak 94.34 MiB (budget 300), load peak 94.52 MiB (budget 384), 0 of 298 load requests failed, not OOM-killed, restart count 0. After the run `docker ps` showed only the openwa-api container of another project, untouched.

## Verify notes
- `make -n gates` still runs the recursive `$(MAKE)` lines, so its output lists `freetier-up`, `freetier-smoke`, `matrix-managed`, `freetier-ram`, `freetier-down` after `freetier-check`. The dry-run test expects them.
- On a fresh CI checkout `make env` writes `CAIRN_PROXY_TOKEN` and `CAIRN_MANAGED_ADMIN_PASSWORD` (both `change-me` in `.env.example`), so no `env-update` step is needed in the workflow.
- The first remote CI run (user step U4) has not happened; the runner's time for `freetier-check` is unknown. The 45 minute timeout is a guess based on the local 8 to 10 minutes.

## Deviations
- A read-only `git status` was run once by mistake while looking at the repository (output discarded, nothing changed). No other git command ran, and nothing was committed.

## For later tasks
- **8.13:** `workers-ai-live-check` goes into `mk/providers.mk` only; `test_live_workers_ai_check_is_in_no_gate_and_no_workflow` keeps it out of the workflow and the `GATES_*` lines.
- **12.3 / 13.3 / 14.1:** `phase0-exit` needs the newest `freetier-ram-<date>.json` to pass and the newest `core-ram-<date>` report to name `pgvector/pgvector`; both hold today. The hash record is untouched by this task.
