"""The ``Violation`` record and JSON pointer helpers shared by every validator rule (design §12)."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass


@dataclass(frozen=True, order=True)
class Violation:
    """One rule violation.

    ``rule`` is a rule id such as ``"TL-1"``, ``path`` a JSON pointer into the scenario
    (``""`` is the document root) and ``message`` a short explanation for the author.
    The field order makes the natural ordering "by rule id, then pointer, then message",
    which is the report order (requirement 13.2).
    """

    rule: str
    path: str
    message: str


def pointer(*parts: str | int) -> str:
    """Build an RFC 6901 JSON pointer, escaping ``~`` and ``/`` inside names."""
    return "".join("/" + str(part).replace("~", "~0").replace("/", "~1") for part in parts)


def finalize(violations: Iterable[Violation]) -> list[Violation]:
    """Sorted, duplicate-free list: the deterministic report order (requirements 13.2, 13.24)."""
    return sorted(set(violations))
