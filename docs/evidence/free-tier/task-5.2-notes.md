# Task 5.2: the proxy token check and the healthcheck header
Date: 3 Nov 2026. Requirements 9.1 to 9.6; decision FT-16 (design §8). No dependency was added. The local `.env` and the compose file were not touched. Only the core stack was used; it is still up.

## Files
| File | State |
|---|---|
| `apps/api/src/cairn_api/proxy_token.py` | New: `EXEMPT_PATHS = {"/healthz"}`, `HEADER = "x-cairn-proxy-token"`, `allowed(path, presented, expected)`, `not_found()`, `install(app, expected)` |
| `apps/api/src/cairn_api/main.py` | `build_app` calls `proxy_token.install(app, settings.proxy_token)` before the access-log middleware is added, so the proxy check sits inside it and refused requests are still access-logged (path, method, status 404; no header) |
| `packages/core/src/cairn_core/config.py` | `ApiSettings.proxy_token: SecretStr \| None` (alias `CAIRN_PROXY_TOKEN`, `min_length=32`). `CombinedSettings` inherits it through `ApiSettings`. An empty variable counts as unset, as for every other key |
| `apps/api/src/cairn_api/healthcheck.py` | `check(url, timeout_s, proxy_token=None)` adds `X-Cairn-Proxy-Token` when the token is not empty; `main` passes `CAIRN_PROXY_TOKEN` from the environment. Standard library only; nothing about the token is printed |
| `apps/api/tests/unit/test_proxy_token.py` | New: 27 tests |
| `apps/api/tests/unit/test_healthcheck.py` | 5 tests added (header against a local recording server) |
| Tests changed (one line each) | `test_main.py`, `test_readiness_properties.py`, `test_readyz_body_properties.py`, `apps/api/tests/integration/test_readyz_integration.py`: the settings they build now pass `"CAIRN_PROXY_TOKEN": None` (see below). `test_main.py` also lists `CAIRN_PROXY_TOKEN` in `CAIRN_ENV_KEYS` |

## Behavior
- Check is off when no token is configured: no middleware is installed, so the split `api` service and every Phase 0 test behave as before.
- With a token: `/healthz` (exact path) passes without a header. Any other path, any method, answers `404 {"error": "not_found"}` with `Cache-Control: no-store` unless the header matches. The body and headers of a missing-header and a wrong-header refusal are identical, and match what an unknown route would reveal to a caller who has no token.
- Compare: `hmac.compare_digest` over UTF-8 bytes of both sides. A missing header returns False before any comparison. A test replaces `compare_digest` with a spy and checks the exact byte pairs; another proves it is not called when the check is off or the path is exempt. `compare_digest` on `bytes` also accepts non-ASCII input, which it would reject for `str`.
- A matching header reaches the route, including the router's own 404 for unknown paths (its body is FastAPI's `{"detail": "Not Found"}`, not the refusal).
- Hygiene: `proxy_token.py` has no logger. The access log records method, path (no query string), status and duration. A test captures the JSON access log and the responses for a refused request with a distinctive presented value and for an accepted one, and finds neither the token nor the presented value. `SecretStr` keeps the token out of `repr` and `model_dump_json`.
- Short token: `load_settings(ApiSettings)` and `load_settings(CombinedSettings)` print `invalid configuration: CAIRN_PROXY_TOKEN (string_too_short)` to stderr and exit 2; the value is not printed (tested).
- Install point: `build_app`, which `create_app` of the split API and `cairn_combined.create_app` both call; a test builds the app from `CombinedSettings` and checks 404, 200 with the header, and the exempt `/healthz`.

## One consequence worth knowing: the local `.env` now matters to tests
`ApiSettings.model_validate({...})` still goes through the settings sources (BaseSettings has its own `__init__`), so it reads `.env` from the current directory. Task 1.2 put a 32-character `CAIRN_PROXY_TOKEN` into the local `.env`. The first run after the change turned 19 existing tests red (the app answered 404 to `/readyz`), although CI, which has no `.env`, would have passed them. Each place that builds `ApiSettings` for an app without a token now passes `"CAIRN_PROXY_TOKEN": None` explicitly (init values beat the environment and `.env`). A module-level `conftest.py` patch was tried and dropped: one test file builds its client at import time, and a collection-time patch would leak into the `.env` tests of other directories. Future tests that build `ApiSettings` for `build_app` should do the same. `apps/combined/tests/unit/test_app.py` already changes directory to a temporary one, so it needed nothing.

## Not done here (other tasks)
- `ft-app` in compose, which sets `CAIRN_PROXY_TOKEN` for the container (task 6.1). The core `api` service does not pass the variable, so the running stack has no proxy check and its healthcheck sends no header.
- Properties 10 and 11 (tasks 5.5 and 5.6). The unit tests here are examples.
- The Pages Function that sets the header (Requirement 18).

## Verify results
```text
$ uv run pytest apps/api/tests/unit apps/combined
125 passed
$ make types
mypy --strict packages/core: no issues in 30 source files / base: no issues in 112 source files
$ make fmt-check lint types test-unit
1287 passed, 54 deselected, 1 xfailed (the canary xfail)
$ uv run pytest -m integration apps/api     (core db up)
6 passed
```
Ruff S105 flagged constants named `*_TOKEN_*` in `healthcheck.py`; they are `ENV_KEY` and `HEADER_NAME` there. No `# noqa` was added.
