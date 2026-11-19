# Task 8.7 notes: Workers AI client, parsers and contract fixtures
Date: 3 Nov 2026 (host clock). Requirements 14.1 to 14.9, 14.11, 15.1, 15.8; design §11; decisions FT-20, FT-21. `.env` was not touched. No Cloudflare, Neon or Render call was made; every test uses `httpx.MockTransport`. `mk/deploy.mk` and `tests/scripts/test_deploy_targets.py` (task 9.3, another agent) were not edited. One read-only `git status` was run by mistake while `uv lock` was being checked, with its output discarded; no git command changed anything.

## State found
A previous run had left `cloudflare/parse.py` (correct, kept) and `budget.py` (task 8.3). `client.py`, the fixtures and the client and parser tests did not exist.

## What exists
- `packages/retrieval/src/cairn_retrieval/cloudflare/client.py`: `WorkersAiError` (`str()` is `<code> model=<model> status=<status> attempts=<n>`), `RetryPolicy`, `Outcome`, `DAILY_LIMIT_CODES = {3036}`, `classify`, `parse_retry_after`, `backoff_delay`, `WorkersAiClient` (`run`, `aclose`, async context manager), `RunResult`. Each attempt reserves its estimate first; a refusal gives `budget_exhausted` with no request; missing account id or token gives `not_configured` with no request. The model id is checked against `^@cf/[a-z0-9._-]+/[a-z0-9._-]+$`. httpx exceptions are reduced to a kind and not chained, so their text (which can name the URL) never survives. `httpx` and `httpcore` loggers are raised to WARNING; the one log line `workers_ai.call` carries model, attempts, status, latency, neurons reserved and error code.
- `cloudflare/parse.py` fix: `_is_finite_number` no longer raises `OverflowError` on an integer too large for a float (now `MalformedResult`); `TypeGuard` annotations for mypy.
- `packages/retrieval/pyproject.toml`: `httpx==0.28.1` (already pinned in the dev group; `uv lock --check` passes).
- Fixtures (13 JSON files) in `packages/retrieval/tests/unit/data/workers_ai/`: embeddings, rerank, text in the OpenAI shape, text with null content and `finish_reason: length`, legacy `response` text, a malformed embedding result, and error envelopes for 400, 401, 403 (paid model), 429 capacity (3040), 429 daily limit (3036), 500, 503.
- Tests: `test_workers_ai_parse.py` and `test_workers_ai_client.py` (URL, method, bearer header, body shape, timeouts from settings and the per-call override, every error class, retry counts, `Retry-After` present, capped and absent or unusable, timeouts, transport failure, `not_configured` and `budget_exhausted` with a transport that fails if called, reservation per attempt, bad responses, log and repr hygiene with caplog at DEBUG, httpx and httpcore log levels, `classify`, `backoff_delay`, `parse_retry_after`).

## FV8
Re-checked against Cloudflare's error page; unchanged. Decisions on 408, transport errors and `Retry-After` handling are recorded in `verify/FV8.md`. Reasoning tokens count as output (FV9): the client does no budgeting itself; callers pass `estimate_neurons` built from `max_tokens_out`.

## Verified
- `uv lock --check`: ok.
- `uv run pytest packages/retrieval/tests/unit`: 208 passed.
- `make fmt-check lint types`: clean.
- `make audit`: exit 0 (pip-audit: no known vulnerabilities; pnpm reports 1 moderate, below the high threshold).
- `make test-unit`: 1654 passed, 1 xfailed (`canary_token` strict xfail unchanged), 1 failed: `tests/scripts/test_licenses.py::test_real_tool_images_are_pinned_by_digest` fails on `cairn-python:local` in `mk/deploy.mk`, a file owned by task 9.3 and untouched here.
