# Task 9.4: Deploy runbook
Done 3 Nov 2026. Requirement 17.13. A previous attempt failed on a network error before it wrote anything, so everything here is new.

## Files
- `docs/deploy.md` (new): the runbook.
- `tests/scripts/test_deploy_doc.py` (new): 31 test cases.
- `docs/evidence/free-tier/README.md`: index row for this file.

## Contents of `docs/deploy.md`
Sections of design §14, in this order: steps at a glance (table FU1 to FU9, each "Needs user", with plan items 0.46 to 0.54), topology, limits with sources (from FV0, dated), prerequisites, FU1 to FU6, end-to-end check (`/api/readyz` through Pages), operations (sleep and wake, heartbeat gaps, Neon compute hours, neuron budget, password rotation), FU7, FU8, FU9, teardown, troubleshooting (a symptom, cause and action table covering the `make` targets, Pages proxy codes, readiness codes, Render and Cloudflare failures). Each FU section starts with a bold **Needs user.** line and its plan item.

Evidence used, by section:

| Section | Evidence |
|---|---|
| Limits and sources | FV0 (URLs and dates), FV6, FV7, FV9 |
| FU1, FU2 | FV2 (PostgreSQL 18, `aws-ap-southeast-1`), FV3 (`neondb_owner`, plaintext passwords behind `--allow-plaintext-password`, items still confirmed by the user), FV11 (`verify-full` and the CA bundle path), tasks 9.3 and 4.3 notes |
| FU3 | FV8 (token permissions Workers AI Read and Edit) |
| FU4 | FV6 (a `sync: false` key added after the first Blueprint creation is typed in by hand), task 9.1 notes |
| FU5 | FV6 (open: whether health probes keep the service awake, 20-minute idle check) |
| FU6 | FV7 (order: create project, `pages secret put`, deploy; `wrangler@4.156.2`; Direct Upload, not drag and drop; no `.dev.vars`; fail closed; open items), task 9.2 notes |
| Neon compute hours | FV5 (DBOS polls about 9.5 transactions a second while launched) and FV0 (100 CU-hours; 400 hours at 0.25 CU) |
| Neuron budget | FV8 (error codes 3036 and 5035), FV9 (rates, reasoning tokens) and design §12 |

## Neon compute hours, as stated in the runbook
While Render is awake DBOS keeps Neon awake, so Neon hours follow Render's awake hours plus about five minutes per wake. At 0.25 CU, which is an assumption (autoscaling can raise it to 2 CU), a 10-minute visit costs about 31 minutes of Neon (10 + 15 until Render sleeps + 5 + about 1 to wake), about 0.13 CU-hours, so about 775 such visits fit in 100 CU-hours; five a day cost about 19 CU-hours. If the service never sleeps, 744 hours is about 186 CU-hours and the allowance ends after about 400 hours, around day 17. The runbook labels the table an estimate and says nothing was measured on Neon. It corrects the design text FT-12 where it implied the heartbeat is free: the cron is free, the polling is not.

## Decisions and deviations
- The design says "teardown" and "removing `cairn_pgdata`" without a command for Pages deletion; the runbook uses the dashboard only (Settings, General, Delete project). Whether `wrangler pages project delete` exists was not confirmed, so it is not documented.
- `wrangler login` is added to FU6 as the Pages credential, separate from the Workers AI token. It is not in design §14. The runbook does not describe an API-token alternative because it was not read from Cloudflare's docs.
- The runbook mentions `make workers-ai-live-check` and `mk/providers.mk` by name in FU8 without a link, because the file belongs to task 8.13 and a link would break `make docs-check` until it exists. It says "a few neurons at most (estimate)" for the live check; FV8 and FV9 give no figure for it.
- The runbook never names ParadeDB or `pg_search` (it says "a different database image" and "an extension"), so the scan that task 12.3 extends to `docs/` will not flag it.
- Render and Cloudflare dashboard labels are described loosely ("look for New and Blueprint") because they change and were not checked.
- Nothing in the runbook asks the user to paste a secret into a command line; `wrangler pages secret put` reads the value from a prompt.

## Test (`tests/scripts/test_deploy_doc.py`)
- Each of FU1 to FU9 has a section headed `FUn: ...` with "Needs user" in the heading and the bold line `**Needs user.**` in the body (9 cases); the overview table lists FU1 to FU9 in order, each row marked "Needs user".
- Eleven section headings from design §14 exist (parametrized).
- Limits section cites the Render, Neon, Pages and Workers AI sources and the numbers 750, 100 CU-hours and 10,000 neurons.
- The Neon section states the polling consequence and the 186 CU-hour always-awake case.
- FU6: every `pnpm dlx wrangler` line is pinned to 4.156.2, and `pages project create`, `pages secret put CAIRN_PROXY_TOKEN` and `pages deploy dist` appear in that order; `make web-dist` is named.
- FU7 holds `docker volume rm cairn_pgdata` and says which volume stays; FU1, FU2 and FU8 name `make env-neon`, `make remote-init`, `make remote-status` and the optional `make workers-ai-live-check`; FU4 says a key added later is entered by hand and that owner and admin credentials never go to Render.
- Links: none absolute, none `file://`, no `/Users/`, and `check_links` from `scripts/check_docs.py` finds none broken.
- No 32-character hex literal outside code fences, no connection string with a password, no `KEY=value` assignment of a secret-like key.

## Verify
- `uv run pytest tests/scripts/test_deploy_doc.py`: 31 passed. With `test_pages_config.py`, `test_check_docs.py` and `test_no_paradedb.py`: 110 passed.
- `uv run python scripts/check_docs.py --only links`: ok. `make docs-check`: ok (prd, adr, threat-model, acceptance, contributing, links).
- `ruff check`, `ruff format --check` and `mypy` on the new test: clean.
- `make secrets` reports one finding, in `packages/retrieval/src/cairn_retrieval/models/fake.py` line 38 (rule `generic-api-key`, a comment in task 8.13's file, which another agent is writing). It is not in a file of this task, and `docs/deploy.md`, the test and these notes have no finding. Task 8.13 needs to resolve it before `make gates`.

No git, no volume `cairn_pgdata` touched, no container started, no Neon, Render or Cloudflare call, `.env` untouched, nothing under `mk/` or `packages/` edited. `env-neon`, `remote-init`, `remote-status` and the wrangler commands were not run.

## Open for the user
FU1 to FU9 as listed in the runbook. The three items the runbook says only a live deploy settles: whether Render health probes keep the service awake (FU5), whether `wrangler pages deploy dist` keeps `_routes.json` and picks up `functions/` and the first-request time after a sleep (FU6), and the Neon behaviors of FV3 (FU2).
