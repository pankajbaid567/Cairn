"""Cheap decision detectors (plan 2.2): ADR paths and front matter, decision phrases, and decision
sections in meeting notes. They propose spans; extraction (LLM) and review decide. Pull-request
labels follow in Phase 3.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final, Literal

from cairn_connectors.documents import SourceDocument

Kind = Literal["adr_file", "decision_phrase", "meeting_section"]

_ADR_KEY: Final = re.compile(r"\bADR[-_ ]?(\d{3,4})\b", re.IGNORECASE)
_PHRASE: Final = re.compile(
    r"(?im)^(?:.*?\b)?(decision|decided|approved|we (?:will|adopt|decided)|accepted|rejected)\b[:.]?\s*(.*)$"  # noqa: E501
)
_SECTION: Final = re.compile(r"(?im)^#{1,6}\s*(decisions?|outcome|resolution)\s*$")


@dataclass(frozen=True)
class Detection:
    kind: Kind
    start: int
    end: int
    key_hint: str | None  # "ADR-007" when the text or title names one


def adr_key(text: str) -> str | None:
    m = _ADR_KEY.search(text)
    return f"ADR-{int(m.group(1)):03d}" if m else None


def detect(doc: SourceDocument) -> list[Detection]:
    """Spans of ``doc.text`` (code point offsets) that probably record a decision."""
    found: list[Detection] = []
    hint = adr_key(doc.title) or adr_key(doc.external_id)
    if doc.source_kind == "adr_file" or (hint and doc.source_kind == "doc"):
        found.append(Detection("adr_file", 0, len(doc.text), hint))
    for m in _SECTION.finditer(doc.text):
        end = doc.text.find("\n#", m.end())
        found.append(
            Detection("meeting_section", m.start(), end if end != -1 else len(doc.text), hint)
        )
    if not found:
        for m in _PHRASE.finditer(doc.text):
            found.append(
                Detection("decision_phrase", m.start(), m.end(), adr_key(m.group(0)) or hint)
            )
    return found
