# FV8: Workers AI REST API shapes, limits and error codes
Resolved from public documentation on 3 Nov 2026 (read during task 8.3 at the orchestrator's request, so that task 8.7 starts from it). Requirements 14.1 to 14.9; design §11. Content was rephrased for compliance with licensing restrictions; the URLs are the authority. No Cloudflare call was made.

## Endpoint and envelope

| Question | Answer | Source |
|---|---|---|
| Endpoint | `POST https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/run/{model}` with `Authorization: Bearer <token>` and a JSON body that holds the model's inputs directly (no model or input wrapper); the model id keeps its `@cf/vendor/name` slashes in the path | [REST API guide](https://developers.cloudflare.com/workers-ai/get-started/rest-api/); [Run a Workers AI model](https://developers.cloudflare.com/api/resources/ai/methods/run/) |
| Envelope | `{"result": {...}, "success": true, "errors": [], "messages": []}`; the model output is `result` | REST API guide |
| Token permissions | Workers AI Read and Workers AI Edit | REST API guide |

## Request and response shapes of the four default models

| Model | Request body | `result` | Source |
|---|---|---|---|
| `@cf/baai/bge-small-en-v1.5` | `{"text": "..." or ["...", ...], "pooling": "mean" or "cls"}`; `text` strings have `minLength` 1; the array allows at most 100 items; at most 512 input tokens per text; default pooling `mean` | `{"shape": [n, 384], "data": [[...], ...], "pooling": "mean"}` | [model page](https://developers.cloudflare.com/workers-ai/models/bge-small-en-v1.5/) and its sync input and output schemas |
| `@cf/baai/bge-reranker-base` | `{"query": str (minLength 1), "contexts": [{"text": str (minLength 1)}, ...], "top_k": int >= 1 (optional)}`; the index in `contexts` is what the response refers to | `{"response": [{"id": int, "score": number}, ...]}`; the score is a raw relevance value that a sigmoid maps to 0..1; the docs do not say what is returned without `top_k` (every context is the expected case) or in what order, so the parser sorts by `id` and checks the ids | [model page](https://developers.cloudflare.com/workers-ai/models/bge-reranker-base/) and its schemas |
| `@cf/google/gemma-4-26b-a4b-it` | OpenAI chat form: `{"messages": [{"role", "content"}, ...], "max_completion_tokens", "temperature", ...}` (`max_tokens` is deprecated) | `{"id", "object", "created", "model", "choices": [{"index", "message": {"role": "assistant", "content": str or null, "refusal"}, "finish_reason": "stop" or "length" or ...}], "usage": {"prompt_tokens", "completion_tokens", "total_tokens", "completion_tokens_details": {"reasoning_tokens"}}}` | [model page](https://developers.cloudflare.com/workers-ai/models/gemma-4-26b-a4b-it/) and its sync output schema |
| `@cf/zai-org/glm-4.7-flash` | same as gemma | same as gemma | [model page](https://developers.cloudflare.com/workers-ai/models/glm-4.7-flash/) and its sync output schema |

Findings that change details of design §11:

- The text models return the OpenAI `choices` shape, not the older `{"response": str}` shape. `parse_text` must accept `choices[0].message.content` first; the `response` form stays as a fallback only (the older models, such as `llama-3.1-8b-instruct-fp8-fast`, answer `{"response": str}` in the REST guide's example).
- `content` may be `null` (reasoning used the whole bound, `finish_reason: "length"`). `parse_text` rejects it as `bad_response`.
- The docs do not state that the reranker returns every context when `top_k` is not sent; the design's "every id 0..n-1 exactly once" check is the safe reading, and task 8.7's contract fixture must be replaced by a recorded live response when one exists.
- The embedding batch limit is 100 texts per call in the schema. The design's 50 is a safe choice and stays (FV8 permits it); there is no stated cap on total request size other than the 413 "Request too large" error below.

## Limits

| Question | Answer | Source |
|---|---|---|
| Rate limits | Embeddings 3,000 requests per minute; text generation 300 per minute (models that need Workers Paid get 20 per minute, which none of the defaults do) | [Workers AI limits](https://developers.cloudflare.com/workers-ai/platform/limits/) |
| Daily free allocation | 10,000 neurons per day, reset 00:00 UTC; beyond it the call fails with an error | [Workers AI pricing](https://developers.cloudflare.com/workers-ai/platform/pricing/) |
| Cloudflare API global limit | 1,200 requests per five minutes per user across all API traffic, HTTP 429 beyond it | [API rate limits](https://developers.cloudflare.com/fundamentals/api/reference/limits/) |

## Error codes

Source: [Workers AI errors](https://developers.cloudflare.com/workers-ai/platform/errors/). Internal code, HTTP status, meaning:

| Code | HTTP | Meaning | Client `ErrorCode` (proposed for task 8.7) |
|---|---|---|---|
| 3036 | 429 | Daily free allocation of 10,000 neurons used up | `daily_limit` (not retried) |
| 3040 | 429 | Capacity temporarily exceeded, try again | `rate_limited` (retried) |
| 5007 | 400 | No such model or task | `bad_request` |
| 5004, 3003 | 400 | Invalid data, incomplete request | `bad_request` |
| 3042 | 404 | Invalid model name | `bad_request` |
| 3006 | 413 | Request too large | `bad_request` |
| 3007, 3008 | 408 | Timeout, aborted | `timeout` (HTTP 408 is not in the default retry set; task 8.7 decides) |
| 5035 | 403 | Model needs a Workers Paid plan | `auth` |
| 5016, 5018, 3041, 3023 | 403 | Model terms not accepted, account not allowed, account blocked | `auth` |
| none listed | 401 | Missing or wrong token (Cloudflare API-level error, not in this table) | `auth` |

`DAILY_LIMIT_CODES` is `frozenset({3036})`.

## Retry-After

Cloudflare's API documents a `retry-after` header (seconds, rounded up) on responses that exceeded a rate limit ([API rate limits](https://developers.cloudflare.com/fundamentals/api/reference/limits/)). The Workers AI pages do not say whether the 429 for code 3040 carries it. The client reads `Retry-After` when present (integer seconds, bounded by `retry_after_cap_s`) and falls back to jittered exponential backoff when absent, as the design has it.

## Open

- Whether a 429 for code 3036 or 3040 carries `Retry-After` is not documented; a live call can show it, and the fallback covers both cases.
- The exact error body for a 401 is not on the Workers AI pages; the client classifies by HTTP status first and Cloudflare codes second, so it does not depend on it.

## Re-check, 3 Nov 2026 (task 8.7)
The [Workers AI errors](https://developers.cloudflare.com/workers-ai/platform/errors/) page was read again before the client was written. Still true: code 3036 is HTTP 429 "daily free allocation of 10,000 neurons" (not retried), code 3040 is HTTP 429 "capacity temporarily exceeded" (retried), 5035 is 403 "needs Workers Paid", 3042 is 404, 3006 is 413, 3007 and 3008 are 408. No change to the table above.

Decisions taken in `cloudflare/client.py` where this file left them open:
- `DAILY_LIMIT_CODES = frozenset({3036})`. A daily-limit code on any non-2xx status gives `daily_limit` and is never retried.
- 408 (codes 3007, 3008) is `timeout` and is not retried (Requirement 14.4 lists no 408). A connect or read timeout before any response is `timeout` and is retried.
- 403 (including 5035) and 401 are `auth`. Other 4xx are `bad_request`. 500, 502, 503, 504 are `server` and retried; other 5xx are `server` and not retried. Any other transport failure is `transport` and not retried.
- `Retry-After` is read as a non-negative number of seconds and capped at `retry_after_cap_s` (30). An absent, negative, non-numeric or HTTP-date value is ignored and the jittered backoff applies. The tests cover a 429 with and without the header.
- The contract fixtures are written from the documented shapes; the error codes in the 500 and 503 fixtures (3001, 3050) are illustrative. They are replaced by recorded responses when a live call is made (Phase 1 item 1.14).
