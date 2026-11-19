# Task 8.1 notes: provider settings and the model family rule

Date: 2 Nov 2026. Requirements 16.6 to 16.9.

## What changed

- `cairn_core.model_family`: `FAMILY_PREFIXES` (design §13 table), `model_family(provider, model)`,
  `UnknownModelFamily`. `fake` provider gives family `fake`; the bare `o` prefix counts only for
  provider `openai`.
- `cairn_core.config`: `WorkersAiSettings`, `ModelSettings`, `LlmSettings` on a new
  `ProviderSettings` base (same source rules as `CommonSettings`: `.env`, empty counts as unset,
  frozen, extras ignored). `load_settings` and `invalid_keys` now accept any `BaseSettings`.
- Validators raise a `ValidationError` located at environment keys, so `load_settings` prints
  `KEY (code)` and exits 2 without a value:
  - `credential_missing` at `CLOUDFLARE_API_TOKEN`, `ANTHROPIC_API_KEY` or `OPENAI_API_KEY`
    for each non-fake provider (`ModelSettings`: embedder or reranker `workers_ai`;
    `LlmSettings`: answer or judge provider).
  - `same_model_family` at both `CAIRN_ANSWER_MODEL` and `CAIRN_JUDGE_MODEL` when neither side is
    fake and the families are equal.
  - `unknown_model_family` at the model key whose family is not in the table (the rule cannot be
    checked, so it is refused).

## Decisions to note

- `ModelSettings` and `LlmSettings` carry their own `CLOUDFLARE_API_TOKEN` field (design §6 shows
  none) so the credential rule can run inside them. Only the token is required; the account id
  stays optional there, and the Workers AI client raises `not_configured` without it (task 8.7).
- With the production defaults and no token, `ModelSettings()` and `LlmSettings()` exit 2. That is
  the intended reading of Requirement 16.8; `.env.example` selects the fakes.

## Verified

- `uv run pytest packages/core/tests/unit/test_config.py packages/core/tests/unit/test_model_family.py`: 79 passed.
- `make fmt-check lint types`: clean.
- `uv run pytest packages/core/tests/unit tests/scripts/test_dev_env.py -m "not integration"`: 511 passed.

No FV item is assigned to this task.
