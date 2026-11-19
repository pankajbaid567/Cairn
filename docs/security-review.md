# Security review checklist (Phase 6)

Each row says what was checked, how, and what only you can check on the deployment. Posture and residual risks: [security.md](security.md). Threats: [threat-model.md](threat-model.md). "Test" means a test in the repository that fails if the property breaks.

| # | Area | Check | Result | Evidence |
|---|---|---|---|---|
| 1 | Database roles | `cairn_app` can read content only through RLS and insert audit rows; `cairn_ingest` writes content; `cairn_edge` can only INSERT into the inbox; `cairn_dbos` is separate; none inherit or are superusers | pass | `packages/core/tests/integration/test_roles.py`, `test_the_edge_role_can_insert_into_the_inbox_and_do_nothing_else` |
| 2 | RLS | Every `cairn` table has forced RLS; every query on a protected relation carries an explicit predicate **and** RLS agrees; stripping the predicate leaves RLS alone returning the same rows | pass | `test_query_registry.py` (static, template, plan and backstop tests) |
| 3 | Neon owner | The Neon-created owner has `BYPASSRLS`; Cairn never connects as it at runtime. The hosted service holds only `cairn_app`, `cairn_dbos` and `cairn_ingest`; the owner and admin credentials stay on the laptop | pass in design; **you confirm** on Neon that the hosted environment has no owner or admin value | `render.yaml` holds no secret values (test); `test_ft_app_environment_names_only_the_allowed_secrets` |
| 4 | Laptop-only credentials | `CAIRN_OWNER_DB_PASSWORD` and `CAIRN_DB_ADMIN_PASSWORD` are used only by bootstrap, seed and the importer, which refuse a non-local host without `--allow-remote` | pass | `cairn_core.hosts`, `packages/eval/tests/unit/test_spike_host_guard*.py` |
| 5 | Secrets at rest | Render's environment and the Pages secrets hold secrets; `.env` is git-ignored; `render.yaml` and `wrangler.toml` hold no value; gitleaks scans the tree | pass; `make secrets` | `test_render_blueprint_has_no_secret_values` |
| 6 | Proxy token | The API refuses every path except `/healthz` without the proxy token, compared in constant time; the Pages Function drops any client-supplied token header | pass | `proxy.test.ts`, API proxy tests |
| 7 | Webhook at the edge | HMAC-SHA256 verified in constant time over the raw body; bad or missing signature gets 401 and stores nothing; a duplicate is a no-op; 500 on a store failure so GitHub redelivers; body capped at 1 MiB; no payload text stored | pass | `webhook.test.ts` and `test_sync_api.py` on the same 10 signed fixtures |
| 8 | `cairn_edge` | INSERT on the inbox only; its connection string is a Pages secret | pass | `test_the_edge_role_can_insert_into_the_inbox_and_do_nothing_else`; **you confirm** the Neon HTTP path and CPU time on Cloudflare |
| 9 | MCP tokens | Signed, expiring, per agent, separate key from session cookies; an agent acts with its user's access and cannot write; a per-agent daily cap; tokens never printed by the server | pass | `test_agent_tokens.py`, `test_mcp_agents.py` |
| 10 | Sessions | HMAC-signed cookie, `HttpOnly`, `SameSite=Strict`, `Secure` in demo; a bearer token is never rescued by a cookie | pass | `apps/api/tests/unit/test_sessions.py`, `test_missing_wrong_expired_and_foreign_tokens_get_nothing` |
| 11 | Prompt injection | Evidence is delimited data; no action tools; the verifier drops unsupported claims; the L6 scan drops hidden canary tokens; public untrusted content is excluded from agent search by default and fenced | pass | `test_injection.py`, `test_verify.py` |
| 12 | LLM egress | Only caller-readable evidence and the question go to Workers AI (default) or an optional provider; retrieval filters first. Data terms for Workers AI and optional providers are Cloudflare's, Anthropic's and OpenAI's, and **you verify them** before enabling a provider | pass in design; **terms: you verify** | [security.md](security.md#what-is-sent-to-model-providers) |
| 13 | Log contents | Logs hold identifiers, error classes and counts, never queries, answers, tokens, headers or payloads; the audit log holds hashes and ids only | pass | `test_proxy_token_hygiene_properties.py` (the token never reaches a log), `test_main.py` (access-log levels), the audit tests in `test_search_api.py` (ids and hashes only, append-only), and the webhook handler logs nothing |
| 14 | Synthetic data only | The corpus is the Kestrel Pay scenario; canary tokens are generated; no real person's data is in the repository or the demo | pass | scenario files, `scenarios/kestrel_pay` |
| 15 | Supply chain | Hashed pins, SBOM, `pip-audit` and `pnpm audit`, licence policy | pass with one documented moderate finding | [supply-chain.md](supply-chain.md) |
| 16 | Permission sync | A revocation is effective for every query path once its event is stored (503 until applied); content waits for ACL events; deleted sources are purged with their vectors, cache entries and sole-supported claims | pass | `test_sync_api.py`, `test_github_sync.py`, `test_deletion_purge.py`, `test_canary_sync_states.py` |
| 17 | Decision visibility | A claim is visible only through one fully readable support set; a conflict only if every side is readable; status derives from visible events | pass | `test_decisions.py`, `test_reviews.py` |

## Findings and open items

* No leak was observed in any matrix (50 tests, including all five MCP tools for two agents, and the sync states).
* **Open, yours:** rows 3, 8 and 12 need the real accounts. Row 12's data-terms check is a reading task, not code.
* **Accepted risk:** the answer endpoint may quote an attacker-written public issue when one is retrieved as evidence; see [security.md](security.md#residual-risks).
