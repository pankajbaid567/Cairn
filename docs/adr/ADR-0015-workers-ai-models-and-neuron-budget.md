---
status: accepted
date: 2026-11-03
decision-makers: Pankaj Baid
---

# ADR-0015: Embed, rerank and answer through Cloudflare Workers AI behind provider interfaces, with a neuron budget

Amends decision D16 of the [brainstorm decision log](../../brainstorm.md#1-tldr-and-decision-log). It records decisions FT-20 to FT-24 and FT-22 (the budget guard) of the `cairn-free-tier-retarget` spec. The deployment it runs in is [ADR-0014](ADR-0014-free-tier-deployment-cloudflare-render-neon.md).

## Context and Problem Statement

D16 assumed local embedding and reranking models, chosen in spike S3, and a hosted answer model reached with the user's own key. The free web service of ADR-0014 has 512 MB of RAM and 0.1 CPU, so no model runs in the process. Cloudflare Workers AI gives every account 10,000 neurons a day for free (FV0), and its catalog has embedding, rerank and text models.

The questions: which provider and models does Cairn call by default, how is the free allocation protected from being spent by retries, a restart or a loop, how is the rule that the judge comes from another model family enforced, and what leaves the deployment when a model is called?

## Decision Drivers

* Nothing may run in the 512 MB process, and CI must never call a hosted service.
* Stay inside the free allocation of 10,000 neurons per UTC day for the whole account.
* Phase 1 must plug in without new interfaces, and tests must be deterministic.
* The judge must not share a model family with the answer model (ADR-0005).
* Every model needs a license or terms entry in the license report.
* Secrets must never reach logs or error messages.
* The Render service restarts when it sleeps, so a counter held in memory is not enough.

## Considered Options

* Workers AI over REST behind async provider interfaces, with fakes for tests and a neuron budget guard, and optional Anthropic and OpenAI adapters (chosen).
* Local models inside the process (ONNX embedders and a local reranker).
* A paid hosted API only (Anthropic or OpenAI).
* Workers AI without a budget guard, relying on Cloudflare's own error when the allocation runs out.

## Decision Outcome

Chosen option: "Workers AI over REST behind async provider interfaces, with fakes and a neuron budget guard", because it is the only option that adds no memory to the process, costs nothing within the allocation, and cannot overspend it silently.

The decisions it records:

* Client (FT-20, FT-21). Workers AI is called over REST with `httpx` and no Cloudflare SDK. The client lives in `cairn_retrieval.cloudflare` and the tests drive it through `httpx.MockTransport`. At most 3 attempts. It retries 429 (except the daily-allocation error, code 3036, which is mapped to `daily_limit` and never retried), 500, 502, 503, 504, and connect and read timeouts. Backoff is exponential with full jitter, base 0.5 s, cap 8 s; `Retry-After` is honored up to 30 s; the connect timeout is 5 s and the read timeout 30 s. 401 and 403 (including code 5035, a model that needs Workers Paid) are `auth` and not retried. The client never logs or returns the token, the URL or the payload.
* Interfaces (FT-23). `Embedder` and `Reranker` in `cairn_retrieval.models` and `LlmProvider` in `cairn_retrieval.llm`, all async. Workers AI adapters and deterministic fakes (`fake-embed-v1`, `fake-rerank-v1`, `fake-llm-v1`) ship now; nothing calls them in Phase 0. `.env.example` selects the fakes. Anthropic and OpenAI stay optional with the user's key; their adapters are Phase 1 work and `build_llm` raises `NotImplementedError` naming the provider until then.
* Default models (FT-24). All four run on the free allocation: none is on the pricing page's list of models that need Workers Paid (FV9, read 3 Nov 2026). Licenses are from the publishers' model cards (FV10).

  | Role | Model | License | Neurons per million tokens (input / output) |
  |---|---|---|---|
  | Embedder, 384 dimensions | `@cf/baai/bge-small-en-v1.5` | MIT | 1,841 / none |
  | Reranker | `@cf/baai/bge-reranker-base` | MIT | 283 / none |
  | Answer | `@cf/google/gemma-4-26b-a4b-it` | Apache-2.0 | 9,091 / 27,273 |
  | Judge | `@cf/zai-org/glm-4.7-flash` | MIT | 5,500 / 36,400 |

  The design assumed Gemma's own terms of use for the answer model. The Gemma 4 model card states Apache-2.0, which is on the license allowlist, so open question OQ-5 needs no restriction handling. The Cloudflare model page links its own "Terms and License" text, which the fetch did not expose; if it differs, the user reconciles it. Not blocking, because the models are called as a hosted service and never redistributed. S3 (ADR-0007) may replace the embedder and reranker and item 1.14 (ADR-0012) picks the final answer and judge pair on 20 questions.
* Family rule (D16). Answer and judge must come from a different model family. `cairn_core.model_family` maps a provider and model to a family by prefix (`@cf/google/` is Google, `@cf/zai-org/` is Zhipu, `claude-` is Anthropic, `gpt-` and the bare `o` prefix for OpenAI, and others). Settings validation rejects a same-family pair, naming both keys, unless one side is the fake provider, and rejects a model whose family it cannot determine. The default pair, Google against Zhipu, passes.
* Neuron budget (FT-22). `NeuronBudget` reserves an estimate before every attempt, so each retry reserves again, and refuses a call that would pass `CAIRN_WORKERS_AI_DAILY_NEURONS`, default 9,000 per UTC day (90% of the free allocation). Reservations are never released, so the guard errs toward spending less. The estimate is `ceil((tokens_in × input rate + max_tokens_out × output rate) / 1e6)`, at least 1, with tokens estimated as `ceil(utf-8 bytes / 3)`. A model without a rate raises `UnpricedModel` and no call is made. The counter lives in a store: in memory for tests and local runs, or in Postgres (`cairn_meta.provider_usage`, migration `0002_provider_usage.sql`) through one atomic conditional upsert as `cairn_app`, which holds SELECT, INSERT and UPDATE on it and no DELETE. A store failure fails closed: no call is made. The Postgres store is the one in combined mode, because a sleep restarts the process.
* Reasoning tokens (FV9). Both text models are reasoning models, and their usage object reports `completion_tokens_details.reasoning_tokens`. In the OpenAI shape those are part of `completion_tokens`, so they are billed at the output rate. `max_tokens_out` must therefore cover the reasoning and the visible answer, and the same number is sent as `max_completion_tokens`. A response with empty `content` and `finish_reason` `length` is a failure of the answer provider, not an empty answer. The guard assumes reasoning tokens are billed until a live call shows otherwise.
* Egress. Chunk texts (to embed), candidate passages (to rerank) and packed evidence (to answer) leave the deployment for Cloudflare. FV10 records Cloudflare's published terms: customer content is not used to train models, a standard inference request is not retained after the response (each of the four model pages shows zero data retention), and operational metadata such as token counts is kept. Customer content includes inputs, outputs and embeddings. The user still confirms which Cloudflare agreement applies to the account. The corpus stays synthetic (D20), so no real company data crosses today. Only evidence the asker may see is ever sent (TM-13), and the threat model lists Workers AI as a processor.
* Credentials. The token is scoped to Workers AI only (FU3). A non-fake provider needs its credential at startup, and the error names the key, never the value. The live check `make workers-ai-live-check` is a user step (FU8), is in no gate and never runs in CI.

### Daily budget (Estimate)

Rates as above, on the Kestrel Pay corpus of about 400 chunks of 300 tokens (FV9 recomputed the figures; none moved by more than one neuron).

| Use | Neurons |
|---|---|
| Embed the whole corpus once (120,000 tokens) | about 221 |
| One answer: query embed, rerank of 30 passages (10,500 tokens), gemma answer (2,500 in, 400 out) | about 37 |
| One judged eval answer (answer plus glm judge, 3,000 in, 200 out) | about 61 |
| B0 and B1 eval runs on 30 questions with judge | about 3,700 |
| 50 demo answers | about 1,850 |
| Five full re-embeds | about 1,100 |
| Total of the last three rows | about 6,650 of the 9,000 cap |

Picking the model pair (item 1.14) can exceed a day. It is spread over days, or run with fakes first.

### Consequences

* Good, because the process stays small: no model files, no ONNX runtime, and the combined process measured about 95 MiB under load (ADR-0014).
* Good, because the allocation cannot be overspent by retries, a loop or a restart: every attempt reserves first, and the count survives a sleep.
* Good, because CI and tests need no token and are deterministic.
* Good, because the judge cannot be configured into the answer model's family.
* Bad, because every embedded chunk, reranked passage and answer context goes to a third party. This is acceptable only for synthetic data.
* Bad, because the guard is conservative (reservations are estimates and never released) and the 10,000 allocation is shared by the whole account, so other uses of the same account reduce what Cairn can spend.
* Bad, because 9,000 neurons a day limits the evaluation plan; larger eval runs need several days or a paid provider.
* Bad, because the rate table is a copy of a pricing page and drifts. Unknown models are refused, but a changed rate is not detected until someone reads the page again.
* Bad, because two facts are unconfirmed until the first live call: whether `completion_tokens` includes the reasoning tokens, and whether a 429 for the daily limit carries `Retry-After`.

### Confirmation

* Family rule: `packages/core/tests/unit/test_model_family.py`, `test_model_family_rule_properties.py` and `test_config.py` (the credential errors, the same-family error naming both keys, an unknown model family, and that `.env.example` selects the fakes).
* Budget: `packages/retrieval/tests/unit/test_budget.py`, `test_budget_cap_properties.py`, `test_budget_estimate_properties.py`, and the integration tests `test_budget_integration.py` (a 20-way concurrent reservation never passes the cap; `cairn_app` cannot DELETE) and `test_budget_store_properties.py` (the Postgres store equals the in-memory model). They run in both admin modes, so `make matrix-managed` covers them on `ft-db`.
* Client: `test_workers_ai_client.py` (URL, bearer header, body shapes, each error code, `Retry-After`, `not_configured`, `budget_exhausted` with a transport that fails if called), `test_workers_ai_retry_properties.py`, `test_workers_ai_backoff_properties.py`, `test_workers_ai_secret_hygiene_properties.py`, `test_workers_ai_roundtrip_properties.py`, `test_workers_ai_malformed_properties.py`, and the contract fixtures in `packages/retrieval/tests/unit/data/workers_ai/`.
* Providers and fakes: `test_providers.py` and `test_fakes_properties.py`.
* Licenses: `make licenses` and `tests/scripts/test_licenses.py` list the four models and the hosted-service terms.
* `tests/scripts/test_live_check.py` checks that the live check is not part of `make gates` or CI. `make workers-ai-live-check` (FU8) confirms the live behavior.

## Pros and Cons of the Options

### Workers AI over REST behind provider interfaces, with fakes and a budget guard

* Good, because it adds no memory, costs nothing within the allocation and keeps every call behind one guard.
* Good, because the interfaces let Phase 1 swap models by configuration.
* Neutral, because the free allocation is small and shared across the account.
* Bad, because content leaves the deployment.

### Local models inside the process

* Good, because no content leaves the deployment.
* Bad, because ONNX models and their runtime do not fit alongside the API in 512 MB, and the Phase 1 plan to load them (PD1-12) is dropped.

### A paid hosted API only

* Good, because the models are strong and the limits are high.
* Bad, because it costs money per call and every default run would need the user's key.

### Workers AI without a budget guard

* Good, because it needs no store.
* Bad, because a retry loop or a restart could spend the allocation and the first sign would be Cloudflare's daily-limit error, after which every call fails for the rest of the UTC day.

## More Information

* Verify results: [FV8, endpoint, shapes and error codes](../evidence/free-tier/verify/FV8.md), [FV9, rates, free-plan availability and reasoning tokens](../evidence/free-tier/verify/FV9.md), [FV10, data terms and model licenses](../evidence/free-tier/verify/FV10.md).
* [Task notes for the budget](../evidence/free-tier/task-8.3-notes.md).
* Revisit if: Workers AI misses the latency target, the free daily allowance cannot cover the evaluation plan (then add a paid provider), Cloudflare changes which models are free, or real company data replaces the synthetic corpus.
