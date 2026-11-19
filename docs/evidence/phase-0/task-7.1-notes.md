# Task 7.1 notes: web placeholder (Vite, React, TypeScript, TanStack Query)

Date: 27 Oct 2026. No Python files, `uv.lock` or shared Make fragments touched. `make web-check` passes (exit 0): eslint, prettier, tsc, vitest (3 tests), vite build.

## Files

| File | State |
|---|---|
| `apps/web/package.json` | exact pins, `packageManager: pnpm@10.8.1`, `engines.node: ">=24 <25"`, scripts `dev build preview lint format format:check typecheck test check` |
| `apps/web/.npmrc` | `save-exact=true`, `engine-strict=true` |
| `apps/web/pnpm-lock.yaml` | lockfile v9 with sha512 integrity hashes, generated in the pinned container (`make web-lock`) |
| `apps/web/tsconfig.json` | `strict`, `noUncheckedIndexedAccess`, `exactOptionalPropertyTypes`, `verbatimModuleSyntax`, `noEmit`; `types: ["vite/client", "node"]` |
| `apps/web/eslint.config.js` | flat config: eslint recommended, typescript-eslint recommended, react-hooks, react-refresh (vite) |
| `apps/web/.prettierrc.json`, `.prettierignore` | width 100, single quotes, trailing commas |
| `apps/web/vite.config.ts` | `defineConfig` from `vitest/config`; jsdom; dev server on 127.0.0.1 proxies `/api` to `127.0.0.1:8000` with the prefix stripped (same as the nginx proxy in 7.2) |
| `apps/web/index.html`, `src/main.tsx` | entry; `AppProviders` wraps `App` at the root |
| `apps/web/src/App.tsx` | `<main>` with `<h1>Cairn</h1>` and "Phase 0 scaffold" |
| `apps/web/src/providers.tsx`, `src/query-client.ts` | `AppProviders` (QueryClientProvider); `createQueryClient` in its own file so react-refresh stays quiet |
| `apps/web/src/App.test.tsx`, `src/setup-tests.ts` | heading test; provider tests (client reachable under the provider, throws without it); jest-dom matchers and cleanup |
| `mk/web.mk` | `web-check`, `web-fmt`, `web-lock`, `web-clean` |
| `docs/evidence/phase-0/verify/V8.md` | Node 24 image, digest and corepack result |

## Pinned versions

| Package | Version | Package | Version |
|---|---|---|---|
| react, react-dom | 19.3.1 | eslint | 10.13.0 |
| @tanstack/react-query | 5.105.0 | @eslint/js | 10.0.1 |
| vite | 8.3.5 | typescript-eslint | 8.72.1 |
| @vitejs/plugin-react | 6.1.2 | eslint-plugin-react-hooks | 7.1.1 |
| vitest | 5.0.5 | eslint-plugin-react-refresh | 0.5.7 |
| jsdom | 30.1.3 | globals | 17.13.0 |
| @testing-library/react | 16.3.3 | prettier | 3.9.10 |
| @testing-library/jest-dom | 7.0.1 | **typescript** | **6.0.3** |
| @testing-library/dom | 10.4.2 | **@types/node** | **24.19.2** |
| @types/react, @types/react-dom | 19.3.0 | | |

Image: `node:24.22.1-alpine3.24@sha256:06d7adf3e0ef61a13f1d28682726366f97a15aa1888c934d3ecec86c33918b53` (Node 24.22.1, corepack 0.36.0, pnpm 10.8.1 through corepack).

## Deviations from "latest on npm today"

- typescript 6.0.3 instead of 7.0.2. typescript-eslint 8.72.1 declares `typescript >=4.8.4 <6.1.0`, so 7.x would break the lint gate. Move to 7.x when typescript-eslint supports it.
- @types/node 24.19.2 instead of 26.6.5, to match the Node 24 runtime.
- @testing-library/dom 10.4.2 added explicitly. It is a peer of @testing-library/react 16 and jest-dom 7 and pnpm does not install peers on its own here.
- The design's `"engines": {"node": ">=24 <25"}` is kept. The host Node 20 fails `engine-strict` on purpose, which is why `WEB_RUNNER=host` needs Node 24.

## Notes for later tasks

- Runner: `make web-check` runs `pnpm install --frozen-lockfile && pnpm run check` in the pinned image. `apps/web` is bind-mounted at `/work`; `node_modules` is in the named volume `cairn-web-node-modules`, and the pnpm store, corepack home and pnpm shim are in `cairn-web-cache`. A short root step chowns both volumes to the host user first, so `dist/` and `pnpm-lock.yaml` are not root-owned. `make web-clean` removes the volumes and `dist/`.
- Docker leaves an empty `apps/web/node_modules` mount point on the host. It is gitignored. Do not install on the host.
- `make web-lock` refreshes the lockfile; `make web-fmt` runs `prettier --write`. Run `make web-lock` after any `package.json` change, because `web-check` uses `--frozen-lockfile` and `CI=true`.
- First run downloads the image and installs about 236 packages (roughly a minute for install). Later runs reuse the volumes and report "Already up to date" for the install step.
- CI web job (task 11.2): set `WEB_RUNNER=host`, use actions/setup-node with Node 24 (match `engines`), pnpm/action-setup with 10.8.1, then `make web-check WEB_RUNNER=host`. Host mode runs `cd apps/web && pnpm install --frozen-lockfile && pnpm run check`.
- 7.2 container build: stage 1 `FROM` the same reference as `NODE_IMAGE` in `mk/web.mk`; run `corepack enable`, `pnpm install --frozen-lockfile`, `pnpm build`. `dist/` is the output, nginx proxies `/api/` to `http://api:8000/`, and the dev proxy already strips the `/api` prefix. `.dockerignore` already excludes `node_modules` and `dist`.
- 7.3 widget: call `GET /api/readyz` through `useQuery` inside `AppProviders`. `createQueryClient` sets `retry: false`, so a failed call shows "Not ready" at once. Show the state as text. Tests should wrap in `AppProviders client={createQueryClient()}` for a fresh cache and stub `fetch`.
- pnpm 10 skips dependency install scripts by default, and nothing here needed one. If a later dependency needs a build script, add it to `pnpm.onlyBuiltDependencies`.
- The pre-commit `web-check` hook now resolves, since the target exists.
- Still open from V8: `pnpm audit --audit-level high` endpoint check (task 11.1).
- The shell tool returned empty output for some long commands during this task; results were read from log files instead. The final run's log was `exit=0`.
