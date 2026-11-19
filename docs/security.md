# Security posture: injection, agents and what leaves the service

The threat model is [threat-model.md](threat-model.md); this note covers prompt injection and what is sent to model providers.

## Injection posture

Text in sources is attacker-writable (a public issue, a pull request from a stranger, a chat message). Cairn treats it as data at every step:

1. **Evidence is delimited data.** The answer prompt passes each chunk in an `<evidence>` block with its source kind and tier; `<` and `&` are escaped so a chunk cannot close its block; the system prompt says the blocks are data.
2. **No action tools.** The answer workflow calls the model once and has no tools. The MCP server has five read-only tools that take no command, URL or file path.
3. **The verifier is the backstop** ([ADR-0017](adr/ADR-0017-answer-workflow-as-plain-async-steps.md)). A claim survives only if it is supported by the spans it cites, its dates appear in the evidence, and its citations are inside the caller's visible set. The L6 output scan drops any canary-format token that is not in the visible evidence. If nothing survives, the answer is the constant not-found reply.
4. **Permissions, not instructions, decide access.** A model told to "read the security policy" can only read what the caller's groups allow; retrieval is filtered in SQL (row-level security plus an explicit predicate) before any text reaches the model.
5. **Public untrusted content is fenced for agents.** MCP search leaves out text from public issues and pull requests unless asked, and wraps it in a labelled block when included.

Tests: `test_injection.py` (the injected `kestrel-sdk` issue), `test_verify.py`, `test_mcp_agents.py` (canary matrix on all five tools for two agents).

## Residual risks

* **A model can still repeat injected text.** If a public issue is included as evidence, an answer may quote it. It is cited and the answer is bounded to cited text, but a quoted instruction is still on screen. Mitigation: untrusted content is excluded from agent search by default; for the answer endpoint the quote is shown with its source kind and location so a reader can judge it.
* **Phrase canaries cannot be pattern-scanned.** The L6 scan finds canary-format tokens (`KP-FAMILY-...`). A hidden *sentence* leaking would be caught only by the permission filter, not the scan.
* **The verifier is lexical.** It checks that a claim's words, numbers and dates appear in the cited spans. A claim that rearranges true words into a false meaning can pass. The model sees only permitted evidence, so the damage is a wrong statement from permitted text, not a leak.
* **Other tools in the same agent.** See the warning in [mcp.md](mcp.md).
* **A stolen agent token** reads what its user reads until it expires. Tokens are short-lived, per-agent and capped per day.
* **The webhook secret** is the only gate on the edge receiver; a forged event can only queue a re-read ([ADR-0016](adr/ADR-0016-edge-webhook-receiver-and-drain-before-serve.md)).

## What is sent to model providers

* **Workers AI (default).** Query text goes to the embedding and rerank models. Passages the caller can read go to the reranker; the packed evidence and the question go to the answer model. Nothing the caller cannot read is ever sent, because retrieval is filtered first. Cloudflare's terms for Workers AI apply to that text.
* **Optional Anthropic or OpenAI adapters** (off unless a key is configured) receive the same packed evidence and question. Enabling one sends permitted evidence text to that provider; do not enable it for a corpus whose terms forbid that.
* **The laptop model cache** (`var/cache/models`, SQLite) stores vectors and rerank scores keyed by text hash, not text. A deletion purge removes cached entries for deleted text.
* **Audit logs** hold hashes, item ids and counts, never question or answer text.
