# Task 7.3 notes: readiness widget

Date: 27 Oct 2026. Only `apps/web/` and this file were touched. No new dependency, so the lockfile is unchanged. `make web-check` passes (exit 0): eslint, prettier, tsc, vitest (8 tests in 2 files), vite build.

## Files

| File | State |
|---|---|
| `apps/web/src/use-readiness.ts` | `fetchReadiness` (`fetch('/api/readyz')`, returns `'ready'` only for status 200, otherwise `'not_ready'`, rejects on network error) and `useReadiness` (`useQuery`, key `['readiness']`, refetch every 15 s) |
| `apps/web/src/ReadinessStatus.tsx` | `<p role="status" aria-live="polite">`: "Checking", "Ready", or "Not ready" with a text detail ("API unreachable" or "API reports not ready") |
| `apps/web/src/App.tsx` | renders `<ReadinessStatus />` after the unchanged `<h1>Cairn</h1>` and "Phase 0 scaffold" |
| `apps/web/src/ReadinessStatus.test.tsx` | 5 tests with stubbed `fetch`: pending, 200, 503, 502, network error |
| `apps/web/src/App.test.tsx` | heading test now stubs `fetch` with a never-resolving promise so it stays off the network |

## Behavior

- Status is text only. The response body is never read, so extension versions and role names cannot reach the page. A test checks this for a 503 body that contains them.
- Any status other than 200 and any network error show "Not ready". `createQueryClient` has `retry: false`, so the error shows at once.
- The hook uses a relative URL, so it works through the Vite dev proxy and the nginx `/api/` proxy.

## For later tasks

- 7.2 (container): nginx must proxy `/api/` to `http://api:8000/` with the prefix stripped, so `/api/readyz` reaches `/readyz`. If an SPA fallback (`try_files … /index.html`) caught `/api/readyz`, nginx would answer 200 and the widget would wrongly show "Ready". Keep the `/api/` location separate and give it no fallback.
- 8.1 (smoke): the page text is server-independent at load ("Checking" first). A browser-free smoke check should hit `GET /api/readyz` through the web container on 8080 and expect 200 and JSON, which proves the proxy. Do not assert on the widget text from the raw HTML, because it renders client-side.
- The widget polls every 15 s while the page is open. The web healthcheck (`wget` on `/`) is not affected.
