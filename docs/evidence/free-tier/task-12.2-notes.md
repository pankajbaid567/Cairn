# Task 12.2: the threat model and its checker
Date: 3 Nov 2026. Requirements 19.5, 19.6; design §16. No Cloudflare, Neon or Render call was made. `.env` was not touched.
## Files
| File | Change |
|---|---|
| `docs/threat-model.md` | Status line; scope and assets (Cloudflare token, proxy token, Neon owner and admin passwords on the laptop); a new actor (anonymous internet client); the trust-boundary diagram now has Cloudflare (Pages Function proxy and Workers AI), Render, Neon and the operator laptop; TM-11 (Neon owner role has BYPASSRLS, laptop-only credential, never hosted); TM-13 (Workers AI receives chunk text, rerank passages and packed evidence; data terms from FV10); new TM-15 and TM-16 rows (phase 0); OWASP row; residual risks (hosted models, single shared proxy token that does not authenticate users, provider trust, exhaustion); Phase 0 controls list gains the proxy token, runtime-only secrets, TLS verify-full, the neuron guard, `_routes.json` and no keep-alive, Workers AI-only token with fakes by default |
| `scripts/check_docs.py` | `THREAT_IDS` is TM-01 to TM-16; docstring and function docstring. The ADR changes of task 12.1 are untouched |
| `tests/scripts/test_check_docs.py` | Sample threat table has 16 rows; unknown-id sample is TM-17; fenced-block case expects 16 findings; new tests for a missing TM-15 and TM-16, for the id range, for the repository's own threat-model group, and for the TM-15 and TM-16 rows being phase 0 and naming test functions that exist |
`tests/scripts/test_check_docs_properties.py` needed no change: it builds its documents from `THREAT_IDS`.
## Facts used
FV10 (Workers AI data terms: no training on customer content, no retention of standard requests; licenses), FV5 and task 5.2 (proxy token: identical 404, constant-time compare, no logging), FV6 and FV7 (`sync: false` secrets, `_routes.json`, the Function adds the token), FV11 (`verify-full` with the image CA bundle), design §16 and §18.
## Differences from the design's wording
- The design names the TM-15 test `test_freetier_app_env_has_only_runtime_secrets` and the TM-16 test `test_budget_guard_never_exceeds_cap`. Neither function exists. The rows cite the tests that do: `test_render_blueprint_has_no_secret_values` and `test_ft_app_environment_names_only_the_allowed_secrets` (TM-15); `test_budget_never_admits_past_the_cap` and `test_routes_json_limits_functions_to_api` (TM-16). A checker test confirms those names exist.
- The "Gemma terms (FV10, OQ-5)" wording is in design §15 only. The repository's license table (`docs/licenses.md`, task 10.1) already says Apache-2.0 for `@cf/google/gemma-4-26b-a4b-it` and no repository document contains the old wording.
- The proxy token protects the direct Render URL only. The Pages Function adds it for every caller of the public site, so the threat model records it as not being user authentication.
## Verify
| Command | Result |
|---|---|
| `uv run pytest tests/scripts/test_check_docs.py tests/scripts/test_check_docs_properties.py` | 85 passed |
| `uv run python scripts/check_docs.py --only threat-model` | ok |
| `make docs-check` | ok (all six groups) |
| `make types` | no issues (core strict 33 files, base 154 files) |
| `uv run ruff format --check .` and `uv run ruff check .` | clean |
| `uv run pytest tests/scripts/test_no_paradedb.py` | 12 passed |
