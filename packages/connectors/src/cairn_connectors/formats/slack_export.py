"""A Slack-style export folder (design §5, V21).

    users.json  channels.json  groups.json  <channel>/<YYYY-MM-DD>.json

``channels.json`` lists public channels, ``groups.json`` private ones. Message files hold
``{"type": "message", "user", "text", "ts", "thread_ts"}`` with ``ts`` as epoch seconds with six
decimals. Parser and printer live together so the renderer and the importer cannot drift apart.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from cairn_connectors.documents import FormatError


@dataclass(frozen=True)
class SlackUser:
    id: str  # "U" plus 8 hex characters
    name: str  # the person id
    real_name: str


@dataclass(frozen=True)
class SlackChannel:
    id: str
    name: str
    created: int  # epoch seconds
    members: tuple[str, ...]  # Slack user ids
    private: bool


@dataclass(frozen=True)
class SlackMessage:
    user: str  # Slack user id
    text: str
    ts: str
    thread_ts: str | None = None


@dataclass(frozen=True)
class SlackExport:
    users: tuple[SlackUser, ...]
    channels: tuple[SlackChannel, ...]
    messages: dict[str, tuple[SlackMessage, ...]]  # channel name -> messages in ts order


def ts_to_datetime(ts: str) -> datetime:
    """Epoch seconds with decimals to an aware UTC datetime (microsecond precision)."""
    try:
        seconds = Decimal(ts)
    except InvalidOperation:
        raise FormatError(f"invalid message ts {ts!r}") from None
    micro = int((seconds * 1_000_000).to_integral_value())
    return datetime.fromtimestamp(0, UTC) + timedelta(microseconds=micro)


def _load(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError):
        raise FormatError(f"{path.name}: cannot read file") from None
    except json.JSONDecodeError:
        raise FormatError(f"{path.name}: not valid JSON") from None


def _field(obj: Any, key: str, kind: type, where: str) -> Any:
    if not isinstance(obj, dict) or not isinstance(obj.get(key), kind):
        raise FormatError(f"{where}: field {key!r} is missing or not a {kind.__name__}")
    return obj[key]


def parse_export(root: Path) -> SlackExport:
    """Read an export folder. Raises ``FormatError`` naming the file and the field."""
    users = tuple(
        SlackUser(
            _field(u, "id", str, "users.json"),
            _field(u, "name", str, "users.json"),
            _field(u, "real_name", str, "users.json"),
        )
        for u in _load(root / "users.json")
    )
    channels: list[SlackChannel] = []
    for file, private in (("channels.json", False), ("groups.json", True)):
        path = root / file
        if not path.exists():
            continue
        for c in _load(path):
            channels.append(
                SlackChannel(
                    _field(c, "id", str, file),
                    _field(c, "name", str, file),
                    int(_field(c, "created", int, file)),
                    tuple(_field(c, "members", list, file)),
                    private,
                )
            )
    messages: dict[str, tuple[SlackMessage, ...]] = {}
    for channel in channels:
        found: list[SlackMessage] = []
        folder = root / channel.name
        for day_file in sorted(folder.glob("*.json")) if folder.is_dir() else []:
            for m in _load(day_file):
                where = f"{channel.name}/{day_file.name}"
                found.append(
                    SlackMessage(
                        _field(m, "user", str, where),
                        _field(m, "text", str, where),
                        _field(m, "ts", str, where),
                        m.get("thread_ts") if isinstance(m.get("thread_ts"), str) else None,
                    )
                )
        found.sort(key=lambda m: (Decimal(m.ts), m.user))
        messages[channel.name] = tuple(found)
    return SlackExport(users, tuple(channels), messages)


def _dump(value: object) -> str:
    return json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def print_export(export: SlackExport, root: Path) -> None:
    """Write the export folder deterministically (sorted keys, LF, one file per channel and day)."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "users.json").write_text(
        _dump([{"id": u.id, "name": u.name, "real_name": u.real_name} for u in export.users]),
        encoding="utf-8",
    )
    for file, private in (("channels.json", False), ("groups.json", True)):
        chosen = [c for c in export.channels if c.private is private]
        if chosen or file == "channels.json":
            (root / file).write_text(
                _dump(
                    [
                        {
                            "id": c.id,
                            "name": c.name,
                            "created": c.created,
                            "members": list(c.members),
                        }
                        for c in chosen
                    ]
                ),
                encoding="utf-8",
            )
    for channel in export.channels:
        by_day: dict[str, list[dict[str, str]]] = {}
        for m in export.messages.get(channel.name, ()):
            day = ts_to_datetime(m.ts).date().isoformat()
            item = {"type": "message", "user": m.user, "text": m.text, "ts": m.ts}
            if m.thread_ts is not None:
                item["thread_ts"] = m.thread_ts
            by_day.setdefault(day, []).append(item)
        folder = root / channel.name
        folder.mkdir(parents=True, exist_ok=True)
        for day, items in sorted(by_day.items()):
            (folder / f"{day}.json").write_text(_dump(items), encoding="utf-8")
