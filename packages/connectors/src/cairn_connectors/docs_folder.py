"""Docs connector: ``<repo>/<path>`` Markdown files with front matter (design §5 and §8).

Readers are each ``read`` entry. The external id is ``doc:<repo>/<path>``; the revision text is
the body; ``source_updated_at`` is ``updated``, else ``date``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

from cairn_connectors.documents import AclRef, FormatError, SourceDocument
from cairn_connectors.formats.frontmatter import parse_front_matter
from cairn_connectors.meeting_notes import to_datetime
from cairn_connectors.normalize import normalize_text

CONNECTOR: Final = "docs_folder"
FORMAT = "doc-v1"
KINDS = ("doc", "adr_file", "wiki")


def parse_doc(path: Path, text: str) -> SourceDocument:
    where = str(path)
    meta, body = parse_front_matter(text, source=path.name)
    if meta.get("cairn_format") != FORMAT:
        raise FormatError(f"{path.name}: cairn_format must be {FORMAT!r}")
    for key in ("kind", "repo", "path", "title", "date"):
        if not isinstance(meta.get(key), str):
            raise FormatError(f"{path.name}: field {key!r} is missing or not a string")
    if meta["kind"] not in KINDS:
        raise FormatError(f"{path.name}: kind must be one of {', '.join(KINDS)}")
    read = meta.get("read")
    if not isinstance(read, list) or not read or not all(isinstance(r, str) for r in read):
        raise FormatError(f"{path.name}: field 'read' must be a non-empty list of strings")
    del where
    return SourceDocument(
        connector=CONNECTOR,
        external_id=f"doc:{meta['repo']}/{meta['path']}",
        source_kind=meta["kind"],
        title=meta["title"],
        text=normalize_text(body),
        source_updated_at=to_datetime(meta.get("updated") or meta["date"], path.name),
        acl=frozenset(AclRef.parse(r) for r in read),
        metadata={
            "repo": meta["repo"],
            "path": meta["path"],
            "author": meta.get("author"),
            "date": meta["date"],
        },
    )


def load(root: Path) -> list[SourceDocument]:
    return [parse_doc(f, f.read_text(encoding="utf-8")) for f in sorted(root.rglob("*.md"))]
