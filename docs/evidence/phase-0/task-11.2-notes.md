# Task 11.2 notes: GitHub Actions workflow and `make gates`

Date: 27 Oct 2026. No dependency, lockfile, compose file or Dockerfile touched. No git command run against this repo (one scratch repository in `/tmp` was used to try `gitleaks git`, then deleted). `make gates`, `make smoke`, `make up` and image builds were not run, by instruction: another task was running the obs profile on an 8 GB host. The first real `make gates` run belongs to the final checkpoint.

## Files

| File | State |
|---|---|
| `.github/workflows/ci.yml` | New |
| `mk/gates.mk` | New: `gates` |
| `mk/security.mk` | New target `secrets-history`; the `actionlint` comment and message no longer mention task 11.2 |
| `tests/scripts/test_ci_workflow.py` | New: 11 tests |
| `docs/evidence/phase-0/verify/V9.md` | New: action SHAs and how they were resolved |
| `CONTRIBUTING.md` | `gates` and `actionlint` rows updated, `secrets-history` added, stale "fails until ci.yml exists" text removed; `phase0-exit` stays pending |

## Workflow

- Triggers: `push` (branch `main`), `pull_request`, `schedule` (`0 3 * * *`), `workflow_dispatch`. `permissions: contents: read` at the top, no job widens it. Concurrency group `ci-<workflow>-<ref>` with `cancel-in-progress: true`. Runner `ubuntu-24.04` everywhere. Workflow-level env: `WEB_RUNNER=host`, `HYPOTHESIS_PROFILE=ci`.
- No secrets, no `GITHUB_TOKEN` reference, no `pull_request_target`. Checkouts use `persist-credentials: false`.
- Jobs and steps (every `run:` is one `make` call):

| Job | Timeout | Steps |
|---|---|---|
| `python` | 20 min | `fmt-check`, `lint`, `types`, `test-unit`, `scenario-validate`, `licenses`, `docs-check` (setup-uv, pnpm and Node first, because `licenses` reads web licenses on the host) |
| `web` | 15 min | `web-check` with `WEB_RUNNER=host` |
| `lint-config` | 10 min | `compose-check`, `actionlint` |
| `security` | 15 min | `audit`, `secrets`, `secrets-history`; checkout with `fetch-depth: 0` |
| `integration` (needs `python`) | 30 min | `env`, `up-db`, `test-integration`, `matrix`, `migrations-check`, `smoke`; on failure `make logs LOG_TAIL=1000 > compose-logs.txt` and an artifact upload (7 days) |

- Actions (all pinned by full SHA with a version comment; method and caveats in `verify/V9.md`): checkout v7.0.1, setup-uv v10.3.2 (uv 0.11.25), setup-node v7.1.0 (Node 24.22.1, the same patch as `NODE_IMAGE`), pnpm/action-setup v6.1.0 (pnpm 10.8.1), upload-artifact v7.0.2.
- `make smoke` replaces the design's `up smoke`: `smoke` depends on `up` (task 8.1 notes).

## Full-history gitleaks

New target `make secrets-history` in `mk/security.mk`, so the CI step is still a make call. It runs `gitleaks git /repo` with the same `$(GITLEAKS_IMAGE)` (read from the pre-commit hook pin), the same `.gitleaks.toml`, `--redact`. It exits 2 with a message when the folder has no `.git`, so it is not in `make gates` (the folder is not a repository yet). The container user differs from the checkout owner on a runner, so the target passes `GIT_CONFIG_COUNT/KEY_0/VALUE_0` (`safe.directory=/repo`) as environment, which writes no git config. Tried against a scratch repository: a clean history exits 0 and "no leaks found"; a secret added in one commit and deleted in the next is found ("leaks found: 1"), which is what the working-tree scan cannot do.

## `make gates`

`mk/gates.mk` runs these in order from a shell loop (so `make -j` cannot reorder them), each as `$(MAKE) <target>`, stopping at the first failure:

`fmt-check lint types test-unit web-check scenario-validate compose-check actionlint licenses docs-check audit secrets` then `up-db test-integration matrix migrations-check smoke` then `down`.

- `compose-check` runs before `up-db`; `licenses` runs before `docs-check` (it rewrites `docs/licenses.md`).
- Deviation from design §10's list: `licenses` and `docs-check` swapped, as asked, and `up smoke` is just `smoke`.
- `down` runs only after every gate passed. After a failure the stack stays up for inspection; `down` never removes volumes.
- `make -n gates` prints every gate with the commands inside it (the loop line contains `$(MAKE)`, so make runs it even under `-n`, and the sub-makes inherit `-n`). One side effect: the final "gates passed" line also prints under `-n`.
- `phase0-exit` is not added (task 17.2).

## Verification (what ran, what did not)

| Check | Result |
|---|---|
| `make actionlint` | exit 0 (was failing with "no workflow files") |
| `make -n gates` | all 18 steps, in the order above |
| `uv run pytest tests/scripts/test_ci_workflow.py` | 11 passed (includes a `make -n gates` order check) |
| `make fmt-check lint types` | pass |
| `make test-unit` | 861 passed, 1 xfailed (the known canary gap from 12.2) |
| `make scenario-validate`, `compose-check`, `docs-check`, `secrets`, `audit`, `licenses` | pass |
| `make web-check` | pass on the third attempt (see below) |
| `make gates`, `make smoke`, `make up`, `make test-integration`, `matrix`, `migrations-check`, `up-db` | **not run**, by instruction |
| The workflow on GitHub | **not run**: no remote (U4). Runner-side behavior is unverified: setup-uv cache, pnpm/action-setup with an explicit version, `gitleaks git` on a runner checkout, the `make logs` artifact step |

Two transient failures, both caused by the host and not by this task:

- `make web-check` failed twice at vitest ("Timeout waiting for worker to respond", then a 10 minute stall) while the load average was 18 to 25 with the obs stack starting; it passed at load 6 (2 files, 8 tests). CI runners are not loaded like this, but `web-check` on a busy laptop can need a retry.
- `make licenses` failed once with LIC-3 for `busybox` while task 10.1 was adding the image to `scripts/licenses.py`. It passed on the next run with that entry in place. Not touched here.

I stopped the one `node` container my timed-out `web-check` left running; nothing else was stopped or removed.

## For later tasks

- 17.2: `phase0-exit` should call `$(MAKE) gates` first; the `GATES_ALL` variable in `mk/gates.mk` holds the list for the checklist.
- Final checkpoint: run `make env` (if no `.env`), then `make gates` on an otherwise idle machine; expect several minutes for the image builds.
- When the remote exists (U4): push a branch, read the run, and fix whatever only a runner shows. `test_ci_workflow.py` must change together with any step added to `ci.yml`, since it pins the step lists.
