# Task 9.2: Pages configuration and the /api proxy Function

Done 3 Nov 2026. Requirements 17.4 to 17.11. A previous run was interrupted; the files it left were inspected, found correct, and kept. This run added the missing notes and index rows and re-ran every check.

## Files

- `apps/web/wrangler.toml`: `name = "cairn"`, `pages_build_output_dir = "./dist"`, `compatibility_date = "2026-11-03"`, `[vars] API_ORIGIN` placeholder (FU6). No secret.
- `apps/web/public/_routes.json`: `{"version": 1, "include": ["/api/*"], "exclude": []}`.
- `apps/web/public/_headers`: `/*` with the three nginx security headers; `/assets/*` with immutable caching.
- `apps/web/functions/api/[[path]].ts`: `onRequest` delegating to `proxyToApi`.
- `apps/web/functions/_lib/proxy.ts`: `parseApiOrigin`, `upstreamUrl`, `forwardHeaders`, `proxyToApi`, constants.
- `apps/web/functions/_lib/proxy.test.ts`: 60 vitest cases (`@vitest-environment node`).
- `apps/web/tsconfig.json` (`include` has `functions`) and `vite.config.ts` (`test.include` has `functions/**/*.test.ts`). eslint and prettier already cover the folder (`eslint .`, `prettier --check .`).
- `mk/deploy.mk`: `web-dist` (builds `apps/web/dist` in the pinned Node container; Vite copies `public/_routes.json` and `public/_headers` into it). The user-run Neon targets come in task 9.3.
- `tests/scripts/test_pages_config.py`: 6 tests (`_routes.json`, `_headers`, `wrangler.toml` project and vars, no secret-like key or long hex value, no `apps/web/.dev.vars`, the Function and proxy constants).
- `verify/FV7.md` and the index rows in `README.md`.

## Deviations from design §14 (also in FV7)

- `forwardHeaders(incoming, token, host?)` takes the request host as a third argument, and does not copy `content-length`.
- The request body is read into memory up to 1 MiB (also enforces the limit for chunked requests without `Content-Length`), then sent.
- Runbook order for task 9.4: create the Pages project, `secret put CAIRN_PROXY_TOKEN`, then deploy (Cloudflare requires secrets before the deployment that uses them).
- `docs/deploy.md` pins `wrangler@4.156.2`; it is not added to `apps/web/package.json`.

## Open for the user (FU6)

Whether `wrangler pages deploy dist` from `apps/web` keeps the hand-written `_routes.json` and picks up `functions/`; time of the first request after a Render sleep; the dashboard fail open/closed setting.

## Verify

- `make web-check` (Node 24 container): eslint, prettier, tsc, vitest (68 tests, 60 of them in `proxy.test.ts`), build: passed.
- `uv run pytest tests/scripts/test_pages_config.py`: 6 passed.
- `make web-dist`: passed; `apps/web/dist/_routes.json` and `_headers` exist and equal the `public/` files.
- `ruff check`, `ruff format --check` and `mypy` on `test_pages_config.py`: clean. `make secrets`: no leaks found.

No git, no volume `cairn_pgdata`, no profile started, no Neon, Render or Cloudflare call, `.env` untouched. Nothing under `packages/core`, migrations or Postgres was edited.
