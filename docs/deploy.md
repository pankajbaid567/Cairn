# Deploy runbook: Cairn on free tiers

This runbook takes the user from nothing to a running hosted Cairn: a Neon free PostgreSQL 18 project, one Render free web service running `cairn-combined`, and a Cloudflare Pages project that serves the web build and forwards `/api/*` to Render. It also covers day-to-day operation, password rotation, teardown, removing the old local volume and troubleshooting.

Every step that only you can do is marked **Needs user** and carries its id, FU1 to FU9. Agents and CI never create accounts, enter secrets, connect to Neon, Render or Cloudflare, or deploy. Nothing in this file is run by `make gates`.

Facts about the platforms are dated. The free-tier numbers below were gathered on 30 Oct 2026 and re-read on 2 and 3 Nov 2026; the evidence is in [FV0](evidence/free-tier/verify/FV0-platform-facts.md). Free plans change without notice, so check the linked pages before you rely on a number.

## Contents

- [Steps at a glance](#steps-at-a-glance)
- [Topology](#topology)
- [Limits and sources](#limits-and-sources)
- [Prerequisites](#prerequisites)
- [FU1: Neon project and `.env.neon`](#fu1-neon-project-and-envneon-needs-user)
- [FU2: bootstrap Neon from the laptop](#fu2-bootstrap-neon-from-the-laptop-needs-user)
- [FU3: Cloudflare account, account id and a Workers AI token](#fu3-cloudflare-account-account-id-and-a-workers-ai-token-needs-user)
- [FU4: Render Blueprint and environment entries](#fu4-render-blueprint-and-environment-entries-needs-user)
- [FU5: first Render deploy](#fu5-first-render-deploy-needs-user)
- [FU6: Pages project, proxy secret and deploy](#fu6-pages-project-proxy-secret-and-deploy-needs-user)
- [End-to-end check](#end-to-end-check)
- [Operations](#operations)
- [FU7: remove the old local volume](#fu7-remove-the-old-local-volume-needs-user)
- [FU8: optional live Workers AI check](#fu8-optional-live-workers-ai-check-needs-user)
- [FU9: keep the plan up to date](#fu9-keep-the-plan-up-to-date-needs-user)
- [Teardown](#teardown)
- [Troubleshooting](#troubleshooting)

## Steps at a glance

| Id | Step | Status | Plan item |
|---|---|---|---|
| FU1 | Neon account, PostgreSQL 18 project, `make env-neon`, fill two keys in `.env.neon` | Needs user | 0.46 |
| FU2 | `make remote-init` and `make remote-status` from the laptop | Needs user | 0.47 |
| FU3 | Cloudflare account, account id, API token scoped to Workers AI | Needs user | 0.48 |
| FU4 | Render Blueprint from `render.yaml` and its six dashboard entries (after the repository is pushed, Phase 0 step U4) | Needs user | 0.49 |
| FU5 | First Render deploy, `/healthz`, 20-minute idle check | Needs user | 0.50 |
| FU6 | Pages project, `API_ORIGIN`, `make web-dist`, `CAIRN_PROXY_TOKEN` Pages secret, pinned wrangler deploy | Needs user | 0.51 |
| FU7 | `docker volume rm cairn_pgdata` (the old Phase 0 local volume) | Needs user | 0.52 |
| FU8 | Optional: `make workers-ai-live-check` with a Workers AI token in `.env` | Needs user | 0.53 |
| FU9 | Tick plan items and add progress-log rows | Needs user | 0.54 |

The order is FU1, FU2, FU3, FU4, FU5, FU6. FU3 can come before FU1 and FU2; FU7, FU8 and FU9 are independent of the hosted deploy.

## Topology

```text
browser
   |  https://<project>.pages.dev            (Cloudflare Pages: static web build, free, unlimited static requests)
   |      /api/*  ->  Pages Function         (adds X-Cairn-Proxy-Token, strips /api, 90 s timeout)
   v
https://<service>.onrender.com               (Render free web service: cairn-combined, 512 MB, 0.1 CPU)
   |      FastAPI app + DBOS jobs in one process, singapore region
   v  TLS verify-full, direct endpoint
Neon free project, aws-ap-southeast-1         (PostgreSQL 18, pgvector, databases cairn and cairn_dbos)

Cloudflare Workers AI (REST)                 (embeddings, rerank, answer model; called by the app in Phase 1)
```

| Piece | Where its definition lives |
|---|---|
| Render service | [render.yaml](../render.yaml) and [infra/docker/python-app.Dockerfile](../infra/docker/python-app.Dockerfile) |
| Pages configuration | [apps/web/wrangler.toml](../apps/web/wrangler.toml), [_routes.json](../apps/web/public/_routes.json), [_headers](../apps/web/public/_headers) |
| `/api` proxy | [functions/_lib/proxy.ts](../apps/web/functions/_lib/proxy.ts) |
| Neon bootstrap | [.env.neon.example](../.env.neon.example) and [mk/deploy.mk](../mk/deploy.mk) |
| Local emulation of this topology | the `freetier` compose profile, [mk/freetier.mk](../mk/freetier.mk) |

Why it is shaped this way:

- The owner and admin credentials (`CAIRN_OWNER_DB_PASSWORD`, `neondb_owner`) stay on your laptop in `.env.neon`. Render receives only the `cairn_app` and `cairn_dbos` passwords, the proxy token and the Cloudflare pair. Every runtime connection uses a role without BYPASSRLS.
- The `onrender.com` URL is public. The API answers 404 `not_found` to every request that lacks the right `X-Cairn-Proxy-Token`, except `/healthz`, so Render can probe liveness. Only the Pages Function holds the token.
- `/healthz` checks the process only. `/readyz` checks the database, extensions, role, schema version and, in this process, that DBOS has launched. A Neon outage therefore shows in `/readyz` and never makes Render restart the service.
- There is no keep-alive pinger. See [Sleep and wake](#sleep-and-wake) and [Neon compute hours](#neon-compute-hours).

## Limits and sources

Paraphrased; the link is the authority. "Date" is when the fact was last read.

| Service | Limit | Date | Source |
|---|---|---|---|
| Render web service (free) | 512 MB RAM, 0.1 CPU | 30 Oct 2026 | [Render article](https://render.com/articles/from-side-project-to-production-scaling-your-first-app) |
| Render | Spins down after 15 minutes without inbound HTTP or WebSocket traffic; the next request wakes it in about one minute; local files are lost on spin-down | 2 Nov 2026 | [Render: Deploy for Free](https://render.com/docs/free) |
| Render | 750 free instance hours per workspace per month; a spun-down service uses none; at zero, free services are suspended until next month. Free services can be restarted at any time, have no shell, and may be suspended for unusually heavy outbound traffic | 2 Nov 2026 | [Render: Deploy for Free](https://render.com/docs/free) |
| Render | A background worker or cron job has no free type, which is why the worker runs inside the web service | 2 Nov 2026 | [Render: Deploy for Free](https://render.com/docs/free) |
| Neon (free) | No time limit, no card. Per project and month: 100 CU-hours of compute, 5 GB of public network transfer. 1 GB of storage per project | 2 Nov 2026 | [Neon: Free plan limits and quotas](https://neon.com/faqs/free-plan-limits-and-quotas) |
| Neon | Compute scales to zero after 5 minutes idle and cannot be switched off on the free plan; resume takes a few hundred milliseconds | 2 Nov 2026 | [Neon: Free plan limits and quotas](https://neon.com/faqs/free-plan-limits-and-quotas) |
| Neon | When CU-hours or transfer run out, compute is suspended until the next billing period and connections drop. Over 1 GB, writes that grow storage fail. No data is deleted | 2 Nov 2026 | [Neon: Free plan limits and quotas](https://neon.com/faqs/free-plan-limits-and-quotas) |
| Neon | 100 CU-hours cover a 0.25 CU compute for about 400 hours a month | 2 Nov 2026 | [Neon: Free plan limits and quotas](https://neon.com/faqs/free-plan-limits-and-quotas) |
| Neon | PostgreSQL 18 is generally available and offered on free projects; the region is fixed at creation | 2 Nov 2026 | [Neon: Postgres version support](https://neon.com/docs/postgresql/postgres-version-policy), [Neon: Regions](https://neon.com/docs/introduction/regions) |
| Neon | Roles created with SQL do not join `neon_superuser`; `neondb_owner` can create roles and databases but is not a superuser; passwords must be sent in plain text (hashed passwords are refused) and need about 60 bits of entropy | 2 Nov 2026 | [Neon: Manage roles](https://neon.com/docs/manage/roles), [Neon: Manage database access](https://neon.com/docs/manage/database-access) |
| Cloudflare Pages | Requests to static assets are free and unlimited; 500 builds a month (not used with Direct Upload); 20,000 files and 25 MiB per file | 30 Oct 2026 | [Pages Functions pricing](https://developers.cloudflare.com/pages/functions/pricing/), [Pages limits](https://developers.cloudflare.com/pages/platform/limits/) |
| Pages Functions | Count against the Workers Free quota: 100,000 requests a day (reset at 00:00 UTC), 10 ms CPU per request (waiting on `fetch` does not count), 128 MB memory, 50 subrequests | 3 Nov 2026 | [Workers limits](https://developers.cloudflare.com/workers/platform/limits/) |
| Workers AI | 10,000 neurons a day free for the account, reset at 00:00 UTC; above that needs Workers Paid. `kimi-k2.6`, `kimi-k2.7-code` and `glm-5.2` need Workers Paid and return HTTP 403 (code 5035) on Free | 3 Nov 2026 | [Workers AI pricing](https://developers.cloudflare.com/workers-ai/platform/pricing/), [changelog, 28 Jul 2026](https://developers.cloudflare.com/changelog/post/2026-07-28-models-require-workers-paid/) |

Not used, and why: Cloudflare Containers have no free allowance. Supabase free pauses a project after about a week of low activity. Oracle Always Free A1 was halved to 2 OCPU and 12 GB in June 2026 and idle instances can be reclaimed. Render's free Postgres expires after 30 days. Details and links are in [FV0](evidence/free-tier/verify/FV0-platform-facts.md).

## Prerequisites

- The repository is pushed to a Git host that Render can read (Phase 0 step U4, in [CONTRIBUTING.md](../CONTRIBUTING.md)). FU4 needs it. FU1 to FU3 do not.
- Docker running on your laptop. `make remote-init` runs `cairn-db` in the `cairn-python:local` image and builds it when it is absent.
- Node 22 or newer and `pnpm` on the laptop, for the pinned wrangler (FU6). Node 24 matches the project's pin.
- Neon's free plan needs no card ([FV0](evidence/free-tier/verify/FV0-platform-facts.md)); check Render and Cloudflare at sign-up. Do not upgrade any plan to make a step work; the free limits are part of the design.
- Keep `.env.neon` and every token out of the repository. `.env.neon` is ignored by `.gitignore`. Do not paste a password or token into a chat, a ticket or a shell command line; this runbook never asks you to.

## FU1: Neon project and `.env.neon` (Needs user)

**Needs user.** Plan item 0.46. Creates an account and a project, so an agent cannot do it.

1. Create a Neon account and a project on the free plan. Choose **PostgreSQL 18** and the region **AWS Asia Pacific (Singapore), `aws-ap-southeast-1`**, next to Render's `singapore` region. Both are fixed after creation. If the free plan offers a different region only, stop and tell the maintainer before you continue; the Render region in [render.yaml](../render.yaml) would change too.
2. Note the default database name (`neondb`) and role (`neondb_owner`). In the connection details choose the **direct** connection (host without `-pooler`) and show the password for `neondb_owner`. Do not use the pooled host: DBOS listens for notifications, which a transaction-mode pooler breaks.
3. In the repository run:

   ```sh
   make env-neon
   ```

   It writes `.env.neon` from [.env.neon.example](../.env.neon.example), with a fresh random value for each `change-me` key and empty values for the two `user-provided` keys. It refuses to overwrite an existing `.env.neon`.
4. Open `.env.neon` in an editor and fill the two empty keys:
   - `CAIRN_DB_HOST`: the direct endpoint host, for example `ep-<name>-<id>.ap-southeast-1.aws.neon.tech`. The part before `.aws.neon.tech` confirms the region.
   - `CAIRN_DB_ADMIN_PASSWORD`: the `neondb_owner` password.
5. Leave `CAIRN_DB_SSLMODE=verify-full` and `CAIRN_DB_SSLROOTCERT=/etc/ssl/certs/ca-certificates.crt` as they are. The `system` value does not load any roots in the runtime image ([FV11](evidence/free-tier/verify/FV11.md)).

Done when `.env.neon` has no empty value. `make remote-init` checks this before it opens a connection and names the empty keys (never their values).

## FU2: bootstrap Neon from the laptop (Needs user)

**Needs user.** Plan item 0.47. Opens a connection to Neon with your admin password.

```sh
make remote-init
make remote-status
```

`remote-init` runs `cairn-db init --allow-plaintext-password` in a container with `--env-file .env.neon`. It creates the roles `cairn_owner`, `cairn_app` and `cairn_dbos` (none with SUPERUSER, REPLICATION or BYPASSRLS), the databases `cairn` and `cairn_dbos`, the extensions `vector` and `btree_gist`, and applies migrations 0001 and 0002. The flag is needed because Neon accepts no pre-hashed password: the three role passwords travel as plain text inside the TLS connection, which `verify-full` protects. The tool refuses the flag (exit 2, before it connects) unless `CAIRN_DB_SSLMODE=verify-full`. It logs no statement and no password. It is safe to run again: existing roles and databases are kept, passwords are set again, and applied migrations are skipped.

`remote-status` is read only and prints the applied and pending migrations.

What to check:

- `remote-status` shows migrations 0001 and 0002 applied, none pending, and a server version of 18.x.
- If `remote-init` stops with a message about an attribute or a grant, copy the message (it names roles and attributes, never passwords) to the maintainer. Three Neon behaviors were taken from the documentation and are confirmed here for the first time: that `neondb_owner` may create the roles and grant itself `SET` on them, that it may create the extensions, and that Neon accepts the generated passwords ([FV3](evidence/free-tier/verify/FV3.md)). A password rejected as too weak is fixed by editing the value in `.env.neon` and running `remote-init` again.

Keep `.env.neon`. You need it for FU4 and for password rotation.

## FU3: Cloudflare account, account id and a Workers AI token (Needs user)

**Needs user.** Plan item 0.48.

1. Create a Cloudflare account on the free plan (no card is needed).
2. Note the **account id** (32 hexadecimal characters) from the dashboard.
3. Create an API token with only the Workers AI permissions (Read and Edit), restricted to your account ([FV8](evidence/free-tier/verify/FV8.md)). Copy it once; Cloudflare does not show it again. Store both values in a password manager, not in the repository.
4. Do not give this token Pages or any other permission. Deploying Pages uses a separate login (FU6).

The app does not call Workers AI in Phase 0. The token is stored in Render now so that Phase 1 needs no further deploy step.

## FU4: Render Blueprint and environment entries (Needs user)

**Needs user.** Plan item 0.49. Requires the repository to be pushed (U4).

1. In the Render dashboard create a **Blueprint** from the repository. Render reads [render.yaml](../render.yaml): one web service `cairn-api`, Docker runtime, free plan, region `singapore`, health check `/healthz`, start command `cairn-combined`, automatic deploys off. Dashboard labels change from time to time; look for "New" and "Blueprint".
2. Render asks for the values marked `sync: false`. Enter:

   | Key | Value comes from |
   |---|---|
   | `CAIRN_DB_HOST` | `.env.neon`, the direct endpoint host (no `-pooler`) |
   | `CAIRN_APP_DB_PASSWORD` | `.env.neon`, the key of the same name |
   | `CAIRN_DBOS_DB_PASSWORD` | `.env.neon`, the key of the same name |
   | `CAIRN_PROXY_TOKEN` | A new secret of at least 32 characters, for example the output of `openssl rand -hex 32`. Keep a copy: FU6 needs the same value |
   | `CLOUDFLARE_ACCOUNT_ID` | FU3 |
   | `CLOUDFLARE_API_TOKEN` | FU3 |

3. Never enter `CAIRN_OWNER_DB_PASSWORD` or `CAIRN_DB_ADMIN_PASSWORD` in Render. The service does not need them, and the Blueprint test fails if the file ever names such a key.
4. A `sync: false` key that is **added to `render.yaml` after the Blueprint was first created** is not asked for again. Render ignores those keys on later Blueprint updates ([FV6](evidence/free-tier/verify/FV6.md)), so type the value into the service's Environment page by hand.
5. Do not set `PORT` or `PACKAGE`. Render supplies `PORT` (default 10000), and the Dockerfile defaults to `cairn-combined`.

Done when the service exists with the six entries. If the dashboard shows `singapore` as unavailable for the free plan, stop and tell the maintainer.

## FU5: first Render deploy (Needs user)

**Needs user.** Plan item 0.50.

1. Trigger the first deploy from the dashboard (Blueprint creation normally starts one; later commits need **Manual Deploy**, because `autoDeployTrigger` is `off`). The first build compiles the image on the free instance and can take several minutes.
2. When the service is live, copy its URL, `https://<service>.onrender.com`. You need it in FU6.
3. Check liveness:

   ```sh
   curl -sS https://<service>.onrender.com/healthz
   ```

   Expected: `{"status":"ok"}`. If the service was asleep, the first answer takes about a minute.
4. Check that the token gate works from outside:

   ```sh
   curl -sS -i https://<service>.onrender.com/readyz
   ```

   Expected: HTTP 404 with `{"error":"not_found"}` and `Cache-Control: no-store`. A direct request without the proxy token must learn nothing. `/readyz` becomes reachable through Pages after FU6.
5. **Idle check (open question).** Render's documentation does not say whether its own health-check probes count as inbound traffic for the 15-minute sleep ([FV6](evidence/free-tier/verify/FV6.md)). Leave the service alone for about 20 minutes, then look at the dashboard. Note whether it shows as spun down. If it never sleeps, the always-on case in [Neon compute hours](#neon-compute-hours) applies and the Neon allowance lasts about 17 days a month. In that case ask the maintainer about dropping `healthCheckPath`, which makes Render use a TCP check instead.

## FU6: Pages project, proxy secret and deploy (Needs user)

**Needs user.** Plan item 0.51. The order matters: Cloudflare reads a Pages secret only for deployments made after it is set ([FV7](evidence/free-tier/verify/FV7.md)), so create the project, set the secret, then deploy.

All wrangler commands run from `apps/web`, where [wrangler.toml](../apps/web/wrangler.toml) and the `functions/` folder live, and use the pinned version **4.156.2** through `pnpm dlx`. The pin is not added to the web `package.json`, so the web lockfile and the audit gate stay unchanged.

1. Set `API_ORIGIN` in [apps/web/wrangler.toml](../apps/web/wrangler.toml) to the Render URL from FU5: `https://<service>.onrender.com`, with no path and no trailing slash. The file holds no secret.
2. Build the static files:

   ```sh
   make web-dist
   ```

   It builds `apps/web/dist` in the pinned Node container. The folder then holds `index.html`, `assets/`, `_routes.json` and `_headers`.
3. Log in to Cloudflare for Pages. This opens a browser and stores a session for wrangler; it does not use the Workers AI token:

   ```sh
   cd apps/web
   pnpm dlx wrangler@4.156.2 login
   ```

4. Create the empty project, with the name used in `wrangler.toml`:

   ```sh
   pnpm dlx wrangler@4.156.2 pages project create cairn --production-branch main
   ```

5. Set the proxy token. wrangler asks for the value on a hidden prompt. Paste the same value you gave Render for `CAIRN_PROXY_TOKEN` in FU4. Do not put it on the command line:

   ```sh
   pnpm dlx wrangler@4.156.2 pages secret put CAIRN_PROXY_TOKEN --project-name cairn
   ```

6. Deploy (Direct Upload). Do not use drag and drop in the dashboard: it does not compile the `functions/` folder.

   ```sh
   pnpm dlx wrangler@4.156.2 pages deploy dist
   ```

7. Do not create `apps/web/.dev.vars`. It holds local secrets for `wrangler pages dev`, `.gitignore` does not cover it, and `tests/scripts/test_pages_config.py` fails if it exists.
8. In the Pages project's settings, under Runtime, leave the behavior when the daily Function allowance is used up on **fail closed**. Then an exhausted allowance shows an error page and the cause is visible. Either choice keeps the API unreachable without the token.

What to confirm, because only a real deploy can: that `wrangler pages deploy dist` keeps the hand-written `_routes.json` and picks up `functions/`, and how long the first request after a Render sleep takes (the proxy waits up to 90 seconds; Render needs about one minute).

If the project name `cairn` is taken, Cloudflare tells you; pick another name, change `name` in `wrangler.toml` and use it in every command above. The site is then at `https://<project>.pages.dev`.

## End-to-end check

```sh
curl -sS https://<project>.pages.dev/api/readyz
```

Expected after FU2 and FU6: HTTP 200 and a body like

```json
{"status":"ready","checks":{"database":{"ok":true},"postgres":{"ok":true,"detail":{"version":"18.6"}},"extensions":{"ok":true},"role":{"ok":true,"detail":{"name":"cairn_app"}},"schema":{"ok":true,"detail":{"version":2,"expected":2}},"jobs":{"ok":true}},"extensions":{"vector":"0.8.6","btree_gist":"1.8"}}
```

The values for `version`, latency and the extension versions can differ. The first request after a sleep takes about a minute. If it times out the Function answers 503 `api_unavailable` with `Retry-After: 30`; retry once. The page `https://<project>.pages.dev/` itself is a static file and never wakes Render.

The role name `cairn_app` and `"jobs":{"ok":true}` show the three things this design cares about: the runtime role has no BYPASSRLS, DBOS has launched, and the schema is at the version the code expects. See [Troubleshooting](#troubleshooting) for each failing code.

## Live sync, seed and reset (Phase 3 and 6)

* **Live sync.** Create the GitHub App, the `cairn_edge` role and the Pages secrets as in [sync.md](sync.md#user-run-steps-need-your-github-cloudflare-and-neon-accounts). On Render set `CAIRN_GITHUB_ORG`, `CAIRN_GITHUB_APP_ID`, `CAIRN_GITHUB_INSTALLATION_ID` and `CAIRN_GITHUB_PRIVATE_KEY`; the webhook secret and the `cairn_edge` password stay with the Pages project and your laptop.
* **Seed (no neurons).** `make seed-dump` writes `var/seed/kestrel.seed.gz` from a seeded local database (items, revisions, chunks **with their embeddings**, ACLs, principals and decisions, about 20 KB). `make seed-load` loads it into an empty content database as `cairn_owner`, and `make seed-reset` truncates the content tables first so every demo starts from the same corpus. Against Neon, run it from your laptop with `cairn-eval seed-dump load --allow-remote` after `make remote-init`; it refuses when the dump plus the database would pass 512 MB. It never touches the audit log.
* **Agents.** `cairn-agent create --for <user> --name <name> --allow-remote` mints an MCP token (see [mcp.md](mcp.md)).

## Free-plan cost check (checked 2026-11-04)

| Service | Plan used | What keeps it free | Checked by |
|---|---|---|---|
| Render web service | `free` (512 MB, 0.1 CPU) | one service, no disk, no cron job, no worker, no add-on | `tests/scripts/test_free_plan.py` reads `render.yaml` |
| Neon | free project | auto-suspend on; 0.5 GB data target; no keep-alive | [Neon compute hours](#neon-compute-hours) |
| Cloudflare Pages and Functions | free plan | Functions only on `/api/*` and `/webhooks/*`; the webhook Function does one HTTP insert and one wake request | `wrangler.toml`, `public/_routes.json` |
| Workers AI | daily free allocation | 9,000-neuron cap per UTC day (5,000 on Render) enforced by the budget guard before every call | `NeuronBudget`, [Neuron budget](#neuron-budget) |
| GitHub | free organisation | public demo repos, or private ones within the free plan | user |

**No keep-alive pinger or uptime monitor is used** (ADR-0014): one would keep Render and Neon awake all month and spend about 186 of Neon's 100 CU-hours. The cold start (about a minute) is accepted, documented in the README, and handled by the web's waking state. `tests/scripts/test_free_plan.py` also fails if a workflow ever calls the deployed URL.

## Operations

### Sleep and wake

- Render spins the service down after 15 minutes without inbound traffic and wakes it on the next request, in about one minute. Memory and local files are lost; nothing needs them, because all state is in Neon.
- The Pages Function waits up to 90 seconds for the wake, then answers 503 `api_unavailable`. Static pages load at once because Pages serves them.
- Render may also restart the service at any time. On a restart the jobs runner relaunches DBOS with bounded backoff, and `/healthz` stays up meanwhile.
- A deploy or restart sends SIGTERM. The process drains within 20 seconds, inside Render's default 30 seconds.
- There is no keep-alive pinger, on purpose: it would keep Render and Neon awake all month and use up the Neon allowance (next section).
- Whether Render's health probes or stray requests to the public URL keep the service awake is not documented. FU5 measures the health-probe case. Any inbound HTTP request, including one answered with 404, probably counts; this has not been tested.

### Heartbeat gaps

The demo heartbeat is a DBOS schedule with the cron `0 */5 * * * *`, so it fires about every five minutes while the service is awake. `automatic_backfill` is off, so after a sleep nothing replays the missed ticks. Gaps in `worker.heartbeat` log lines and in the DBOS workflow table that match the sleep periods are expected and are not an outage.

### Neon compute hours

The allowance is 100 CU-hours a month for the project. Compute time counts while the compute is awake, and it stays awake while anything keeps a connection busy.

DBOS polls its database about 9.5 times a second whenever it is launched, even with no traffic ([FV5](evidence/free-tier/verify/FV5.md), measured on the local database). So **while the Render service is awake, Neon is awake too**. Neon sleeps only after Render has slept (15 minutes without inbound traffic) and 5 more minutes have passed. This means Neon compute hours follow Render's awake hours, not the traffic of the app. The heartbeat cron itself costs nothing; the polling is what costs.

Estimate (Estimate, not measured on Neon): assume the compute stays at 0.25 CU, the figure behind Neon's own "about 400 hours" statement. Autoscaling can go up to 2 CU, which would use the allowance several times faster; check the compute size in the Neon console.

| Pattern | Neon awake time | CU-hours at 0.25 CU | Against 100 |
|---|---|---|---|
| One 10-minute visit | about 31 minutes (10 visit, 15 until Render sleeps, 5 more, about 1 wake) | about 0.13 | about 775 visits a month fit |
| 5 such visits a day, 30 days | about 78 hours | about 19 | within the allowance |
| Render never sleeps (a pinger, or probes or bots that keep it awake), 31 days | 744 hours | about 186 | the allowance ends after about 400 hours, around day 17; compute is then suspended until the next billing period |

When the allowance runs out, Neon suspends the compute and connections drop. `/healthz` still answers, `/readyz` returns 503 with `db_unreachable` (or `db_timeout`), and the jobs runner keeps retrying. No data is deleted. Render's 750 instance hours are not the tighter limit: 744 hours of a 31-day month fit.

Watch the usage page in the Neon console after the first week. If the budget is tight in Phase 1, the options are DBOS's polling intervals or fewer DBOS features in the always-on process; neither is changed here.

### Neuron budget

Workers AI is free up to 10,000 neurons a day for the whole Cloudflare account, reset at 00:00 UTC. The app reserves an estimate before every call and refuses the call when the day's total would pass `CAIRN_WORKERS_AI_DAILY_NEURONS` (9,000 in `render.yaml`, 90% of the allocation). Reservations are stored in `cairn_meta.provider_usage` in Neon (`CAIRN_WORKERS_AI_BUDGET_STORE=postgres`), so a Render sleep does not reset the count. The code is in `packages/retrieval` and is not called by the Phase 0 app.

Estimates for the default models (neurons per million tokens: bge-small embeddings 1,841 in; bge-reranker-base 283 in; gemma-4-26b-a4b-it 9,091 in and 27,273 out; glm-4.7-flash 5,500 in and 36,400 out; [FV9](evidence/free-tier/verify/FV9.md)):

| Use | Neurons |
|---|---|
| Embed the whole demo corpus once (about 120,000 tokens) | about 221 |
| One answer (query embedding, rerank 30 passages, answer model with 2,500 tokens in and 400 out) | about 37 |
| One judged evaluation answer | about 61 |
| The three planned uses together (evaluation runs, 50 demo answers, five full re-embeds) | about 6,650 of 9,000 |

Two points to keep in mind:

- The answer and judge models are reasoning models. The guard counts their reasoning tokens as output tokens and sends the same bound as `max_completion_tokens`. Whether Cloudflare reports them inside `completion_tokens` has not been checked on a live response ([FV9](evidence/free-tier/verify/FV9.md)); the first live call in Phase 1 does. Until then the guard errs on the safe side.
- A big model-comparison run can pass one day's allowance. Spread it over several days or use the fakes first.

When a call is refused, the app reports `budget_exhausted`. Cloudflare's own daily error (code 3036) reports `daily_limit`. Neither is retried. Wait for 00:00 UTC.

### Password rotation

Rotate when a value was exposed or on a schedule you choose. Each case below lists everything that must change.

**`cairn_app` or `cairn_dbos` password** (the two that Render holds):

1. Edit `CAIRN_APP_DB_PASSWORD` or `CAIRN_DBOS_DB_PASSWORD` in `.env.neon` to a new strong value (for example `openssl rand -base64 24`). Keep the key name; change only the value.
2. Run `make remote-init`. It sets the role passwords again and skips applied migrations. Run `make remote-status` to check.
3. Enter the same new value in the Render service's Environment page and save. Render redeploys the service with it; if it does not, use Manual Deploy.
4. Check `/api/readyz` through Pages (see [End-to-end check](#end-to-end-check)).

Between steps 2 and 3 the old Render value is wrong. New connections fail, `/readyz` returns 503 and the jobs runner retries until step 3 is done. Do the two steps back to back.

**`neondb_owner` password:** change it in the Neon console, then put the new value into `CAIRN_DB_ADMIN_PASSWORD` in `.env.neon`. Render never has it, so nothing else changes.

**`CAIRN_OWNER_DB_PASSWORD`:** edit it in `.env.neon` and run `make remote-init`. Render never has it.

**Proxy token:** pick a new value, set it in Render (`CAIRN_PROXY_TOKEN`), set it in Pages with `pnpm dlx wrangler@4.156.2 pages secret put CAIRN_PROXY_TOKEN --project-name cairn` from `apps/web`, then run the deploy command again so the new secret takes effect. Until both match, `/api/*` answers 404 or 503.

**Cloudflare token:** roll or recreate it in the Cloudflare dashboard, then update `CLOUDFLARE_API_TOKEN` in Render.

If a value was ever printed or pasted somewhere it should not be, rotate it at once, then treat the old value as public.

## FU7: remove the old local volume (Needs user)

**Needs user.** Plan item 0.52. This is the only step that deletes data, and nothing in the repository does it for you.

Phase 0 ran locally on a different database image and stored its data in the Docker volume `cairn_pgdata`. The free-tier retarget switched the local `db` service to the `pgvector/pgvector` image with a new volume, `cairn_pgdata-pgvector`. The old volume cannot be mounted by the new image (its configuration preloads an extension that the plain image does not have), so it has sat unused since. Remove it when you are satisfied with the new store:

```sh
docker ps -a --filter volume=cairn_pgdata
docker volume rm cairn_pgdata
```

The first command must print no container. If it lists one, that container still uses the volume; stop and ask. The data in the old volume is Phase 0 test and spike data that `make db-init` and the fixtures can recreate. Removing it cannot be undone.

Do not use `make db-reset` for this. It targets only `cairn_pgdata-pgvector` and refuses any other volume name. Do not remove `cairn_pgdata-pgvector`: it is the current local database. Volumes of other Docker projects on your machine are not touched by this step.

## FU8: optional live Workers AI check (Needs user)

**Needs user.** Plan item 0.53. Optional; skip it if you do not want to spend neurons or put a token on your laptop.

1. Put `CLOUDFLARE_ACCOUNT_ID` and `CLOUDFLARE_API_TOKEN` (from FU3) into the local `.env`. Edit the file by hand; never regenerate it.
2. Run:

   ```sh
   make workers-ai-live-check
   ```

   The target lives in `mk/providers.mk` (task 8.13). It runs one tiny embedding, one rerank and one short text call against the real service, with `CAIRN_LIVE_CHECK=1` set by the target itself. The tests skip with a message unless both that variable and the token are set. A run uses a few neurons at most (estimate) from the 10,000 a day.
3. The target is never part of `make gates` or the CI workflow, and agents never run it.

A pass confirms the request shapes against the real service. It also shows the open points in [FV8](evidence/free-tier/verify/FV8.md) (whether a 429 carries `Retry-After`) and [FV9](evidence/free-tier/verify/FV9.md) (reasoning-token counting) only if you read the output closely; it does not settle them by itself.

## FU9: keep the plan up to date (Needs user)

**Needs user.** Plan item 0.54. Ticking items in [development_plan.md](../development_plan.md) and adding progress-log rows stays with the project owner each working day. Agents write evidence under `docs/evidence/` and never edit the checkboxes. After FU1 to FU8, tick items 0.46 to 0.53 and link your notes.

## Teardown

Do these in this order. None of the steps touches your local Docker volumes.

1. **Pages:** delete the project in the Cloudflare dashboard (Workers & Pages, the project's Settings, General, Delete project).
2. **Render:** delete the `cairn-api` web service (or the Blueprint) in the Render dashboard. Deleting stops the service and removes its environment entries.
3. **Cloudflare token:** revoke the Workers AI API token in the dashboard.
4. **Neon:** delete the project in the Neon console. This deletes the databases `cairn` and `cairn_dbos` and all roles. It cannot be undone. Export anything you need first (for example with `pg_dump` over the direct endpoint, using `CAIRN_APP_DB_PASSWORD`).
5. **Laptop:** delete `.env.neon` (it holds the owner and admin credentials). Optionally remove the helper image: `docker rmi cairn-python:local`.

To keep the project but stop spending free allowances, deleting only the Render service is enough: Neon then sleeps within five minutes and Workers AI is not called.

To rebuild after a teardown, start again at FU1. A new Neon project gets a new host, so `.env.neon`, Render and the passwords must all be redone.

## Troubleshooting

| Symptom | Cause | What to do |
|---|---|---|
| `make remote-init` or `remote-status` stops with "not found" or "empty values for: …" before doing anything | `.env.neon` is missing or a `user-provided` key is empty | `make env-neon`, then fill `CAIRN_DB_HOST` and `CAIRN_DB_ADMIN_PASSWORD` (FU1). No connection was opened |
| `make env-neon` says the file already exists | It never overwrites | Edit `.env.neon` by hand, or delete it if you want new random secrets (you must then fill the two keys again and re-run `remote-init`, which resets the three role passwords) |
| `remote-init` exits 2 and mentions `--allow-plaintext-password` or `verify-full` | `CAIRN_DB_SSLMODE` in `.env.neon` is not `verify-full` | Set `CAIRN_DB_SSLMODE=verify-full`; keep the root certificate path from the template |
| `remote-init` fails with a certificate error | `CAIRN_DB_SSLROOTCERT` changed, or the host is wrong | Restore `/etc/ssl/certs/ca-certificates.crt`; check that `CAIRN_DB_HOST` is the Neon endpoint host |
| `remote-init` fails to connect or times out | Neon compute was waking, or the host is the pooled one | Run it again; use the direct host (no `-pooler`) |
| Neon refuses a password as too weak | Neon requires about 60 bits of entropy | Put a longer random value in the key, run `remote-init` again |
| `remote-init` reports a forbidden attribute on `cairn_owner`, `cairn_app` or `cairn_dbos` | A role has SUPERUSER, REPLICATION or BYPASSRLS, for example because it was created in the Neon console | In a fresh project, drop the role with SQL as `neondb_owner` and run `remote-init` again, or recreate the project. Never create these roles through the console, CLI or API (roles made there join `neon_superuser`) |
| `cairn-db migrate` or `remote-init` reports `ChecksumMismatch` | The database recorded an older `0001_baseline.sql` (only the retired Phase 0 local volume does) | Local: `make db-reset` (it deletes the local `cairn_pgdata-pgvector` data) and `make db-init`. A fresh Neon project is not affected |
| `/api/…` answers 503 `{"error":"proxy_not_configured"}` | `API_ORIGIN` or the `CAIRN_PROXY_TOKEN` Pages secret is missing | Check `wrangler.toml`; run `secret put`, then deploy again (a secret reaches only later deployments) |
| `/api/…` answers 503 `{"error":"api_unavailable"}` with `Retry-After: 30` | The Render service is waking (about one minute), is down, or took more than 90 seconds | Wait and retry; look at the Render logs |
| `/api/…` answers 404 `{"error":"not_found"}` | The Pages secret and the Render `CAIRN_PROXY_TOKEN` differ, or the path does not exist | Set both to the same value; deploy Pages again |
| `https://<service>.onrender.com/readyz` answers 404 | Working as designed: no proxy token | Use the Pages URL. `/healthz` is the only open path |
| `/readyz` 503 `jobs_not_launched` | DBOS cannot launch: wrong `CAIRN_DBOS_DB_PASSWORD`, database `cairn_dbos` missing, or Neon unreachable | Check the Render logs (the attempt count grows); compare the password with `.env.neon`; run `make remote-status` |
| `/readyz` 503 `db_unreachable` or `db_timeout` | Neon is waking, its allowance is used up, or the host or password is wrong | Wait a minute; check the Neon console usage page; check `CAIRN_DB_HOST` and `CAIRN_APP_DB_PASSWORD` in Render |
| `/readyz` 503 `schema_behind` | Migrations are older than the code | `make remote-init` |
| `/readyz` 503 `extensions_missing` or `extensions_outdated` | `vector` or `btree_gist` is missing, or `vector` is older than 0.8.0 | `make remote-init`; check the version in the Neon console. Tell the maintainer if Neon offers an older pgvector |
| `/readyz` 503 `role_unsafe` | The runtime role has SUPERUSER or BYPASSRLS | The app connects as `cairn_app` only. Check that the Neon role `cairn_app` was created by `remote-init` and not in the console; never point Render at `neondb_owner` |
| Render build fails | Build error, or the free instance ran out of build memory | Read the build log; do not set `PACKAGE` or `PORT`; a later commit may need Manual Deploy |
| Render service restarts repeatedly | The process exits at start. Settings errors exit with status 2 before the port opens (for example a `CAIRN_PROXY_TOKEN` shorter than 32 characters) | Read the log line; it names the key and never the value |
| Render service is suspended | The 750 free instance hours are used up for the month | It resumes next month; check that nothing keeps it awake |
| Cloudflare error 1027 or a Pages Function error page | The 100,000 daily Function requests are used up | Wait for 00:00 UTC. Static pages still load unless the project is set to fail closed |
| `wrangler pages deploy` does not use `functions/` | It was run from the wrong folder, or by drag and drop | Run it from `apps/web` with `dist` as the argument |
| `pnpm dlx` complains about the Node version | wrangler 4.156.2 needs Node 22 or newer | Upgrade Node (Phase 0 step U7 for Node 24) |
| A Workers AI call returns HTTP 403 (code 5035) | The model needs Workers Paid | Use a model on the free allocation; the defaults are |
| A Workers AI call returns 429 with code 3036 | The 10,000-neuron day is used up | Wait for 00:00 UTC |

For local failures (Docker memory, ports, `make gates`) see the Troubleshooting section of [CONTRIBUTING.md](../CONTRIBUTING.md). The task evidence for each deploy artifact is in [docs/evidence/free-tier/README.md](evidence/free-tier/README.md).
