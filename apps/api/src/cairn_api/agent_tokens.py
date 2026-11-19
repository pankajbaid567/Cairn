"""Delegated agent tokens for the MCP server (plan 4.8, 4.17).

A token names one agent principal (``agent:<name>``, created with ``acting_for`` set to a user) and
expires. It is signed with a key derived from the session secret under a different label, so a
session cookie is never accepted as an agent token and the reverse. The token carries no access of
its own: the API resolves the agent to its user and every query runs with that user's groups, so an
agent can never exceed the user it acts for. It is read-only by construction (no write route
accepts an agent).
"""

from __future__ import annotations

import hashlib
import hmac

from cairn_api.sessions import sign, verify

PREFIX = "cairn-agent."
MAX_TTL_S = 30 * 24 * 3600


def _key(session_secret: bytes) -> bytes:
    return hmac.new(session_secret, b"cairn-agent-token-v1", hashlib.sha256).digest()


def issue(agent: str, session_secret: bytes, ttl_s: int, *, now: float | None = None) -> str:
    """A bearer token for ``agent`` (the part after ``agent:``), valid for ``ttl_s`` seconds."""
    if not 0 < ttl_s <= MAX_TTL_S:
        raise ValueError("ttl_s must be between 1 second and 30 days")
    return PREFIX + sign(agent, _key(session_secret), ttl_s, now=now)


def verify_token(
    token: str | None, session_secret: bytes, *, now: float | None = None
) -> str | None:
    """The agent name for a valid, unexpired token, else ``None``."""
    if not token or not token.startswith(PREFIX):
        return None
    return verify(token[len(PREFIX) :], _key(session_secret), now=now)
