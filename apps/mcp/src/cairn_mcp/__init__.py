"""Cairn MCP server (stdio, read-only; plan 4.8, 4.9).

Runs on the user's machine and talks to the Cairn API with a per-agent delegated token. It is never
a hosted process. See ``docs/mcp.md``.
"""

from cairn_mcp.server import main

__all__ = ["main"]
