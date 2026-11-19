"""Chat connector: a Slack-style export folder to one item per thread (design §6 and §8).

Text is one line per message, ``[<ISO UTC>] <user>: <text>``, in ts order. Newlines inside a
message become spaces so a message stays on one line (the chunker never splits inside a line).
Public channels are readable by ``group:employees``; a private channel by ``channel:<name>``.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Final

from cairn_connectors.documents import AclRef, SourceDocument
from cairn_connectors.formats.slack_export import (
    SlackChannel,
    SlackExport,
    SlackMessage,
    parse_export,
    ts_to_datetime,
)
from cairn_connectors.normalize import normalize_text

CONNECTOR: Final = "chat_export"
TITLE_CHARS = 80


@dataclass(frozen=True)
class ChannelGroup:
    """Members of a private channel, as person ids, from ``groups.json``."""

    name: str
    created: int
    members: tuple[str, ...]


def _stamp(ts: str) -> str:
    return ts_to_datetime(ts).strftime("%Y-%m-%dT%H:%M:%SZ")


def _one_line(text: str) -> str:
    return " ".join(text.split())


def threads(messages: tuple[SlackMessage, ...]) -> dict[str, list[SlackMessage]]:
    """Group messages by thread root ts (a message without ``thread_ts`` is its own thread)."""
    grouped: dict[str, list[SlackMessage]] = {}
    for m in messages:
        grouped.setdefault(m.thread_ts or m.ts, []).append(m)
    return grouped


def documents_from_export(export: SlackExport) -> list[SourceDocument]:
    names = {u.id: u.name for u in export.users}
    docs: list[SourceDocument] = []
    for channel in export.channels:
        acl = (
            frozenset({AclRef("channel", channel.name)})
            if channel.private
            else frozenset({AclRef("group", "employees")})
        )
        for root_ts, msgs in threads(export.messages.get(channel.name, ())).items():
            msgs = sorted(msgs, key=lambda m: Decimal(m.ts))
            lines = [
                f"[{_stamp(m.ts)}] {names.get(m.user, m.user)}: {_one_line(m.text)}" for m in msgs
            ]
            first = _one_line(msgs[0].text)
            title = f"#{channel.name}: {first[:TITLE_CHARS]}"
            docs.append(
                SourceDocument(
                    connector=CONNECTOR,
                    external_id=f"chat:{channel.name}/{root_ts}",
                    source_kind="chat_thread",
                    title=title,
                    text=normalize_text("\n".join(lines)),
                    source_updated_at=ts_to_datetime(msgs[-1].ts),
                    acl=acl,
                    metadata={
                        "channel": channel.name,
                        "private": channel.private,
                        "thread_ts": root_ts,
                        "participants": sorted({names.get(m.user, m.user) for m in msgs}),
                        "messages": len(msgs),
                    },
                )
            )
    return docs


def channel_groups(export: SlackExport) -> list[ChannelGroup]:
    """Private channels and their members (person ids), for ``apply_chat_groups``."""
    names = {u.id: u.name for u in export.users}
    out: list[ChannelGroup] = []
    for c in export.channels:
        if c.private:
            out.append(
                ChannelGroup(c.name, c.created, tuple(sorted(names.get(m, m) for m in c.members)))
            )
    return out


def load(root: Path) -> tuple[list[SourceDocument], list[ChannelGroup]]:
    export = parse_export(root)
    return documents_from_export(export), channel_groups(export)


__all__ = [
    "ChannelGroup",
    "SlackChannel",
    "channel_groups",
    "documents_from_export",
    "load",
    "threads",
]
