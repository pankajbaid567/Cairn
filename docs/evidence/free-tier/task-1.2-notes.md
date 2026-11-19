# Task 1.2: new template keys and the append-only env update

Date: 1 Nov 2026. Requirements 13.1 to 13.4 and 22.3; design §6 and §14.

## What changed

- `.env.example`: new keys of design §6, with the fake embedder, reranker, answer and judge providers selected. The ParadeDB comment is gone (image pins live in `docker-compose.yml`). The header documents `user-provided` and `make env-update`.
- `.env.neon.example`: new, as in design §14. `.gitignore` gains `!.env.neon.example`; `.env.neon` stays ignored by `.env.*`.
- `scripts/dev_env.py`: `user-provided` is written as an empty value (the settings use `env_ignore_empty`, so it counts as unset). `--add-missing` appends a dated comment and every absent template key; the output is opened with `O_APPEND | O_NOFOLLOW` and nothing is opened when no key is missing. `--output` gained the alias `--out`. A missing output under `--add-missing` is created as by `make env`.
- `mk/env.mk`: `env-update` target.
- Tests: `tests/scripts/test_dev_env.py` covers `user-provided`, append after a dated comment, the newline guard, byte-preserving update with mode kept, no-op on a complete file (bytes and mtime), names-only output, `--out`, and the existing overwrite refusal. The template rule runs on both templates and accepts `change-me` or `user-provided`. Property 19 (`test_dev_env_properties.py`) now excludes `user-provided` from its plain values, since that string is a placeholder now.

## The local `.env`

- Backed up before the first run to `var/.env.backup-20261101T170047` (mode 0600, `var/` is gitignored). The backup is the user's to delete.
- `make env-update` was run once. Afterwards the first 2,736 bytes of `.env` (all 59 original lines) have the same SHA-256 as the backup, `CAIRN_DB_PORT=55432` and `CAIRN_TEST_ADMIN_PORT=55432` are unchanged, and the mode stays 0600. A second run leaves the file unchanged (same SHA-256).
- Appended key names, in order, after the comment `# Added by make env-update on 2026-11-01: ...` (values not recorded):
  - Generated secrets (`change-me`): `CAIRN_MANAGED_ADMIN_PASSWORD`, `CAIRN_PROXY_TOKEN`
  - Empty, for the user (`user-provided`): `CLOUDFLARE_ACCOUNT_ID`, `CLOUDFLARE_API_TOKEN`, `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`
  - Template defaults: `CAIRN_DB_SSLMODE`, `CAIRN_FREETIER_DB_PORT`, `CAIRN_FREETIER_APP_PORT`, `CAIRN_WORKERS_AI_BASE_URL`, `CAIRN_WORKERS_AI_DAILY_NEURONS`, `CAIRN_WORKERS_AI_BUDGET_STORE`, `CAIRN_WORKERS_AI_CONNECT_TIMEOUT_S`, `CAIRN_WORKERS_AI_READ_TIMEOUT_S`, `CAIRN_WORKERS_AI_MAX_ATTEMPTS`, `CAIRN_EMBEDDER`, `CAIRN_EMBED_MODEL`, `CAIRN_EMBED_DIM`, `CAIRN_EMBED_QUERY_PREFIX`, `CAIRN_RERANKER`, `CAIRN_RERANK_MODEL`, `CAIRN_ANSWER_PROVIDER`, `CAIRN_ANSWER_MODEL`, `CAIRN_JUDGE_PROVIDER`, `CAIRN_JUDGE_MODEL`
- 25 keys in total.

## Verify

`uv run pytest tests/scripts/test_dev_env.py && make env-update && grep -q '^CAIRN_DB_PORT=55432$' .env && a=$(shasum -a 256 .env) && make env-update && [ "$a" = "$(shasum -a 256 .env)" ]`, plus `make fmt-check lint types`.

Result on 1 Nov 2026: all passed (31 tests in `test_dev_env.py`; both `env-update` runs after the first reported the file unchanged and the SHA-256 matched). `make compose-check` passes for the core, obs and experiments profiles with the new `.env.example`, and all 237 tests in `tests/scripts` pass.
