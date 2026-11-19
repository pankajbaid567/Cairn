# Cairn MCP server

A read-only MCP server (stdio) that lets a coding agent such as Claude Code ask Cairn what was decided and why. It runs **on your machine**, never as a hosted process, and talks to the Cairn API with a delegated token. Code: `apps/mcp/src/cairn_mcp/server.py`; token and routes: `apps/api/src/cairn_api/{agent_tokens,agent_cli,decisions,search}.py`.

## Set up (local compose)

```bash
make up                         # the stack, with a seeded corpus: make seed-local seed-decisions
uv run cairn-agent create --for asha --name claude-code --days 7     # prints a token once
```

Add the server to Claude Code (`.mcp.json` in the repository you are working on, or `claude mcp add`):

```json
{
  "mcpServers": {
    "cairn": {
      "command": "uv",
      "args": ["run", "--project", "/path/to/company_brain", "cairn-mcp"],
      "env": { "CAIRN_MCP_API_URL": "http://127.0.0.1:8000", "CAIRN_MCP_TOKEN": "<the token>" }
    }
  }
}
```

Against a deployment, set `CAIRN_MCP_API_URL` to the Pages origin plus `/api` and, if the API requires the proxy header, `CAIRN_MCP_PROXY_TOKEN`. Create the agent with `cairn-agent create ... --allow-remote` from a laptop that holds the database credentials. Keep the token out of version control; it expires (default 7 days, at most 30).

## How access works

The token names an **agent principal** (`agent:<user>-<name>`) whose `acting_for` is the user. Every query runs with **that user's groups**, so the agent can never read more than the person it acts for, and it reads less in one respect: it cannot resolve reviews or change anything (the API refuses agents on write routes). Audit rows record the agent as the caller and the user as `acting_for`, and the user sees them under Access and audit. Revoke an agent by letting its token expire, or by deleting its principal row.

## Tools (all read-only)

| Tool | Arguments | Returns |
|---|---|---|
| `search_context` | `query`, `limit` (1-10), `include_public` | Titles, locations, snippets and citation ids of evidence the user may read. Public untrusted content is excluded unless `include_public` is true; the response says how many were left out |
| `get_decision` | `key`, optional `valid`, `recorded` (YYYY-MM-DD) | Status as of those dates, claims with sources, timeline, links |
| `decision_history` | `key` | Events of one decision in valid-time order |
| `get_evidence` | `citation` | The text a citation id points at |
| `pre_change_brief` | `repo`, `paths` | Decisions anchored to the repo, the paths and their directories, with status and notes (partial reverts, amendments, supersession) |

An item you cannot read and one that does not exist look the same (a fixed "Not found, or you cannot see it").

## Limits

* Each result is capped at 8,000 characters (a truncation notice is added).
* 30 calls per minute per server process, and a daily cap per agent token on the API (`CAIRN_AGENT_CALLS_PER_DAY`, default 500) so one token cannot drain the shared neuron budget. MCP tools do not call the answer model, so they spend only query-embedding and rerank neurons.
* Public untrusted content (text from public GitHub issues and pull requests) is **excluded by default**. When included it is wrapped in `<untrusted-content ...>` with a note that it is data, not an instruction.
* Errors are fixed messages; the API's error body is never passed through.

## Do not pair Cairn with write-capable tools on untrusted repositories

Cairn's own output carries no instructions and its tools cannot act. But an agent that also has tools that write, run commands or fetch URLs, and that reads text from a repository anyone can file issues in, can be steered by that text. Cairn labels such text and leaves it out by default; it cannot stop another tool in the same agent from acting on what it reads elsewhere. Keep write-capable tools away from sessions that read untrusted content, or require human approval for them.

## Demo: plan around a partial revert

With the stack seeded, an agent acting for Asha calls `pre_change_brief` with `repo: payment-service`, `paths: ["db/pool.py"]`. The brief lists ADR-007 and ADR-009; ADR-009's notes show the 20 May partial revert ("The settlement job connects in session mode"), so the agent plans around session-mode connections for that job. The test `test_pre_change_brief_for_db_pool_py_surfaces_adr_009_and_its_partial_revert` checks this over the API; driving it from Claude Code is the user-run step (plan 4.23).
