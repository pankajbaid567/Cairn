"""Live sync from GitHub into Cairn (plan 3.5 to 3.11, 3.28).

The model is *fetch-on-notify*: a webhook row in the inbox only says "something changed here", and
the drainer then reads the **current** state from GitHub. So duplicates are harmless, delivery order
does not matter and a missed event is repaired by the next read. Two classes of work:

* ``acl`` rows (membership, team, collaborator, visibility, installation) run first. One permission
  sync re-reads the whole organisation's access state, so many ACL events cost one pass. Only rows
  received *before the pass began* are marked applied; later ones stay pending and keep the query
  barrier closed (drain-before-serve).
* ``content`` rows (pull requests, issues, Markdown on push) wait until no ACL row is pending, so a
  revocation always lands before new content is written (the new-enemy rule), and each item's ACL
  is computed from the repository's access as GitHub reports it at read time.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final

import psycopg

from cairn_connectors.decisions import store as decision_store
from cairn_connectors.documents import AclRef
from cairn_connectors.github import documents as gdoc
from cairn_connectors.github.client import GitHubClient, GitHubError, NotFound
from cairn_connectors.github.models import GhRepo
from cairn_connectors.store import (
    _bump_acl_version,
    _ensure_principals,
    _range_text,
    _sync_acl,
    _sync_memberships,
    import_document,
    resolve_acl,
)
from cairn_retrieval.models.cache import ModelCache

log = logging.getLogger("cairn.sync")
Conn = psycopg.Connection[Any]

READ_PERMISSIONS: Final = frozenset({"pull", "triage", "push", "maintain", "admin"})
MAX_ATTEMPTS: Final = 10
MEMBERSHIP_SOURCE: Final = "github"
NOBODY: Final = AclRef("group", "nobody")
"""A group with no members: the ACL of an item whose repository has no readers."""


def person_id(login: str, user_map: Mapping[str, str] | None) -> str:
    """The principal id for a GitHub login: the mapped person, else the lower-cased login."""
    mapped = (user_map or {}).get(login) or (user_map or {}).get(login.lower())
    return (mapped or login).lower()


@dataclass
class PermissionResult:
    memberships_added: int = 0
    memberships_removed: int = 0
    items_reacled: int = 0
    purged_items: int = 0
    repos: int = 0


@dataclass
class ContentResult:
    imported: int = 0
    unchanged: int = 0
    purged: int = 0
    errors: list[str] = field(default_factory=list)


@dataclass
class DrainStats:
    acl_applied: int = 0
    content_applied: int = 0
    failed: int = 0
    permissions: PermissionResult | None = None
    waited_on_acl: bool = False


def repo_acl(
    gh: GitHubClient, org: str, repo: GhRepo, user_map: Mapping[str, str] | None
) -> frozenset[AclRef]:
    """Who may read a repository: everyone if public, else its teams and direct collaborators."""
    if not repo.private:
        return frozenset({AclRef("group", "public")})
    refs: set[AclRef] = set()
    for team in gh.list_repo_teams(org, repo.name):
        if team.permission in READ_PERMISSIONS:
            refs.add(AclRef("team", team.slug.lower()))
    for collab in gh.list_collaborators(org, repo.name):
        refs.add(AclRef("user", person_id(collab.login, user_map)))
    # A private repo nobody can read fails closed: its items go to a group with no members.
    return frozenset(refs) or frozenset({NOBODY})


# --- freshness ---------------------------------------------------------------------------------


def touch_source(
    conn: Conn,
    key: str,
    *,
    event_at: datetime | None = None,
    error: str | None = None,
    synced: bool = True,
) -> None:
    conn.execute(
        "INSERT INTO cairn.sync_sources (source_key, kind, last_event_at, last_synced_at, last_error) "  # noqa: E501
        "VALUES (%s, 'github', %s, CASE WHEN %s THEN now() END, %s) "
        "ON CONFLICT (source_key) DO UPDATE SET "
        "last_event_at = coalesce(EXCLUDED.last_event_at, cairn.sync_sources.last_event_at), "
        "last_synced_at = CASE WHEN %s THEN now() ELSE cairn.sync_sources.last_synced_at END, "
        "last_error = EXCLUDED.last_error",
        (key, event_at, synced, error, synced),
    )


# --- permission sync (plan 3.7) ----------------------------------------------------------------


def sync_permissions(
    conn: Conn, gh: GitHubClient, org: str, user_map: Mapping[str, str] | None = None
) -> PermissionResult:
    """Make principals, team memberships and every GitHub item's ACL equal GitHub's current state."""  # noqa: E501
    result = PermissionResult()
    members = {m: person_id(m, user_map) for m in gh.list_org_members(org)}
    teams = {t.slug.lower(): t for t in gh.list_teams(org)}
    team_members = {slug: list(gh.list_team_members(org, slug)) for slug in teams}
    repos = list(gh.list_repos(org))
    acls = {r.name: repo_acl(gh, org, r, user_map) for r in repos}
    # Everything below is one transaction: a permission sync is never half-visible.
    with conn.transaction():
        wanted: dict[str, tuple[str, bool]] = {NOBODY.external_id: ("Nobody", False)}
        for login, pid in members.items():
            wanted[f"user:{pid}"] = (login, False)
        for slug, team in teams.items():
            wanted[f"team:{slug}"] = (team.name, False)
        for acl in acls.values():  # repository access can name users and teams outside the lists
            for ref in acl:
                if ref.external_id not in wanted and ref.kind in ("user", "team"):
                    wanted[ref.external_id] = (ref.id, ref.kind == "user")
        ids, _, _ = _ensure_principals(conn, wanted)
        existing = {
            (int(r[0]), int(r[1])): str(r[2])
            for r in conn.execute(
                "SELECT member_id, group_id, valid_period::text FROM cairn.memberships WHERE source = %s",  # noqa: E501
                (MEMBERSHIP_SOURCE,),
            ).fetchall()
        }
        now = datetime.now(UTC)
        desired: dict[tuple[int, int, str], tuple[datetime, datetime | None]] = {}
        for slug, logins in team_members.items():
            group_id = ids[f"team:{slug}"]
            for login in logins:
                member_id = ids.get(f"user:{person_id(login, user_map)}")
                if member_id is None:
                    continue
                if (member_id, group_id) in existing:  # keep the first-seen start
                    rng = existing[(member_id, group_id)]
                    desired[(member_id, group_id, rng)] = (now, None)  # start text is the key
                else:
                    desired[(member_id, group_id, _range_text(conn, now, None))] = (now, None)
        scope = {ids[f"team:{slug}"] for slug in teams}
        # rows whose stored range text we kept must not be re-inserted: they match `have` exactly
        added, removed = (
            _sync_memberships(conn, MEMBERSHIP_SOURCE, desired, scope) if scope else (0, 0)
        )
        result.memberships_added, result.memberships_removed = added, removed
        if added or removed:
            _bump_acl_version(conn, "github")
        # Items: re-ACL what changed; purge what no longer exists.
        live = {r.name for r in repos}
        for r in repos:
            key = gdoc.source_key(org, r.name)
            refs = acls[r.name]
            resolved = resolve_acl(conn, refs)
            if not isinstance(resolved, tuple):
                log.warning("sync.repo_acl_unresolved", extra={"repo": r.name})
                continue
            for item_id, existing_acl in conn.execute(
                "SELECT item_id, acl FROM cairn.items WHERE source_key = %s", (key,)
            ).fetchall():
                if sorted(int(a) for a in existing_acl) != list(resolved):
                    _sync_acl(conn, int(item_id), resolved)
                    decision_store.refresh_acl_copies(conn, int(item_id), list(resolved))
                    decision_store.mark_stale(conn, int(item_id))
                    result.items_reacled += 1
            touch_source(conn, key)
        prefix = f"github:{org}/"
        gone = [
            int(row[0])
            for row in conn.execute(
                "SELECT item_id, source_key FROM cairn.items WHERE source_key LIKE %s",
                (prefix + "%",),
            ).fetchall()
            if str(row[1])[len(prefix) :] not in live
        ]
        if gone:
            result.purged_items = purge_items(conn, gone).items
    result.repos = len(repos)
    return result


# --- content (plan 3.5, 3.6) -------------------------------------------------------------------


def _find_repo(gh: GitHubClient, org: str, name: str) -> GhRepo | None:
    return next((r for r in gh.list_repos(org) if r.name == name), None)


def sync_repo_content(
    conn: Conn,
    gh: GitHubClient,
    org: str,
    repo_name: str,
    *,
    event: str,
    number: int | None = None,
    action: str | None = None,
    user_map: Mapping[str, str] | None = None,
    cache: ModelCache | None = None,
) -> ContentResult:
    """Fetch the current state of what a content event names and write it in one transaction each."""  # noqa: E501
    result = ContentResult()
    repo = _find_repo(gh, org, repo_name)
    if repo is None:  # the repository is gone: the permission sync purges it
        return result
    acl = repo_acl(gh, org, repo, user_map)
    key = gdoc.source_key(org, repo_name)

    def put(doc: Any) -> None:
        out = import_document(conn, doc)
        if out.error:
            result.errors.append(f"{doc.external_id}: {out.error}")
        elif out.revision is not None and out.revision.name == "INSERT":
            result.imported += 1
            _bump_acl_version(conn, "content")
        else:
            result.unchanged += 1

    if event == "push":
        files = list(gh.list_markdown(org, repo_name))
        for f in files:
            put(gdoc.markdown_document(org, repo_name, f, acl))
        present = {gdoc.file_id(org, repo_name, f.path) for f in files}
        stale = [
            int(r[0])
            for r in conn.execute(
                "SELECT item_id, external_id FROM cairn.items WHERE source_key = %s "
                "AND external_id LIKE %s",
                (key, f"github:{org}/{repo_name}/blob/%"),
            ).fetchall()
            if str(r[1]) not in present
        ]
        if stale:
            result.purged += purge_items(conn, stale, cache=cache).items
    elif number is not None:
        ids = (gdoc.pull_id(org, repo_name, number), gdoc.issue_id(org, repo_name, number))
        try:
            if event.startswith("pull_request") or event == "issue_comment":
                try:
                    put(
                        gdoc.pull_document(org, repo_name, gh.get_pull(org, repo_name, number), acl)
                    )
                except NotFound:
                    if event != "issue_comment":
                        raise
                    put(
                        gdoc.issue_document(
                            org, repo_name, gh.get_issue(org, repo_name, number), acl
                        )
                    )
            else:
                put(gdoc.issue_document(org, repo_name, gh.get_issue(org, repo_name, number), acl))
        except NotFound:  # deleted on GitHub
            gone = [
                int(r[0])
                for r in conn.execute(
                    "SELECT item_id FROM cairn.items WHERE external_id = ANY(%s)", (list(ids),)
                ).fetchall()
            ]
            if gone:
                result.purged += purge_items(conn, gone, cache=cache).items
    touch_source(conn, key)
    return result


# --- deletion purge (plan 3.11) ----------------------------------------------------------------


@dataclass
class PurgeResult:
    items: int = 0
    chunks: int = 0
    embeddings: int = 0
    cache_rows: int = 0
    claim_supports: int = 0
    events: int = 0
    decisions_rebuilt: int = 0


def purge_items(
    conn: Conn, item_ids: Sequence[int], *, cache: ModelCache | None = None
) -> PurgeResult:
    """Remove items and everything derived from them.

    Derived artifacts are marked stale first (they need the lineage rows that are deleted below);
    then the claim support, lineage, decision events from these sources, embeddings, chunks,
    revisions, pending embed jobs, ACL rows and the items go. Decisions whose events came from the
    source have their version rectangles rebuilt from what remains; a claim left with no support
    set is invisible to everyone. Audit rows keep only hashes and item ids, never content, and are
    retained (docs/sync.md, audit retention).
    """
    out = PurgeResult()
    ids = sorted(set(item_ids))
    if not ids:
        return out
    for item_id in ids:
        decision_store.mark_stale(conn, item_id)
    if cache is not None:
        texts = [
            str(r[0])
            for r in conn.execute(
                "SELECT text FROM cairn.chunks WHERE item_id = ANY(%s)", (ids,)
            ).fetchall()
        ]
        out.cache_rows = cache.forget_texts(texts)
    out.claim_supports = conn.execute(
        "DELETE FROM cairn.claim_support WHERE item_id = ANY(%s)", (ids,)
    ).rowcount
    conn.execute("DELETE FROM cairn.lineage WHERE input_item_id = ANY(%s)", (ids,))
    affected = [
        int(r[0])
        for r in conn.execute(
            "SELECT DISTINCT decision_id FROM cairn.decision_events WHERE source_item_id = ANY(%s)",
            (ids,),
        ).fetchall()
    ]
    out.events = conn.execute(
        "DELETE FROM cairn.decision_events WHERE source_item_id = ANY(%s)", (ids,)
    ).rowcount
    for decision_id in affected:
        decision_store.rebuild_versions(conn, decision_id)
        out.decisions_rebuilt += 1
    conn.execute(
        "DELETE FROM cairn.outbox WHERE revision_id IN (SELECT revision_id FROM cairn.revisions WHERE item_id = ANY(%s))",  # noqa: E501
        (ids,),
    )
    out.embeddings = conn.execute(
        "DELETE FROM cairn.embeddings WHERE chunk_id IN (SELECT chunk_id FROM cairn.chunks WHERE item_id = ANY(%s))",  # noqa: E501
        (ids,),
    ).rowcount
    out.chunks = conn.execute("DELETE FROM cairn.chunks WHERE item_id = ANY(%s)", (ids,)).rowcount
    conn.execute("DELETE FROM cairn.revisions WHERE item_id = ANY(%s)", (ids,))
    conn.execute("DELETE FROM cairn.item_acls WHERE item_id = ANY(%s)", (ids,))
    out.items = conn.execute("DELETE FROM cairn.items WHERE item_id = ANY(%s)", (ids,)).rowcount
    _bump_acl_version(conn, "purge")
    return out


# --- the drainer (plan 3.5, 3.8, 3.28) ---------------------------------------------------------


def _pending(conn: Conn, klass: str, before: Any, limit: int) -> list[tuple[Any, ...]]:
    return conn.execute(
        "SELECT delivery_id, event, action, repo_name, object_number, received_at, attempts "
        "FROM cairn.inbox WHERE applied_at IS NULL AND class = %s AND received_at <= %s AND attempts < %s "  # noqa: E501
        "ORDER BY priority, received_at LIMIT %s",
        (klass, before, MAX_ATTEMPTS, limit),
    ).fetchall()


def _mark(conn: Conn, ids: Sequence[str], error: str | None = None) -> None:
    if error is None:
        conn.execute(
            "UPDATE cairn.inbox SET applied_at = now(), last_error = NULL WHERE delivery_id = ANY(%s)",  # noqa: E501
            (list(ids),),
        )
    else:
        conn.execute(
            "UPDATE cairn.inbox SET attempts = attempts + 1, last_error = %s WHERE delivery_id = ANY(%s)",  # noqa: E501
            (error[:300], list(ids)),
        )


def drain(
    conn: Conn,
    gh: GitHubClient,
    org: str,
    *,
    user_map: Mapping[str, str] | None = None,
    max_rows: int = 200,
    cache: ModelCache | None = None,
) -> DrainStats:
    """Apply pending inbox rows: ACL class first, content only when no ACL row is pending."""
    stats = DrainStats()
    cutoff = conn.execute("SELECT clock_timestamp()").fetchone()
    assert cutoff is not None  # noqa: S101
    conn.commit()
    acl_rows = _pending(conn, "acl", cutoff[0], max_rows)
    if acl_rows:
        ids = [str(r[0]) for r in acl_rows]
        try:
            stats.permissions = sync_permissions(conn, gh, org, user_map)
            _mark(conn, ids)
            stats.acl_applied = len(ids)
        except GitHubError as exc:
            conn.rollback()
            _mark(conn, ids, f"github {exc.status}: {exc}")
            stats.failed += len(ids)
        conn.commit()
    left = conn.execute(
        "SELECT count(*) FROM cairn.inbox WHERE applied_at IS NULL AND class = 'acl'"
    ).fetchone()
    conn.commit()
    if left is not None and int(left[0]) > 0:
        stats.waited_on_acl = True  # content waits for ACL rows (new-enemy rule)
        return stats
    for delivery, event, action, repo_name, number, received, _attempts in _pending(
        conn, "content", cutoff[0], max_rows
    ):
        if repo_name is None:
            _mark(conn, [delivery])
            conn.commit()
            continue
        bare = repo_name.split("/", 1)[-1]  # the inbox holds "org/repo"
        try:
            sync_repo_content(
                conn,
                gh,
                org,
                bare,
                event=event,
                action=action,
                number=number,
                user_map=user_map,
                cache=cache,
            )
            _mark(conn, [delivery])
            touch_source(conn, gdoc.source_key(org, bare), event_at=received)
            stats.content_applied += 1
        except GitHubError as exc:
            conn.rollback()
            _mark(conn, [delivery], f"github {exc.status}: {exc}")
            touch_source(
                conn, gdoc.source_key(org, bare), error=f"github {exc.status}", synced=False
            )
            stats.failed += 1
        conn.commit()
    return stats


# --- reconciliation (plan 3.10) ----------------------------------------------------------------


@dataclass
class ReconcileResult:
    redelivered: int = 0
    backfilled: int = 0
    errors: list[str] = field(default_factory=list)


def reconcile(
    conn: Conn,
    gh: GitHubClient,
    org: str,
    *,
    since: datetime | None = None,
    max_redeliveries: int = 50,
) -> ReconcileResult:
    """Recover what webhooks missed: redeliver failed deliveries and diff object lists.

    GitHub keeps deliveries for a short window (Verify), so redelivery covers recent failures; the
    object diff covers everything else, and enqueues missing work at backfill priority (2) so it
    never delays live events.
    """
    out = ReconcileResult()
    known = {str(r[0]) for r in conn.execute("SELECT delivery_id FROM cairn.inbox").fetchall()}
    try:
        for d in gh.list_deliveries(since):
            if not 200 <= d.status_code < 300 and out.redelivered < max_redeliveries:
                gh.redeliver(d.id)
                out.redelivered += 1
    except GitHubError as exc:
        out.errors.append(f"deliveries: {exc}")
    stamp = int(datetime.now(UTC).timestamp())
    try:
        for repo in gh.list_repos(org):
            key = gdoc.source_key(org, repo.name)
            have = {
                str(r[0])
                for r in conn.execute(
                    "SELECT external_id FROM cairn.items WHERE source_key = %s", (key,)
                ).fetchall()
            }
            want: list[tuple[str, str, int | None]] = []
            for n in gh.list_pulls(org, repo.name):
                if gdoc.pull_id(org, repo.name, n) not in have:
                    want.append(("pull_request", repo.name, n))
            for n in gh.list_issues(org, repo.name):
                if gdoc.issue_id(org, repo.name, n) not in have:
                    want.append(("issues", repo.name, n))
            files = {gdoc.file_id(org, repo.name, f.path) for f in gh.list_markdown(org, repo.name)}
            have_files = {h for h in have if "/blob/" in h}
            if files != have_files:
                want.append(("push", repo.name, None))
            for want_event, name, num in want:
                delivery = f"reconcile-{want_event}-{name}-{num or 0}-{stamp}"
                if delivery in known:
                    continue
                conn.execute(
                    "INSERT INTO cairn.inbox (delivery_id, event, class, priority, repo_name, object_number) "  # noqa: E501
                    "VALUES (%s, %s, 'content', 2, %s, %s) ON CONFLICT DO NOTHING",
                    (delivery, want_event, f"{org}/{name}", num),
                )
                out.backfilled += 1
    except GitHubError as exc:
        out.errors.append(f"objects: {exc}")
    conn.commit()
    return out
