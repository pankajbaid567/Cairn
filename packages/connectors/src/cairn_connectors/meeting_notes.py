"""Meeting-notes connector: Markdown files with front matter (design §5 and §8).

Readers are every attendee (``user:<id>``) plus each ``share_with`` entry. The revision text is
the body after front matter; ``source_updated_at`` is ``recorded``, else ``date``.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Final

from cairn_connectors.documents import AclRef, FormatError, SourceDocument
from cairn_connectors.formats.frontmatter import parse_front_matter
from cairn_connectors.normalize import normalize_text

CONNECTOR: Final = "meeting_notes"
FORMAT = "meeting-note-v1"


def to_datetime(value: object, where: str) -> datetime:
    """A ``YYYY-MM-DD`` date or an RFC 3339 timestamp, as an aware UTC datetime."""
    text = str(value)
    try:
        if len(text) == 10:
            return datetime.combine(date.fromisoformat(text), datetime.min.time(), UTC)
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise FormatError(f"{where}: not a date or timestamp") from None


def _list(meta: dict[str, Any], key: str, where: str) -> list[str]:
    value = meta.get(key, [])
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise FormatError(f"{where}: field {key!r} must be a list of strings")
    return value


def parse_meeting(path: Path, text: str) -> SourceDocument:
    where = path.name
    meta, body = parse_front_matter(text, source=where)
    if meta.get("cairn_format") != FORMAT:
        raise FormatError(f"{where}: cairn_format must be {FORMAT!r}")
    for key in ("id", "title", "date"):
        if not isinstance(meta.get(key), str):
            raise FormatError(f"{where}: field {key!r} is missing or not a string")
    attendees = _list(meta, "attendees", where)
    share_with = _list(meta, "share_with", where)
    acl = {AclRef("user", a) for a in attendees}
    for entry in share_with:
        acl.add(AclRef.parse(entry if ":" in entry else f"user:{entry}"))
    updated = to_datetime(meta.get("recorded") or meta["date"], where)
    return SourceDocument(
        connector=CONNECTOR,
        external_id=f"meeting:{meta['id']}",
        source_kind="meeting_note",
        title=meta["title"],
        text=normalize_text(body),
        source_updated_at=updated,
        acl=frozenset(acl),
        metadata={"date": meta["date"], "attendees": sorted(attendees), "kind": "meeting_note"},
    )


def load(root: Path) -> list[SourceDocument]:
    return [parse_meeting(f, f.read_text(encoding="utf-8")) for f in sorted(root.glob("*.md"))]
