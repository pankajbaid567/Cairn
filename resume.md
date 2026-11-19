# Cairn: resume material

Repo: github.com/pankajbaid567/Cairn · Live: https://cairn.3-109-36-90.sslip.io (demo personas, synthetic data)
What it is: a permission-aware memory of engineering decisions. It answers "why did we decide X, who approved it, is it still in force" with cited sources, and shows each person only what they are allowed to read.
Stack: Python, FastAPI, DBOS, PostgreSQL 18 + pgvector with row-level security (Neon), Amazon Bedrock (Titan v2, gpt-oss, Qwen3), AWS EC2 + IAM + SSM + CloudFormation + CloudWatch Logs, Caddy, Docker, React, MCP, pytest, mypy --strict.

Numbers come from `docs/evidence/aws/README.md`, `docs/evidence/phase-5/runs/2026-11-19-live/` and `docs/results.md`. The "Do not claim" list matters as much as the bullets.

## CARL bullets (pick 3 or 4)

**1. Permission enforcement in the database**
- **Context:** a company memory that mixes chat, meetings and docs must never show a person a source they cannot read, including through search, citations or agents.
- **Actions:** **Enforced** access with PostgreSQL row-level security (forced, so even the app role obeys it), **planted** canary tokens in restricted documents, and **ran** a leak matrix across four retrieval variants.
- **Results:** **0 leaks** across 46 live-model questions and all four variants; 0 of 4 must-not-cite violations; 2,111 unit tests, `mypy --strict`.
- **Learning:** put the guarantee in the database, not the app; vectors stay in pgvector so the same policy covers them.

**2. Bedrock migration with a cost guard**
- **Context:** the first version used another model provider; the target was AWS-only models in one Region (ap-south-1).
- **Actions:** **Built** Bedrock adapters (Titan v2 embeddings at 1,024 dimensions, `gpt-oss-120b` answers, `qwen3-32b` judge) behind the existing provider protocols, **replaced** the missing Bedrock rerank model with an LLM listwise reranker, and **added** a daily dollar cap stored in the database.
- **Results:** a full verification session cost **about $0.005**; a call to each model takes 0.2 to 0.7 s; 71 new tests.
- **Learning:** when a managed model is not offered in your Region, a small prompted model plus a strict parser is a workable substitute, but its failure modes need logging.

**3. Cheap always-available hosting**
- **Context:** the free database tier (100 compute-hours a month) runs out in about 17 days if the app polls it all day.
- **Actions:** **Wrote** a stdlib front door (the "waker") that starts the container on the first request and stops it after 15 idle minutes, **deployed** on an existing EC2 `t4g.small` with CloudFormation, SSM SecureString parameters and an instance role (no keys on disk).
- **Results:** cold start **8 to 11 s**, idle stop confirmed at 20 minutes, fixed cost about **$1 to $3 a month**; 23 waker tests.
- **Learning:** measure what keeps the database awake (a background poller) before choosing the hosting shape.

**4. Finding bugs by running it for real**
- **Context:** all unit tests passed, but the live service had two defects.
- **Actions:** **Traced** a crash at start to macOS AppleDouble `._*` files in the build archive, and a silent degradation to a miscounted reranker reply (25 scores for 26 passages); **changed** the reranker to return scores keyed by passage number and **added** a bounded diagnostic log.
- **Results:** live answers ran with no degraded stages; the packaging script now refuses such archives.
- **Learning:** log the model's bad output (bounded) when a parser rejects it; do not guess.

## Honest results to quote

- Live-model run, 46 questions (30 dev, 16 test), small synthetic corpus (33 chunks): Recall@5 0.98 to 1.00 for all variants, 0 leaks, p50 144 ms (hybrid search) and 479 ms (with the LLM reranker), as-of status accuracy 12 of 14.
- Answer abstention was correct on **31 of 46 (67%)**, and the gold-labelled sources were cited 29% to 52% of the time. Say this is the weak part and what you plan next.

## Do not claim

- That the reranker or the vectors improve retrieval: the corpus is too small (Recall@5 is saturated), so this run cannot show it.
- Real users, real company data or production traffic: it is a demo with synthetic data and persona login (no real sign-in yet).
- Answer quality beyond the numbers above; the judge labels are not human-checked yet.
- That the Neon database is on AWS: it is on Neon (Singapore); everything else is on AWS in Mumbai.
- Any comparison with other products or "state of the art".

## Interview one-liners

- "I put access control in PostgreSQL row-level security and tested it with planted canary tokens, so a leak is a failing test."
- "The app sleeps when idle so a free database tier lasts the month; the first request takes about 10 seconds."
- "My live run found my reranker miscounting scores; I changed the contract so a miscount cannot misalign them."
