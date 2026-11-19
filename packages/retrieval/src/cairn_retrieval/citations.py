"""Citation ids: ``chunk:<external id>@<hash8>:<start>-<end>`` (design §16, Properties 16, 17)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

_HASH8: Final = re.compile(r"^[0-9a-f]{8}$")
PREFIX: Final = "chunk:"


class CitationError(ValueError):
    """The text is not a citation id."""


@dataclass(frozen=True)
class Citation:
    external_id: str  # "doc:eng-docs/design/payment-db.md"
    hash8: str  # first 8 hex of the revision content hash
    start: int
    end: int


def format_citation(c: Citation) -> str:
    return f"{PREFIX}{c.external_id}@{c.hash8}:{c.start}-{c.end}"


def parse_citation(s: str) -> Citation:
    """Splits from the right, so an external id may contain ``:`` and ``@``."""
    if not s.startswith(PREFIX):
        raise CitationError("not a citation")
    body = s[len(PREFIX) :]
    head, sep, span = body.rpartition(":")
    if not sep:
        raise CitationError("missing offsets")
    start_s, dash, end_s = span.partition("-")
    if not dash or not start_s.isdigit() or not end_s.isdigit():
        raise CitationError("bad offsets")
    external, at, hash8 = head.rpartition("@")
    if not at or not external or not _HASH8.match(hash8):
        raise CitationError("bad hash")
    start, end = int(start_s), int(end_s)
    if end <= start:
        raise CitationError("empty span")
    return Citation(external, hash8, start, end)
