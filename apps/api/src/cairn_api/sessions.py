"""Signed session cookies (PD1-15; Property 21).

A token is ``base64url(json) + "." + base64url(HMAC-SHA-256)``. The payload holds the persona and
an expiry. Verification is constant-time and rejects any tampered, truncated, expired or
malformed token with ``None``; it never raises and never echoes the token.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time

COOKIE_NAME = "cairn_session"


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _mac(secret: bytes, body: str) -> str:
    return _b64(hmac.new(secret, body.encode("ascii"), hashlib.sha256).digest())


def sign(persona: str, secret: bytes, ttl_s: int, *, now: float | None = None) -> str:
    payload = {"p": persona, "exp": int((now if now is not None else time.time()) + ttl_s)}
    body = _b64(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode())
    return f"{body}.{_mac(secret, body)}"


def verify(token: str | None, secret: bytes, *, now: float | None = None) -> str | None:
    """The persona, or ``None`` for anything that is not a valid unexpired token."""
    if not token or token.count(".") != 1 or len(token) > 512:
        return None
    body, _, mac = token.partition(".")
    try:
        if not hmac.compare_digest(mac.encode("ascii"), _mac(secret, body).encode("ascii")):
            return None
        payload = json.loads(_unb64(body))
    except (ValueError, UnicodeError):
        return None
    if not isinstance(payload, dict) or not isinstance(payload.get("p"), str):
        return None
    exp = payload.get("exp")
    if not isinstance(exp, int) or exp < (now if now is not None else time.time()):
        return None
    return payload["p"]
