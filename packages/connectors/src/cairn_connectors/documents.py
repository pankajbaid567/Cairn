"""Plain data the connectors produce and the store consumes (design §8)."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Final, Literal

Connector = Literal["chat_export", "meeting_notes", "docs_folder", "github"]
SourceKind = Literal[
    "chat_thread", "meeting_note", "doc", "adr_file", "wiki", "pull_request", "issue"
]
AclKind = Literal["user", "team", "channel", "group"]

_REF_RE: Final = re.compile(r"^(user|team|channel|group):([a-z0-9][a-z0-9_.-]*)$")


class FormatError(Exception):
    """A source file is malformed. The message names the file and the field, never the content."""


@dataclass(frozen=True, order=True)
class AclRef:
    """A principal an item is readable by. The external id is ``f"{kind}:{id}"``."""

    kind: AclKind
    id: str

    @property
    def external_id(self) -> str:
        return f"{self.kind}:{self.id}"

    @classmethod
    def parse(cls, text: str) -> AclRef:
        match = _REF_RE.match(text)
        if match is None:
            raise FormatError(f"invalid principal reference (expected kind:id): {text!r}")
        return cls(match.group(1), match.group(2))  # type: ignore[arg-type]


@dataclass(frozen=True)
class SourceDocument:
    connector: Connector
    external_id: str
    source_kind: SourceKind
    title: str
    text: str  # already normalized
    source_updated_at: datetime
    acl: frozenset[AclRef]
    metadata: Mapping[str, object] = field(default_factory=dict)
    source_key: str | None = None  # "github:<org>/<repo>"; None means "import:<connector>"


@dataclass(frozen=True)
class DirUser:
    id: str
    name: str
    external: bool = False


@dataclass(frozen=True)
class DirGroup:
    id: str  # external id such as "team:payments" or "group:employees"
    name: str


@dataclass(frozen=True)
class DirMembership:
    user: str
    group: str  # external id
    start: date
    end: date | None = None


@dataclass(frozen=True)
class DirectorySnapshot:
    as_of: date
    users: tuple[DirUser, ...]
    groups: tuple[DirGroup, ...]
    memberships: tuple[DirMembership, ...]
