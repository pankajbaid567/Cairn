"""The directory file: users, groups and dated memberships (design §5).

It stands in for GitHub team sync until Phase 3. ``group:public`` is implicit and never listed.
"""

from __future__ import annotations

import json
from datetime import date
from typing import Any

from cairn_connectors.documents import (
    DirectorySnapshot,
    DirGroup,
    DirMembership,
    DirUser,
    FormatError,
)

FORMAT = "cairn-directory-v1"


def _req(obj: dict[str, Any], key: str, kind: type, where: str) -> Any:
    if key not in obj or not isinstance(obj[key], kind):
        raise FormatError(f"{where}: field {key!r} is missing or not a {kind.__name__}")
    return obj[key]


def _date(text: str, where: str) -> date:
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise FormatError(f"{where}: not a date (YYYY-MM-DD)") from None


def parse_directory(text: str, *, source: str = "directory.json") -> DirectorySnapshot:
    try:
        raw = json.loads(text)
    except json.JSONDecodeError:
        raise FormatError(f"{source}: not valid JSON") from None
    if not isinstance(raw, dict) or raw.get("format") != FORMAT:
        raise FormatError(f"{source}: format must be {FORMAT!r}")
    users = tuple(
        DirUser(
            _req(u, "id", str, f"{source} users[{i}]"),
            _req(u, "name", str, f"{source} users[{i}]"),
            bool(u.get("external", False)),
        )
        for i, u in enumerate(_req(raw, "users", list, source))
    )
    groups = tuple(
        DirGroup(
            _req(g, "id", str, f"{source} groups[{i}]"),
            _req(g, "name", str, f"{source} groups[{i}]"),
        )
        for i, g in enumerate(_req(raw, "groups", list, source))
    )
    memberships = tuple(
        DirMembership(
            _req(m, "user", str, f"{source} memberships[{i}]"),
            _req(m, "group", str, f"{source} memberships[{i}]"),
            _date(_req(m, "from", str, f"{source} memberships[{i}]"), f"{source} memberships[{i}]"),
            _date(m["to"], f"{source} memberships[{i}]") if m.get("to") else None,
        )
        for i, m in enumerate(_req(raw, "memberships", list, source))
    )
    as_of = _date(_req(raw, "as_of", str, source), source)
    snapshot = DirectorySnapshot(as_of, users, groups, memberships)
    known_users = {u.id for u in users}
    known_groups = {g.id for g in groups}
    for m in memberships:
        if m.user not in known_users:
            raise FormatError(f"{source}: membership names an unknown user {m.user!r}")
        if m.group not in known_groups:
            raise FormatError(f"{source}: membership names an unknown group {m.group!r}")
        if m.end is not None and m.end <= m.start:
            raise FormatError(f"{source}: membership of {m.user!r} ends before it starts")
    return snapshot


def print_directory(snapshot: DirectorySnapshot) -> str:
    """Deterministic JSON: sorted keys, sorted lists, LF line end."""
    doc = {
        "format": FORMAT,
        "as_of": snapshot.as_of.isoformat(),
        "users": [
            {"id": u.id, "name": u.name, "external": u.external}
            for u in sorted(snapshot.users, key=lambda u: u.id)
        ],
        "groups": [
            {"id": g.id, "name": g.name} for g in sorted(snapshot.groups, key=lambda g: g.id)
        ],
        "memberships": [
            {"user": m.user, "group": m.group, "from": m.start.isoformat()}
            | ({"to": m.end.isoformat()} if m.end else {})
            for m in sorted(snapshot.memberships, key=lambda m: (m.user, m.group, m.start))
        ],
    }
    return json.dumps(doc, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
