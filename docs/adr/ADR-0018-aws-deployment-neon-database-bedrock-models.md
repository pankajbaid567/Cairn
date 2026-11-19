---
status: accepted
date: 2026-11-19
decision-makers: Pankaj Baid
---

# ADR-0018: Deploy on AWS with Neon as the database, Amazon Bedrock for models, and an app host that sleeps when idle

Amends [ADR-0014](ADR-0014-free-tier-deployment-cloudflare-render-neon.md) (hosting) and [ADR-0015](ADR-0015-workers-ai-models-and-neuron-budget.md) (models). ADR-0013 (PostgreSQL 18, pgvector, native full-text, on Neon) is unchanged.

## Context and Problem Statement

The free-tier design ran on Cloudflare Pages, one Render free service and Workers AI, and was never deployed. The owner wants the whole system on AWS (one account, one Region, ap-south-1), with Amazon Bedrock for every model call, but wants to keep Neon as the database. What runs where, and what does that cost?

## Considered Options

* **Database:** Neon free (kept); RDS PostgreSQL 18 `db.t4g.micro`; Aurora Serverless v2; vectors in Amazon S3 Vectors.
* **Compute:** the existing EC2 host (shared); ECS Fargate; Lambda.
* **Models:** Bedrock in-Region on-demand models; Claude through an `in.*` inference profile; Bedrock rerank models; a local ONNX cross-encoder.

## Decision Outcome

* **Neon stays the database, and vectors stay in it.** Cairn's central guarantee, that an answer, snippet, count or citation never reveals what the asker cannot read, is enforced by row-level security on the same rows that hold the embeddings, and the ACL-filtered vector search was chosen from a measured spike (ADR-0008). Moving the vectors to S3 Vectors would put the permission check outside the database, so the guarantee would rest on metadata filters instead of RLS. Not done.
* **Compute is the existing EC2 host** (a `t4g.small` that also runs two other apps). The combined process measured about 100 MiB idle (`docs/evidence/free-tier/`), so it fits beside them; the incremental compute cost is zero.
* **The app container sleeps when idle.** A launched DBOS polls its database about 9.5 times a second (FV5, ADR-0014), which keeps Neon's compute awake whenever the app is. Neon's free plan gives 100 compute-hours a month, about 400 awake hours at 0.25 CU; an always-on host needs about 730 and would exhaust the plan around day 17. `deploy/aws/cairn_waker.py` is a small standard-library front door: it serves the static web build, forwards `/api/*` with the same rules as the Pages Function (token added, client token dropped, 1 MiB body limit), starts the container on the first API request, waits for `/readyz`, and stops the container after 15 idle minutes. Neon then scales to zero after its own 5 minutes. This is the Render sleep model on a host we control.
* **Models are Bedrock models in ap-south-1, called with the instance role** (no key is stored or configured). Embeddings: Amazon Titan Text Embeddings V2 at 1,024 dimensions. Answer: `openai.gpt-oss-120b`; judge: `qwen.qwen3-32b` (different families, as the settings validator requires). Reranker: an in-Region LLM call (`openai.gpt-oss-20b`) that scores the candidate passages 0 to 10 in one listwise request, because **Bedrock's rerank models (Amazon Rerank 1.0, Cohere Rerank 3.5) are not offered in ap-south-1** (AWS documentation, read 2026-11-19), and the project keeps every regional call in one Region. Claude is available in this Region only through an `in.*` inference profile, which may route to another Indian Region; it is not used until the owner decides.
* **A daily dollar cap replaces the neuron cap for Bedrock.** Every attempt reserves its estimated cost in micro-dollars in the same `cairn_meta.provider_usage` table, under the provider name `bedrock`, and a refused reservation makes no call. The default is 2 US dollars a day (`CAIRN_BEDROCK_DAILY_USD`).
* **The embedding dimension is a deployment setting.** Migration 0004 creates `vector(384)`. `cairn-db init --embed-dim 1024` (or `CAIRN_EMBED_DIM` in `.env.neon`) changes the column and rebuilds the HNSW index once, before any vector exists; it refuses, and changes nothing, if vectors of another dimension are stored (`--reset-embeddings` deletes them; they are re-created by the embedder). The default stays 384, so local runs and CI are unchanged.
* **Secrets** are SSM Parameter Store SecureStrings under `/cairn/` in ap-south-1. `scripts/aws_push_neon_secrets.py` (run by the owner) copies only the Neon host and the `cairn_app`, `cairn_dbos` and `cairn_ingest` passwords from `.env.neon`; the owner, admin and edge passwords never leave the laptop. The session secret, audit hash key and proxy token are generated on AWS and never printed. `cairn-run.sh` reads them with the instance role into tmpfs at start.
* **Infrastructure as code:** `infra/aws/cairn.yaml` (CloudFormation) holds the IAM policy for the host role (only the four models, the `/cairn/*` parameters, the log group, the artifact bucket), the artifact bucket and the container log group.

### Cost (estimate; AWS Price List API, Asia Pacific (Mumbai), read 2026-11-19)

| Item | Per month |
|---|---|
| Neon free plan | 0 |
| EC2 `t4g.small` ($0.0112/h), public IPv4, disk: already paid for the two other apps | about 13, shared |
| CloudWatch logs (14 days), SSM standard parameters, S3 artifacts | about 1 to 3 |
| Bedrock: `gpt-oss-120b` $0.18 / $0.71 per million input / output tokens, `qwen3-32b` $0.18 / $0.355, `gpt-oss-20b` $0.08 / $0.35, Titan embeddings about $0.02 per million input tokens (list price, not re-read for Mumbai) | about $0.0017 per answered question |

Cairn's own fixed cost is about 1 to 3 dollars a month. RDS `db.t4g.micro` would add $15.33 plus $2.62 for 20 GB; Aurora Serverless v2 at its 0.5 ACU minimum would add about $66, and never pause because of DBOS polling.

### Consequences

* The first API request after a pause waits for the container to start and DBOS to launch. **Not measured yet**; the web already shows a waking state. It is the price of staying inside Neon's free hours.
* Neon is outside AWS: traffic crosses the public internet over TLS with `sslmode=verify-full`, and the free plan has no IP allow-list. Every runtime role lacks BYPASSRLS, so a stolen connection string still sees nothing without a principal. The Neon region is chosen at project creation; Mumbai is preferred if offered, otherwise Singapore, which adds round-trip time to every query. **Not measured yet.**
* The LLM reranker is slower and noisier than a cross-encoder and costs tokens. `CAIRN_RERANKER=none` keeps the fused order. Its effect on Recall@5 is to be measured with the live Phase 5 run (ablation against no rerank); no quality claim is made here.
* Answer quality with these models is **not measured**; the only completed evaluation uses fakes.
* `boto3` is a new locked dependency of `cairn-retrieval`, and resolving it moved `python-dateutil` from 3.0.0 to 2.9.0.post0 in `uv.lock`.
* The GitHub live-sync receiver (Phase 3, a Pages Function writing to a thin inbox) is not deployed by this ADR: it needs a GitHub App that does not exist yet.

### Confirmation

`packages/retrieval/tests/unit/test_bedrock.py` (client, adapters, budget, settings), `packages/core/tests/unit/test_embed_dim.py`, `tests/deploy/test_waker.py` (a real HTTP run against a fake app), `tests/scripts/test_aws_push_neon_secrets.py`. The deployment itself is verified by the runbook's end-to-end check in [deploy-aws.md](../deploy-aws.md).
