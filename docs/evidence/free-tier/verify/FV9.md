# FV9: neuron rates, free-plan availability and reasoning tokens
Resolved 3 Nov 2026 (task 8.3). Requirements 15.3 and 16.6; design FT-22, FT-24 and §12. Content was rephrased for compliance with licensing restrictions; the URLs are the authority. Only public pages were read. No Cloudflare call was made with user credentials, so the reasoning-token behavior (part 3) is from the published schemas, not from a live response (see "Open").

## 1. Rates on the pricing page, 3 Nov 2026

Source: [Cloudflare docs: Workers AI pricing](https://developers.cloudflare.com/workers-ai/platform/pricing/) (the Markdown form of the page, last modified 20 Oct 2026). One neuron costs $0.011 per 1,000.

| Model | Neurons per M input tokens | Neurons per M output tokens | In `NEURON_RATES` before | Now |
|---|---|---|---|---|
| `@cf/baai/bge-small-en-v1.5` | 1,841 ($0.020) | none | 1,836 | **1,841** |
| `@cf/baai/bge-reranker-base` | 283 ($0.003) | none | 283 | 283 |
| `@cf/google/gemma-4-26b-a4b-it` | 9,091 | 27,273 | 9,091 / 27,273 | same |
| `@cf/zai-org/glm-4.7-flash` | 5,500 | 36,400 | 5,500 / 36,400 | same |
| `@cf/meta/llama-3.1-8b-instruct-fp8-fast` | 4,119 | 34,868 | 4,119 / 34,868 | same |

Only `bge-small-en-v1.5` changed (1,836 to 1,841, +0.3%). The higher figure is kept, as task 1.1 asked. The model page still shows $0.0202 per M, which is the same price before rounding.

## 2. Free-plan availability

The pricing page lists the models that need Workers Paid or prepaid AI Gateway credits: `kimi-k2.6`, `kimi-k2.7-code`, `glm-5.2`, `glm-5.3`, `glm-5.3-flash` and the three `deepseek-v4*` models. `gemma-4-26b-a4b-it`, `glm-4.7-flash`, `bge-small-en-v1.5`, `bge-reranker-base` and `llama-3.1-8b-instruct-fp8-fast` are not on that list, so they run on the free allocation. This agrees with the 28 Jul 2026 changelog in [FV0](FV0-platform-facts.md). The error table ([Workers AI errors](https://developers.cloudflare.com/workers-ai/platform/errors/)) returns HTTP 403, code 5035, for a paid-only model; task 8.7 decides how the client reports it (FV8).

The free allocation is 10,000 neurons per day for all Workers AI use of the account, reset at 00:00 UTC (the pricing page). The guard's UTC day boundary matches. Rate limits by task type: 3,000 requests per minute for embeddings and 300 per minute for text generation on the free-eligible models ([Workers AI limits](https://developers.cloudflare.com/workers-ai/platform/limits/)); Phase 0 and the eval volumes stay far below both.

## 3. Reasoning output of glm-4.7-flash and gemma-4-26b-a4b-it

Sources: the model pages for [glm-4.7-flash](https://developers.cloudflare.com/workers-ai/models/glm-4.7-flash/) and [gemma-4-26b-a4b-it](https://developers.cloudflare.com/workers-ai/models/gemma-4-26b-a4b-it/) and their published output schemas.

- Both pages mark the model "Reasoning: Yes".
- Both return the OpenAI chat-completion shape: `choices[0].message.content` (string or null) and `usage` with `prompt_tokens`, `completion_tokens`, `total_tokens`, and `completion_tokens_details.reasoning_tokens`. In the OpenAI shape the reasoning tokens are a part of `completion_tokens`, so they are output tokens at the output rate. Neither page says reasoning tokens are free.
- Both inputs accept `max_completion_tokens` (and the deprecated `max_tokens`), described as an upper bound for the tokens generated for a completion. The `glm-4.7-flash` input also takes `chat_template_kwargs`, which the page does not describe further; it is not used.

Consequence for the guard (already in the `budget.py` docstring): `max_tokens_out` must cover the reasoning tokens plus the visible answer, and the same number is sent as `max_completion_tokens`. A call whose reasoning uses the whole bound returns empty `content` with `finish_reason: "length"`; the answer provider must treat that as a failure, not as an empty answer (task 8.13).

## 4. Budget figures (design §12) after the check

Recomputed with the table above and the same corpus assumptions:

| Use | Design | Now |
|---|---|---|
| Embed the whole corpus once (120,000 tokens) | about 220 | 221 (220.9) |
| One answer (query embed, rerank 30 passages, gemma 2,500 in and 400 out) | about 37 | about 37 (query embed about 0.04, rerank 2.97, gemma 33.6) |
| One judged eval answer | about 61 | about 61 (glm 3,000 in and 200 out adds 23.8) |
| Total of the design's three rows | about 6,650 of 9,000 | about 6,650 of 9,000 |

No figure in the design changes by more than one neuron, so the design text stays as written.

## Open

- Whether `completion_tokens` in a live Workers AI response includes the reasoning tokens (the schema says so by shape; no live call was made). The first live call in Phase 1 item 1.14 compares `usage.completion_tokens` with `completion_tokens_details.reasoning_tokens` and the neurons shown in the dashboard. Until then the guard assumes reasoning tokens are billed, which errs on the safe side.
