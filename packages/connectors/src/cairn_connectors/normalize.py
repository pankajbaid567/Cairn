"""Text normalization and revision planning (design §6; PD1-09).

A revision is written only when the normalized text differs from the latest revision, and never
when the incoming ``source_updated_at`` is older than the latest one. Normalization is
idempotent: ``normalize_text(normalize_text(x)) == normalize_text(x)``.
"""

from __future__ import annotations

import hashlib
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from enum import Enum


def normalize_text(text: str) -> str:
    """NFC, LF line ends, trailing whitespace stripped per line, blank lines at both ends dropped."""  # noqa: E501
    text = unicodedata.normalize("NFC", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.rstrip() for line in text.split("\n")]
    start, end = 0, len(lines)
    while start < end and not lines[start]:
        start += 1
    while end > start and not lines[end - 1]:
        end -= 1
    return "\n".join(lines[start:end])


def content_hash(normalized: str) -> str:
    """SHA-256 (hex) of the UTF-8 bytes of an already normalized text."""
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


class RevisionPlan(Enum):
    INSERT = "insert"
    SKIP_UNCHANGED = "skip_unchanged"
    REJECT_OLDER = "reject_older"


@dataclass(frozen=True)
class LatestRevision:
    content_hash: str
    source_updated_at: datetime


def plan_revision(
    latest: LatestRevision | None, incoming_hash: str, incoming_updated_at: datetime
) -> RevisionPlan:
    """INSERT with no latest revision; SKIP_UNCHANGED when the hashes are equal; REJECT_OLDER
    when the incoming time is strictly earlier; INSERT otherwise.

    A change back to older text is a new revision, because it differs from the latest one.
    """
    if latest is None:
        return RevisionPlan.INSERT
    if latest.content_hash == incoming_hash:
        return RevisionPlan.SKIP_UNCHANGED
    if incoming_updated_at < latest.source_updated_at:
        return RevisionPlan.REJECT_OLDER
    return RevisionPlan.INSERT
