"""Render the Kestrel Pay scenario into the corpus the importers read (design §5; plan 1.3).

    var/corpus/kestrel_pay/
      directory/directory.json
      chat/users.json  channels.json  groups.json  <channel>/<YYYY-MM-DD>.json
      meetings/<source-id>.md
      docs/<repo>/<path>
      manifest.json

The output is a pure function of the scenario file and ``as_of``: keys sorted, LF line ends, no
render timestamps, so the manifest's ``corpus_sha256`` is stable. GitHub objects (pull requests,
issues) are not rendered in Phase 1; the manifest lists them as skipped.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from collections import defaultdict
from pathlib import Path
from typing import Any, Final

from cairn_connectors.documents import DirectorySnapshot, DirGroup, DirMembership, DirUser
from cairn_connectors.formats.directory import print_directory
from cairn_connectors.formats.frontmatter import print_front_matter
from cairn_connectors.formats.slack_export import (
    SlackChannel,
    SlackExport,
    SlackMessage,
    SlackUser,
    print_export,
)
from cairn_eval.scenario.access import readers
from cairn_eval.scenario.canary import PLACEHOLDER_RE, canary_token
from cairn_eval.scenario.model import PrincipalRef, Scenario, Source

RENDERER_VERSION: Final = "r1"
RENDERABLE: Final = ("chat_thread", "meeting_note", "doc", "adr_file", "wiki")
CHANNEL_CREATED_EPOCH: Final = 1_735_689_600  # 2025-01-01T00:00:00Z


class RenderError(Exception):
    """The output folder is not usable, or a placeholder cannot be resolved."""


def slack_user_id(person: str) -> str:
    return "U" + hashlib.sha256(person.encode()).hexdigest()[:8].upper()


def _substitute(scenario: Scenario, text: str) -> str:
    families = {c.id: c.family for c in scenario.canaries if c.kind == "token"}

    def repl(match: Any) -> str:
        cid = match.group(1)
        if cid not in families:
            raise RenderError(f"unknown canary placeholder {cid!r}")
        return canary_token(scenario.meta.canary_seed, cid, families[cid])

    return PLACEHOLDER_RE.sub(repl, text)


def _status(scenario: Scenario, src: Source, as_of: dt.date) -> str:
    if src.kind not in RENDERABLE:
        return "skipped_github_object"
    if src.deleted is not None and src.deleted <= as_of:
        return "skipped_deleted"
    if src.available_or_date > as_of:
        return "skipped_future"
    return "rendered"


def _ref(ref: PrincipalRef) -> str:
    return f"team:{ref.team}" if ref.team is not None else f"user:{ref.person}"


def build_directory(scenario: Scenario, as_of: dt.date) -> DirectorySnapshot:
    users = tuple(DirUser(p.id, p.name, p.external) for p in scenario.people)
    groups = [DirGroup(f"team:{t.id}", t.name or t.id) for t in scenario.teams]
    groups.append(DirGroup("group:employees", "Employees"))
    memberships = [
        DirMembership(m.person, f"team:{m.team}", m.from_, m.to) for m in scenario.memberships
    ]
    earliest: dict[str, dt.date] = {}
    for m in scenario.memberships:
        earliest[m.person] = min(m.from_, earliest.get(m.person, m.from_))
    for p in scenario.people:
        if not p.external:
            start = earliest.get(p.id, scenario.meta.reference_date)
            memberships.append(DirMembership(p.id, "group:employees", start))
    return DirectorySnapshot(as_of, users, tuple(groups), tuple(memberships))


def build_export(scenario: Scenario, as_of: dt.date, rendered: list[Source]) -> SlackExport:
    users = tuple(SlackUser(slack_user_id(p.id), p.id, p.name) for p in scenario.people)
    channels = []
    for ch in scenario.channels:
        members = (
            sorted(slack_user_id(p) for p in readers(scenario, f"channel:{ch.id}", as_of))
            if ch.private
            else []
        )
        channels.append(
            SlackChannel(
                f"{'G' if ch.private else 'C'}{hashlib.sha256(ch.id.encode()).hexdigest()[:8].upper()}",  # noqa: E501
                ch.id,
                CHANNEL_CREATED_EPOCH,
                tuple(members),
                ch.private,
            )
        )
    messages: dict[str, list[SlackMessage]] = defaultdict(list)
    for src in rendered:
        if src.kind != "chat_thread" or src.location.channel is None or not src.messages:
            continue
        ordered = sorted(src.messages, key=lambda m: m.at)
        root_ts = f"{ordered[0].at.timestamp():.6f}"
        for m in ordered:
            messages[src.location.channel].append(
                SlackMessage(
                    slack_user_id(m.author),
                    _substitute(scenario, m.text),
                    f"{m.at.timestamp():.6f}",
                    root_ts,
                )
            )
    return SlackExport(
        users, tuple(channels), {c.name: tuple(messages.get(c.name, ())) for c in channels}
    )


def _doc_read(scenario: Scenario, repo_id: str) -> list[str]:
    repo = next(r for r in scenario.repos if r.id == repo_id)
    if repo.visibility == "public":
        return ["group:public"]
    return [_ref(g) for g in repo.access]


def _write(root: Path, rel: str, text: str, files: dict[str, str]) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")
    files[rel] = hashlib.sha256(text.encode("utf-8")).hexdigest()


def render_corpus(
    scenario: Scenario,
    out: Path,
    *,
    as_of: dt.date | None = None,
    overwrite: bool = False,
    scenario_sha256: str = "",
) -> dict[str, Any]:
    """Write the corpus under ``out`` and return the manifest."""
    as_of = as_of or scenario.meta.reference_date
    if out.exists() and any(out.iterdir()):
        if not overwrite:
            raise RenderError(f"{out} is not empty; pass --overwrite")
        import shutil

        shutil.rmtree(out)
    out.mkdir(parents=True, exist_ok=True)
    files: dict[str, str] = {}
    sources: dict[str, dict[str, str]] = {}
    rendered: list[Source] = []
    for src in scenario.sources:
        status = _status(scenario, src, as_of)
        info = {"status": status, "kind": src.kind}
        if status == "rendered":
            rendered.append(src)
        sources[src.id] = info

    _write(
        out, "directory/directory.json", print_directory(build_directory(scenario, as_of)), files
    )

    export = build_export(scenario, as_of, rendered)
    chat_root = out / "chat"
    print_export(export, chat_root)
    for p in sorted(chat_root.rglob("*.json")):
        rel = p.relative_to(out).as_posix()
        files[rel] = hashlib.sha256(p.read_bytes()).hexdigest()

    for src in rendered:
        if src.kind == "chat_thread":
            info = sources[src.id]
            info["external_id"] = "chat:{}/{}".format(
                src.location.channel, f"{sorted(m.at for m in src.messages)[0].timestamp():.6f}"
            )
            info["scope"] = src.scope or ""
        elif src.kind == "meeting_note" and src.location.meeting is not None:
            meeting = src.location.meeting
            meta: dict[str, object] = {
                "cairn_format": "meeting-note-v1",
                "id": src.id,
                "title": _substitute(scenario, src.title or src.id),
                "date": src.date.isoformat(),
                "recorded": src.available_or_date.isoformat(),
                "timezone": "UTC",
                "attendees": list(meeting.attendees),
                "share_with": [_ref(r) for r in meeting.share_with],
            }
            body = f"# {meta['title']}\n\n{_substitute(scenario, src.body or '')}\n"
            _write(out, f"meetings/{src.id}.md", print_front_matter(meta, body), files)
            sources[src.id].update(external_id=f"meeting:{src.id}", scope=src.scope or "")
        elif src.kind in ("doc", "adr_file", "wiki") and src.location.repo and src.location.path:
            meta = {
                "cairn_format": "doc-v1",
                "kind": src.kind,
                "repo": src.location.repo,
                "path": src.location.path,
                "title": _substitute(scenario, src.title or src.id),
                "author": src.author,
                "date": src.date.isoformat(),
                "updated": (src.last_edited or src.date).isoformat(),
                "read": _doc_read(scenario, src.location.repo),
            }
            body = f"# {meta['title']}\n\n{_substitute(scenario, src.body or '')}\n"
            _write(
                out,
                f"docs/{src.location.repo}/{src.location.path}",
                print_front_matter(meta, body),
                files,
            )
            sources[src.id].update(
                external_id=f"doc:{src.location.repo}/{src.location.path}", scope=src.scope or ""
            )

    digest = hashlib.sha256()
    for rel in sorted(files):
        digest.update(f"{rel}\0{files[rel]}\n".encode())
    manifest = {
        "format": "cairn-corpus-v1",
        "scenario_sha256": scenario_sha256,
        "renderer_version": RENDERER_VERSION,
        "as_of": as_of.isoformat(),
        "corpus_sha256": digest.hexdigest(),
        "files": dict(sorted(files.items())),
        "sources": dict(sorted(sources.items())),
    }
    (out / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    leftovers = [
        p
        for p in out.rglob("*")
        if p.is_file() and "{{canary:" in p.read_text(encoding="utf-8", errors="ignore")
    ]
    if leftovers:
        raise RenderError(f"{len(leftovers)} file(s) still hold a canary placeholder")
    return manifest
