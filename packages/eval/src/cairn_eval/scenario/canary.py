"""Deterministic canary tokens (design §11, PD-14).

A token canary is a unique string planted in one restricted source. Seeing it anywhere
else proves a leak. Tokens are derived, never typed:

    KP-<FAMILY>-<first 16 base32 characters of SHA-256("cairn-canary" 0x1F seed 0x1F id)>

That is 80 bits, so tokens for different ids never collide in practice (rule CAN-3
still checks). The family is part of the visible token but not of the digest.

Scenario text refers to a token canary as ``{{canary:<id>}}``; renderers (Phase 1)
substitute the token. Phrase canaries are literal text and have no generator.
"""

from __future__ import annotations

import base64
import hashlib
import re
from typing import Final

CANARY_FAMILIES: Final = ("hr", "partner", "security", "incident")
CANARY_RE: Final = re.compile(r"^KP-(HR|PARTNER|SECURITY|INCIDENT)-[A-Z2-7]{16}$")

#: ``{{canary:CAN-HR-01}}``; group 1 is the canary id.
PLACEHOLDER_RE: Final = re.compile(r"\{\{canary:([A-Za-z0-9_-]+)\}\}")

_SEPARATOR: Final = "\x1f"


def canary_token(seed: str, canary_id: str, family: str) -> str:
    """Return the token for ``canary_id`` under ``seed``.

    Pure and deterministic. ``family`` must be one of :data:`CANARY_FAMILIES`
    (lowercase); anything else raises ``ValueError``.
    """
    if family not in CANARY_FAMILIES:
        raise ValueError(f"unknown canary family {family!r}; expected one of {CANARY_FAMILIES}")
    material = f"cairn-canary{_SEPARATOR}{seed}{_SEPARATOR}{canary_id}"
    digest = hashlib.sha256(material.encode()).digest()
    return f"KP-{family.upper()}-{base64.b32encode(digest).decode()[:16]}"
