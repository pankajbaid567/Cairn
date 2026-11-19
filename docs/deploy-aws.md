# Deploy runbook: Cairn on AWS (Neon database, Bedrock models)

The decision and its reasons are in [ADR-0018](adr/ADR-0018-aws-deployment-neon-database-bedrock-models.md). This file is the procedure. Everything runs in **ap-south-1**. The older free-tier runbook ([deploy.md](deploy.md)) still describes the Cloudflare and Render path and its Neon steps; steps AU1 and AU2 below reuse its Neon parts.

```text
browser ──HTTPS──▶ Caddy (existing EC2 t4g.small) ──▶ cairn-waker (127.0.0.1:8003)
                                                          │  serves the web build; wakes and stops the app
                                                          ▼  /api/* (token added)
                                              cairn container (127.0.0.1:8004, 512 MB)
                                                          │ TLS verify-full             │ instance role
                                                          ▼                             ▼
                                                 Neon PostgreSQL 18 (pgvector)     Amazon Bedrock (ap-south-1)
secrets: SSM Parameter Store /cairn/*   logs: CloudWatch /cairn/app   build artifacts: S3 cairn-artifacts-<account>-ap-south-1
```

## Steps

| Id | Who | Step |
|---|---|---|
| AU1 | you | Create the Neon project (PostgreSQL 18, direct endpoint) and `.env.neon` |
| AU2 | you | `make remote-init` (roles, databases, migrations, **1024-dimension embedding column**) |
| AU3 | you | `make aws-secrets` (copies four runtime settings into SSM; prints names only) |
| AA1 | agent | CloudFormation stack `cairn` (IAM policy, artifact bucket, log group) |
| AA2 | agent | Generate the session secret, audit key and proxy token in SSM (never printed) |
| AA3 | agent | Pack the source, upload to S3, build the image on the host, install the web build and the waker service, add the Caddy site |
| AA4 | agent | First start, end-to-end check, cold-start timing |

AA1 to AA3 do not need the Neon steps and can run first; AA4 needs AU1 to AU3.

## AU1: Neon project and `.env.neon`

Follow [deploy.md, FU1](deploy.md#fu1-neon-project-and-envneon-needs-user), with one difference: **region**. Pick **AWS Asia Pacific (Mumbai) `aws-ap-south-1` if the dropdown offers it**; otherwise Singapore (`aws-ap-southeast-1`). The region cannot be changed later. Use the **direct** endpoint (host without `-pooler`): DBOS listens for notifications and a pooler breaks that.

`make env-neon` writes `.env.neon` with fresh random passwords and `CAIRN_EMBED_DIM=1024`. Fill the two empty keys (`CAIRN_DB_HOST`, `CAIRN_DB_ADMIN_PASSWORD`).

## AU2: bootstrap Neon

```sh
make remote-init
make remote-status
```

`remote-init` runs `cairn-db init`: roles, databases, extensions, migrations 0001 to 0012, and then, because `.env.neon` has `CAIRN_EMBED_DIM=1024`, it changes `cairn.embeddings.embedding` to `vector(1024)` and rebuilds the HNSW index. The step is safe to repeat (it reports "already 1024") and refuses, changing nothing, if vectors of another dimension exist. Check `remote-status` shows head 12, none pending.

## AU3: secrets into AWS

```sh
make aws-secrets            # uses AWS_PROFILE, or: AWS_PROFILE=pankaj-aws make aws-secrets
```

It copies `CAIRN_DB_HOST` and the passwords of `cairn_app`, `cairn_dbos` and `cairn_ingest` into SSM Parameter Store (SecureString, `/cairn/<NAME>`). The owner, admin and edge passwords are never sent. It refuses empty values and a pooled host, and prints names only. `python scripts/aws_push_neon_secrets.py --dry-run` checks the file without calling AWS.

## AA1 to AA3: the AWS side

Resources are defined in [infra/aws/cairn.yaml](../infra/aws/cairn.yaml): a managed policy on the host's instance role (Bedrock `InvokeModel` on Titan Text Embeddings V2, `gpt-oss-120b`, `gpt-oss-20b` and `qwen3-32b` in this Region only; read of `/cairn/*` parameters; write to the log group; read of the artifact bucket), an encrypted private artifact bucket with a 14-day expiry, and the log group `/cairn/app` (14-day retention).

On the host (`/opt/cairn-src`, from the tarball made by `make aws-package`, which refuses to include an env file or a key):

- `docker build -f infra/docker/python-app.Dockerfile -t cairn-combined:aws .` builds the combined image (non-root user, read-only root filesystem at run time).
- `deploy/aws/cairn-waker.service` runs `cairn-run.sh create` first (reads the secrets from SSM into tmpfs, `docker create`s the container with its configuration), then `cairn_waker.py`.
- The static web build is in `/opt/cairn/web` and the host's Caddy proxies the site `cairn.<ip-with-dashes>.sslip.io` to `127.0.0.1:8003`.

## AA4: end-to-end check

1. `curl https://cairn.<host>/waker/health` answers `app_running: false` (nothing woken).
2. Open the page, choose a persona, search: the first API call starts the container; `docker ps` shows it, `/api/readyz` answers 200 once DBOS has launched.
3. Seed the demo corpus into Neon and check an answer cites its sources (`make seed-local` equivalents run against Neon are listed in the evidence file written by this step).
4. Watch Bedrock usage: `cairn_meta.provider_usage` (provider `bedrock`, micro-dollars) and CloudWatch Bedrock metrics.
5. Idle for 15 minutes: the container stops by itself (`docker ps`), and Neon suspends five minutes later (Neon console).
6. Time the first request after the stop (cold start) and write it into `docs/evidence/aws/`.

## Operations

| Task | How |
|---|---|
| Logs | CloudWatch log group `/cairn/app`; `journalctl -u cairn-waker` on the host |
| Wake or stop by hand | `docker start cairn` / `docker stop cairn`; the waker adapts |
| Change a model or limit | edit the block in `deploy/aws/cairn-run.sh`, then `systemctl restart cairn-waker` |
| Rotate a secret | put a new SecureString under `/cairn/<NAME>` and restart the waker; a database password also needs `ALTER ROLE` on Neon |
| Neon hours | the Neon console; with normal demo use about 19 compute-hours a month (ADR-0014); a page left open keeps the app awake |
| Spend | `CAIRN_BEDROCK_DAILY_USD` caps Bedrock per day; the account AWS Budget alerts on total spend |
| Teardown | delete the stack `cairn` (empty the artifact bucket first), `systemctl disable --now cairn-waker`, remove the Caddy site and `/opt/cairn`, delete `/cairn/*` parameters, delete the Neon project |

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `503 api_unavailable`, `Retry-After: 30` | the container did not become ready in 90 s: `docker logs cairn`, `/cairn/app` logs; Neon unreachable or its hours used up (`/readyz` fails the database check) |
| `/readyz` 503 `jobs_not_launched` | DBOS cannot reach `cairn_dbos` on Neon yet; check the direct host and the role password |
| `AccessDeniedException` from Bedrock in the logs | the model is not in the policy, or model access is not enabled for the account; the error text carries no prompt |
| `budget_exhausted` in answers | the daily Bedrock cap was reached; it resets at 00:00 UTC |
| Search returns no vectors, or `expected 1024 dimensions` | the embedding column was not widened: run `make remote-init` again with `CAIRN_EMBED_DIM=1024` in `.env.neon` |
