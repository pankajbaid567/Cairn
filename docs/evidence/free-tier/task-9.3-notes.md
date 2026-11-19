# Task 9.3: The user-run Neon targets
Done 3 Nov 2026. Requirements 13.5, 13.6 and 17.12. A previous run was interrupted; `mk/deploy.mk` and `tests/scripts/test_deploy_targets.py` were inspected, found correct, and kept. This run changed the help text wording, added these notes and the index row, and re-ran the Verify.
## Targets (`mk/deploy.mk`)
- `env-neon`: refuses when `.env.neon` exists (file or symlink) before it runs Python; otherwise runs `scripts/dev_env.py --template .env.neon.example --out .env.neon` (mode 0600, fresh random secrets for each `change-me`, `user-provided` keys written empty).
- `remote-init`: `docker run --rm --env-file .env.neon cairn-python:local cairn-db init --allow-plaintext-password`. The flag is the OQ-1 decision of 2 Nov 2026; `.env.neon.example` carries `CAIRN_DB_SSLMODE=verify-full`.
- `remote-status`: the same container running `cairn-db status` (no flag).
- Both depend on two internal targets without `##` text (hidden from `make help`). `neon-env-check` fails before any docker call when `.env.neon` is missing ("not found", hint `make env-neon`) or when a key that the template marks `user-provided` is empty, blank or quoted-empty; it names the keys only, never a value, and says no connection was opened. `neon-image` builds `cairn-python:local` (`PACKAGE=cairn-core`, the compose `db-init` build) only when the image is absent.
- Help text of all three starts "Neon, user-run:". Changed in this run from "User-run:" so that it matches the task wording. None of the targets is in `GATES_ALL`.
- `DEV_ENV_PYTHON`, `NEON_ENV_FILE`, `NEON_ENV_TEMPLATE` and `NEON_IMAGE_REF` are overridable; the tests use the first.
## Tests (`tests/scripts/test_deploy_targets.py`, 17 cases with parametrization)
Each runs in a temporary copy of `mk/`, `Makefile`, `scripts/dev_env.py` and `.env.neon.example`, with a stub `docker` first on `PATH` that only logs its arguments. The repository's `.env.neon` is never created or read.
- `env-neon` writes (mode 0600, secrets of at least 24 characters, user keys empty, `verify-full`), then refuses and leaves the bytes unchanged; an existing file is refused before Python runs.
- Missing file, and empty or blank user-provided keys (`""`, `''`, spaces), fail with the hint and make no docker call; no secret appears in the output.
- With keys filled by fake values, exactly one `docker run` is logged, with `--allow-plaintext-password` for init only; a missing image gives `image inspect`, `build`, `run` in that order.
- `make -n remote-init` shows the flag on `cairn-db init`, calls nothing and creates no file; `make -n remote-status` has no flag.
- `make help` marks the three targets "user-run" and hides the internal ones; `make -n gates` mentions none of them.
## Verify
- `uv run pytest tests/scripts/test_deploy_targets.py tests/scripts/test_dev_env.py`: 48 passed.
- `make -n remote-init >/dev/null`: exit 0.
- `ruff check`, `ruff format --check` and `mypy` on the test file: clean. `make secrets`: no leaks found.
No git, no volume `cairn_pgdata`, no profile started, no Neon, Render or Cloudflare call; `env-neon`, `remote-init` and `remote-status` were not run in the repo (no `.env.neon` exists); `.env` untouched; nothing under `packages/retrieval` or `uv.lock` edited.
## Open for the user
FU1 (fill `CAIRN_DB_HOST` and `CAIRN_DB_ADMIN_PASSWORD` in `.env.neon` after `make env-neon`) and FU2 (`make remote-init`, `make remote-status`).
