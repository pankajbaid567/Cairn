"""Agent tokens (plan 4.17): signed, expiring, and separate from session cookies."""

from __future__ import annotations

import pytest

from cairn_api import agent_tokens
from cairn_api.sessions import sign

SECRET = b"s" * 40


def test_a_token_round_trips_and_expires() -> None:
    t = agent_tokens.issue("asha-claude", SECRET, 600, now=1000.0)
    assert t.startswith("cairn-agent.")
    assert agent_tokens.verify_token(t, SECRET, now=1100.0) == "asha-claude"
    assert agent_tokens.verify_token(t, SECRET, now=1700.0) is None  # expired


@pytest.mark.parametrize(
    "token", [None, "", "garbage", "cairn-agent.", "cairn-agent.a.b", "x" * 600]
)
def test_missing_or_malformed_tokens_get_nothing(token: str | None) -> None:
    assert agent_tokens.verify_token(token, SECRET) is None


def test_a_tampered_token_or_wrong_secret_gets_nothing() -> None:
    t = agent_tokens.issue("asha-claude", SECRET, 600)
    body, mac = t.rsplit(".", 1)
    flipped = body + "." + ("A" if mac[0] != "A" else "B") + mac[1:]
    assert agent_tokens.verify_token(flipped, SECRET) is None
    assert agent_tokens.verify_token(t, b"o" * 40) is None
    assert agent_tokens.verify_token(t.replace("cairn-agent.", ""), SECRET) is None


def test_a_session_cookie_is_not_an_agent_token_and_the_reverse() -> None:
    cookie = sign("asha", SECRET, 600)
    assert agent_tokens.verify_token(cookie, SECRET) is None
    assert (
        agent_tokens.verify_token(agent_tokens.PREFIX + cookie, SECRET) is None
    )  # signed with the wrong key
    from cairn_api.sessions import verify

    assert verify(agent_tokens.issue("asha-claude", SECRET, 600), SECRET) is None


def test_lifetime_is_bounded() -> None:
    for ttl in (0, -1, agent_tokens.MAX_TTL_S + 1):
        with pytest.raises(ValueError):  # noqa: PT011
            agent_tokens.issue("a", SECRET, ttl)
