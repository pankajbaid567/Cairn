# Task 7.2 notes: web container and compose service

Date: 27 Oct 2026. No dependency, lockfile, Python or `uv.lock` change. `scripts/licenses.py` needed no change: `nginxinc/nginx-unprivileged` (BSD-2-Clause) and `node` already have rows in `IMAGE_LICENSES`, and `make licenses` finds both images through `apps/web/Dockerfile` and passes.

## Files

| File | State |
|---|---|
| `apps/web/Dockerfile` | New. Stage 1 `node:24.22.1-alpine3.24@sha256:06d7adf3...` (same reference as `NODE_IMAGE` in `mk/web.mk`): corepack, `pnpm install --frozen-lockfile`, `pnpm run build`. Stage 2 `nginxinc/nginx-unprivileged:1.31.7-alpine3.24@sha256:b5047b9b...`: `nginx.conf` and `dist/`, user 101, port 8080 |
| `apps/web/nginx.conf` | New. Full `nginx.conf` (replaces the image's) |
| `apps/web/.dockerignore` | New. The build context is `apps/web`, so the root `.dockerignore` does not apply |
| `docker-compose.yml` | `web` service added; header comments updated |
| `docs/evidence/phase-0/verify/V8.md` | Section on the nginx image: tag, digest, how it was resolved |

## Image pin

`nginxinc/nginx-unprivileged:1.31.7-alpine3.24@sha256:b5047b9b76845eac124358373c6852377c52e4b05834a47647e955dd91cb8f1e`, the multi-arch index digest. Resolved with `docker buildx imagetools inspect` and cross-checked against the Docker Hub tags API (same digest; the tags `alpine`, `mainline-alpine`, `1-alpine`, `1.31-alpine`, `1.31.7-alpine` and the `alpine3.24` variants point at it). Details in `verify/V8.md`.

## nginx.conf

- `/api/` is proxied to `http://api:8000` with the prefix stripped (`/api/readyz` reaches `/readyz`). The location has no `try_files`, so an API 404 stays a 404. `/api` without the slash returns 404. The SPA fallback (`try_files $uri $uri/ /index.html`) applies to `/` only; `/assets/` returns 404 for missing files; `/index.html` is `no-cache`.
- Deviation in form, not in behavior: the upstream is a variable (`set $api_upstream http://api:8000; rewrite ^/api/(.*)$ /$1 break; proxy_pass $api_upstream;`) with `resolver 127.0.0.11 valid=10s`. A literal `proxy_pass http://api:8000/;` resolves the name once at start and keeps the old address when `docker compose up --build api` recreates the api container. Checked live: api stopped gives 502, api recreated gives 200 with no web restart. Cost: `rewrite ... break` passes the decoded URI, which is fine for the two endpoints that exist.
- Everything nginx writes is under `/tmp` (pid, `*_temp`); logs go to `/dev/stdout` and `/dev/stderr`. `worker_processes 1` (the image default `auto` would start one per host CPU, 8 here, inside a 64m limit). `server_tokens off`, `nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`. No CSP: not tested in a browser here.
- The stock `conf.d/default.conf` is removed in the Dockerfile; the image's IPv6 entrypoint script then exits quietly.

## The web service

| Item | Value |
|---|---|
| Image / build | `cairn-web:local`, context `apps/web`, `Dockerfile` |
| Hardening | `user: 101:101`, `read_only: true`, `tmpfs: /tmp:size=16m,mode=1777`, `cap_drop: [ALL]`, `no-new-privileges:true` (inspect confirms all of them) |
| Depends on | `api: service_healthy` (which brings `db-init` and `db`) |
| Limit / port | `mem_limit: 64m` (inspect: 67108864); `127.0.0.1:8080:8080` only (`docker port`: `8080/tcp -> 127.0.0.1:8080`) |
| Healthcheck | `wget -q -O /dev/null http://127.0.0.1:8080/`, interval 10 s, timeout 5 s, 6 retries, start period 5 s. 127.0.0.1 because nginx listens on IPv4 only |
| Memory | 8.3 MiB of 64 MiB at rest |

Memory sum for the long-running core services: db 1024m + api 384m + worker 384m + web 64m = 1856m, within the 4 GB of Requirement 4.5. The header comment in `docker-compose.yml` says so.

## Verification (live)

`docker compose --profile core config --hash db` gave `6e9b0dfb...`, equal to the label on the running `cairn-db-1`, so `up` would not recreate it. Afterwards `cairn-db-1` had the same `StartedAt` (14:46:50 UTC) and was healthy.

```text
$ docker compose --profile core up -d --build --wait web   # db-init ran and exited 0; api, web Healthy
$ curl -fsS http://127.0.0.1:8080/ | grep -q Cairn          # 200, title "Cairn", no-cache
$ curl http://127.0.0.1:8080/api/readyz                      # 200 application/json, status ready, pg 18.6, pg_search 0.26.3, vector 0.8.6, btree_gist 1.8, schema 1/1
$ curl http://127.0.0.1:8080/api/healthz?x=1                 # 200 {"status":"ok"} (query string forwarded)
$ curl http://127.0.0.1:8080/api/nope                        # 404 application/json (from the API, not the page)
$ curl http://127.0.0.1:8080/api                             # 404
$ curl http://127.0.0.1:8080/some/route                      # 200 text/html (SPA fallback)
$ curl http://127.0.0.1:8080/assets/missing.js               # 404
```

Then `docker compose --profile core stop web api` and `rm -f web api db-init`: only what this task started. `cairn-db-1`, the `cairn_pgdata` volume and the other projects' containers were not touched. No `down`, no `--volumes`.

## For later tasks

- 8.1 (smoke): `GET http://127.0.0.1:8080/api/readyz` should return 200 JSON; the page text is rendered client-side, so check for the `<title>Cairn</title>` or `grep Cairn` as above. Web container: `cairn-web-1`, port 8080.
- 10.1 (obs): nothing needed in the web container; it has no OTLP exporter.
- If the web app gains a route that must return 404 for unknown paths, change the `location /` block; the `/api/` block is independent of it.
- The Node reference in `apps/web/Dockerfile` and `NODE_IMAGE` in `mk/web.mk` are the same string kept in two places. Change both together (V8).
