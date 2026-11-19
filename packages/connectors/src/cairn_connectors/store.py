"""Writes content, ACLs and the directory with the ``cairn_ingest`` role (design §8, PD1-05..10).

One transaction per document. The item row is locked by the upsert, so two importers cannot
interleave on one item. ``items.acl``, ``revisions.acl``, ``chunks.acl`` and ``embeddings.acl``
are sorted, distinct copies of ``item_acls``, updated in the same transaction as any ACL change
(Property 9). An unknown ACL reference rejects the whole document: a narrower ACL would hide it
silently, a rejection is loud.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

import psycopg

from cairn_connectors.chat_export import ChannelGroup
from cairn_connectors.chunking import Chunk, chunk_chat, chunk_markdown
from cairn_connectors.decisions import store as decision_store
from cairn_connectors.documents import AclRef, DirectorySnapshot, SourceDocument
from cairn_connectors.normalize import (
    LatestRevision,
    RevisionPlan,
    content_hash,
    normalize_text,
    plan_revision,
)

Chunker = Callable[[str], list[Chunk]]
ACL_UNRESOLVED: Final = "acl_unresolved"
ACL_EMPTY: Final = "acl_empty"

Conn = psycopg.Connection[Any]


@dataclass(frozen=True)
class AclError:
    code: str
    missing: tuple[str, ...] = ()


@dataclass(frozen=True)
class ImportOutcome:
    external_id: str
    revision: RevisionPlan | None  # None when rejected for ACL reasons
    acl_changed: bool = False
    error: str | None = None
    revision_id: int | None = None
    chunks: int = 0


@dataclass(frozen=True)
class DirectoryOutcome:
    principals_added: int = 0
    principals_updated: int = 0
    memberships_added: int = 0
    memberships_removed: int = 0

    @property
    def changed(self) -> bool:
        return bool(
            self.principals_added
            or self.principals_updated
            or self.memberships_added
            or self.memberships_removed
        )


def default_chunker(doc: SourceDocument) -> Chunker:
    return chunk_chat if doc.source_kind == "chat_thread" else chunk_markdown


# --- ACL resolution -------------------------------------------------------------------------


def resolve_acl(conn: Conn, refs: Iterable[AclRef]) -> tuple[int, ...] | AclError:
    """Sorted principal ids for ``refs``, or an :class:`AclError` (unknown refs, or empty)."""
    wanted = sorted({r.external_id for r in refs})
    if not wanted:
        return AclError(ACL_EMPTY)
    rows = conn.execute(
        "SELECT external_id, principal_id FROM cairn.principals WHERE external_id = ANY(%s)",
        (wanted,),
    ).fetchall()
    found = {str(r[0]): int(r[1]) for r in rows}
    missing = tuple(w for w in wanted if w not in found)
    if missing:
        return AclError(ACL_UNRESOLVED, missing)
    return tuple(sorted(found.values()))


def _bump_acl_version(conn: Conn, reason: str) -> None:
    conn.execute("INSERT INTO cairn.acl_versions (reason) VALUES (%s)", (reason,))


def _sync_acl(conn: Conn, item_id: int, acl: tuple[int, ...]) -> bool:
    """Make item_acls and every denormalized copy equal ``acl``. True when it changed."""
    rows = conn.execute(
        "SELECT principal_id FROM cairn.item_acls WHERE item_id = %s", (item_id,)
    ).fetchall()
    existing = sorted(int(r[0]) for r in rows)
    if existing == list(acl):
        return False
    conn.execute(
        "DELETE FROM cairn.item_acls WHERE item_id = %s AND NOT (principal_id = ANY(%s))",
        (item_id, list(acl)),
    )
    for principal_id in acl:
        if principal_id not in existing:
            conn.execute(
                "INSERT INTO cairn.item_acls (item_id, principal_id) VALUES (%s, %s)",
                (item_id, principal_id),
            )
    conn.execute("UPDATE cairn.items SET acl = %s::int[] WHERE item_id = %s", (list(acl), item_id))
    conn.execute(
        "UPDATE cairn.revisions SET acl = %s::int[] WHERE item_id = %s", (list(acl), item_id)
    )
    conn.execute("UPDATE cairn.chunks SET acl = %s::int[] WHERE item_id = %s", (list(acl), item_id))
    conn.execute(
        "UPDATE cairn.embeddings SET acl = %s::int[] "
        "WHERE chunk_id IN (SELECT chunk_id FROM cairn.chunks WHERE item_id = %s)",
        (list(acl), item_id),
    )
    if existing:  # a new item is not a permission change
        _bump_acl_version(conn, "item_acl")
    return bool(existing)


# --- documents ------------------------------------------------------------------------------


def import_document(
    conn: Conn, doc: SourceDocument, *, chunker: Chunker | None = None
) -> ImportOutcome:
    """Import one document in one transaction. ``conn`` must not be in autocommit mode."""
    normalized = normalize_text(doc.text)
    resolved = resolve_acl(conn, doc.acl)
    if isinstance(resolved, AclError):
        conn.rollback()
        return ImportOutcome(doc.external_id, None, error=resolved.code)
    acl = resolved
    new_hash = content_hash(normalized)
    chunk_fn: Chunker = chunker or default_chunker(doc)
    with conn.transaction():
        row = conn.execute(
            "INSERT INTO cairn.items (connector, external_id, source_kind, title, acl, source_key) "
            "VALUES (%s, %s, %s, %s, %s::int[], %s) "
            "ON CONFLICT (connector, external_id) DO UPDATE SET title = EXCLUDED.title "
            "RETURNING item_id",
            (
                doc.connector,
                doc.external_id,
                doc.source_kind,
                doc.title,
                list(acl),
                doc.source_key or f"import:{doc.connector}",
            ),
        ).fetchone()
        assert row is not None  # noqa: S101
        item_id = int(row[0])
        latest_row = conn.execute(
            "SELECT content_hash, source_updated_at FROM cairn.revisions "
            "WHERE item_id = %s ORDER BY revision_id DESC LIMIT 1",
            (item_id,),
        ).fetchone()
        latest = LatestRevision(str(latest_row[0]), latest_row[1]) if latest_row else None
        plan = plan_revision(latest, new_hash, doc.source_updated_at)
        acl_changed = _sync_acl(conn, item_id, acl)
        if acl_changed:
            # Decision tables keep ACL copies equal to the item's (like chunks) and artifacts derived  # noqa: E501
            # from the item rebuild on next read (plan 2.12).
            decision_store.refresh_acl_copies(conn, item_id, list(acl))
            decision_store.mark_stale(conn, item_id)
        if plan is RevisionPlan.INSERT:
            revision_id, n_chunks = _insert_revision(
                conn, item_id, doc, normalized, new_hash, acl, chunk_fn
            )
            if latest is not None:  # an edit, not a first import
                decision_store.mark_stale(conn, item_id)
            return ImportOutcome(doc.external_id, plan, acl_changed, None, revision_id, n_chunks)
        if plan is RevisionPlan.SKIP_UNCHANGED:
            conn.execute(
                "UPDATE cairn.chunks SET title = %s WHERE item_id = %s AND is_current "
                "AND title <> %s",
                (doc.title, item_id, doc.title),
            )
        return ImportOutcome(doc.external_id, plan, acl_changed)


def _insert_revision(
    conn: Conn,
    item_id: int,
    doc: SourceDocument,
    text: str,
    digest: str,
    acl: tuple[int, ...],
    chunker: Chunker,
) -> tuple[int, int]:
    import json

    row = conn.execute(
        "INSERT INTO cairn.revisions (item_id, content_hash, text, metadata, source_updated_at, acl)"  # noqa: E501
        " VALUES (%s, %s, %s, %s::jsonb, %s, %s::int[]) RETURNING revision_id",
        (
            item_id,
            digest,
            text,
            json.dumps(dict(doc.metadata), sort_keys=True),
            doc.source_updated_at,
            list(acl),
        ),
    ).fetchone()
    assert row is not None  # noqa: S101
    revision_id = int(row[0])
    # PD1-23: only current chunks have embeddings.
    conn.execute(
        "DELETE FROM cairn.embeddings WHERE chunk_id IN "
        "(SELECT chunk_id FROM cairn.chunks WHERE item_id = %s AND is_current)",
        (item_id,),
    )
    conn.execute(
        "UPDATE cairn.chunks SET is_current = false WHERE item_id = %s AND is_current", (item_id,)
    )
    chunks = chunker(text)
    for c in chunks:
        conn.execute(
            "INSERT INTO cairn.chunks (item_id, revision_id, ordinal, start_offset, end_offset, "
            "text, heading_path, token_estimate, source_kind, title, is_current, acl) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s::text[], %s, %s, %s, true, %s::int[])",
            (
                item_id,
                revision_id,
                c.ordinal,
                c.start,
                c.end,
                c.text,
                list(c.heading_path),
                c.token_estimate,
                doc.source_kind,
                doc.title,
                list(acl),
            ),
        )
    conn.execute(
        "UPDATE cairn.items SET current_revision_id = %s, title = %s, updated_at = now() "
        "WHERE item_id = %s",
        (revision_id, doc.title, item_id),
    )
    if chunks:
        conn.execute(
            "INSERT INTO cairn.outbox (kind, revision_id) VALUES ('embed_revision', %s)",
            (revision_id,),
        )
    return revision_id, len(chunks)


# --- directory and chat groups --------------------------------------------------------------


def _kind_of(external_id: str) -> str:
    return external_id.split(":", 1)[0]


def _ensure_principals(
    conn: Conn, wanted: dict[str, tuple[str, bool]]
) -> tuple[dict[str, int], int, int]:
    """Insert missing principals, update changed names. ``wanted``: external id -> (name, external)."""  # noqa: E501
    rows = conn.execute(
        "SELECT external_id, principal_id, display_name, is_external FROM cairn.principals "
        "WHERE external_id = ANY(%s)",
        (sorted(wanted),),
    ).fetchall()
    existing = {str(r[0]): (int(r[1]), str(r[2]), bool(r[3])) for r in rows}
    added = updated = 0
    ids: dict[str, int] = {k: v[0] for k, v in existing.items()}
    for external_id, (name, external) in sorted(wanted.items()):
        if external_id not in existing:
            row = conn.execute(
                "INSERT INTO cairn.principals (external_id, kind, display_name, is_external) "
                "VALUES (%s, %s, %s, %s) RETURNING principal_id",
                (external_id, _kind_of(external_id), name, external),
            ).fetchone()
            assert row is not None  # noqa: S101
            ids[external_id] = int(row[0])
            added += 1
        elif (existing[external_id][1], existing[external_id][2]) != (name, external):
            conn.execute(
                "UPDATE cairn.principals SET display_name = %s, is_external = %s "
                "WHERE external_id = %s",
                (name, external, external_id),
            )
            updated += 1
    return ids, added, updated


def _sync_memberships(
    conn: Conn,
    source: str,
    desired: dict[tuple[int, int, str], tuple[datetime, datetime | None]],
    scope_groups: set[int],
) -> tuple[int, int]:
    """Make the membership rows of ``source`` (limited to ``scope_groups``) equal ``desired``.

    ``desired`` maps (member_id, group_id, range text) to (start, end). Changed ranges are a
    delete plus an insert, deletes first, so the temporal key never sees an overlap.
    """
    rows = conn.execute(
        "SELECT member_id, group_id, valid_period::text FROM cairn.memberships "
        "WHERE source = %s AND group_id = ANY(%s)",
        (source, sorted(scope_groups)),
    ).fetchall()
    have = {(int(r[0]), int(r[1]), str(r[2])) for r in rows}
    want = set(desired)
    removed = 0
    for member_id, group_id, rng in sorted(have - want):
        conn.execute(
            "DELETE FROM cairn.memberships WHERE member_id = %s AND group_id = %s "
            "AND valid_period::text = %s AND source = %s",
            (member_id, group_id, rng, source),
        )
        removed += 1
    added = 0
    for key in sorted(want - have):
        member_id, group_id, _ = key
        start, end = desired[key]
        conn.execute(
            "INSERT INTO cairn.memberships (member_id, group_id, valid_period, source) "
            "VALUES (%s, %s, tstzrange(%s, %s, '[)'), %s)",
            (member_id, group_id, start, end, source),
        )
        added += 1
    return added, removed


def _range_text(conn: Conn, start: datetime, end: datetime | None) -> str:
    row = conn.execute("SELECT tstzrange(%s, %s, '[)')::text", (start, end)).fetchone()
    assert row is not None  # noqa: S101
    return str(row[0])


def _midnight(day: Any) -> datetime:
    return datetime.combine(day, datetime.min.time(), UTC)


def apply_directory(conn: Conn, snapshot: DirectorySnapshot) -> DirectoryOutcome:
    """Diff principals and memberships against the snapshot. Running it twice changes nothing."""
    with conn.transaction():
        wanted: dict[str, tuple[str, bool]] = {}
        for user in snapshot.users:
            wanted[f"user:{user.id}"] = (user.name, user.external)
        for group in snapshot.groups:
            wanted[group.id] = (group.name, False)
        ids, p_added, p_updated = _ensure_principals(conn, wanted)
        desired: dict[tuple[int, int, str], tuple[datetime, datetime | None]] = {}
        for m in snapshot.memberships:
            start, end = _midnight(m.start), _midnight(m.end) if m.end else None
            key = (ids[f"user:{m.user}"], ids[m.group], _range_text(conn, start, end))
            desired[key] = (start, end)
        scope = {ids[g.id] for g in snapshot.groups}
        added, removed = _sync_memberships(conn, "directory", desired, scope)
        outcome = DirectoryOutcome(p_added, p_updated, added, removed)
        if outcome.changed:
            _bump_acl_version(conn, "directory")
        return outcome


def apply_chat_groups(conn: Conn, groups: Iterable[ChannelGroup]) -> DirectoryOutcome:
    """Private channels become ``channel:<name>`` principals whose members come from the export.

    Members must already exist as ``user:<id>`` principals (apply the directory first).
    """
    groups = list(groups)
    with conn.transaction():
        wanted = {f"channel:{g.name}": (f"#{g.name}", False) for g in groups}
        ids, p_added, p_updated = _ensure_principals(conn, wanted) if wanted else ({}, 0, 0)
        user_ids = {
            str(r[0]): int(r[1])
            for r in conn.execute(
                "SELECT external_id, principal_id FROM cairn.principals WHERE kind = 'user'"
            ).fetchall()
        }
        desired: dict[tuple[int, int, str], tuple[datetime, datetime | None]] = {}
        for g in groups:
            start = datetime.fromtimestamp(g.created, UTC)
            for person in g.members:
                if f"user:{person}" not in user_ids:
                    raise ValueError(f"channel {g.name!r} lists unknown member {person!r}")
                key = (
                    user_ids[f"user:{person}"],
                    ids[f"channel:{g.name}"],
                    _range_text(conn, start, None),
                )
                desired[key] = (start, None)
        scope = {ids[f"channel:{g.name}"] for g in groups}
        added, removed = _sync_memberships(conn, "chat_export", desired, scope) if scope else (0, 0)
        outcome = DirectoryOutcome(p_added, p_updated, added, removed)
        if outcome.changed:
            _bump_acl_version(conn, "chat_groups")
        return outcome
