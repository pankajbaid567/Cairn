"""GitHub objects to ``SourceDocument`` (plan 3.6)."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Final

from cairn_connectors.documents import AclRef, SourceDocument, SourceKind
from cairn_connectors.github.models import GhComment, GhFile, GhIssue, GhPull
from cairn_connectors.normalize import normalize_text

DECISION_LABELS: Final = frozenset({"decision", "adr", "architecture-decision", "design-decision"})
_ADR_PATH: Final = re.compile(r"(^|/)(adr|adrs|decisions?)([-_/]|$)|(^|/)adr-?\d+", re.IGNORECASE)


def source_key(org: str, repo: str) -> str:
    return f"github:{org}/{repo}"


def pull_id(org: str, repo: str, number: int) -> str:
    return f"github:{org}/{repo}/pull/{number}"


def issue_id(org: str, repo: str, number: int) -> str:
    return f"github:{org}/{repo}/issues/{number}"


def file_id(org: str, repo: str, path: str) -> str:
    return f"github:{org}/{repo}/blob/{path}"


def is_decision_pr(labels: tuple[str, ...]) -> bool:
    """A pull request or issue labelled as a decision is a decision candidate (plan 3.6)."""
    return any(label.casefold() in DECISION_LABELS for label in labels)


def _thread(heading: str, items: tuple[GhComment, ...]) -> str:
    if not items:
        return ""
    lines = [f"\n\n## {heading}\n"]
    lines += [f"**{c.author}** ({c.at.date().isoformat()}): {c.body.strip()}\n" for c in items]
    return "\n".join(lines)


def pull_document(org: str, repo: str, pull: GhPull, acl: frozenset[AclRef]) -> SourceDocument:
    text = f"# PR #{pull.number}: {pull.title}\n\n{pull.body.strip()}"
    text += _thread("Reviews", pull.reviews) + _thread("Comments", pull.comments)
    meta: Mapping[str, object] = {
        "repo": repo,
        "number": pull.number,
        "state": pull.state,
        "merged": pull.merged,
        "author": pull.author,
        "labels": list(pull.labels),
        "decision_label": is_decision_pr(pull.labels),
    }
    return SourceDocument(
        "github",
        pull_id(org, repo, pull.number),
        "pull_request",
        f"PR #{pull.number}: {pull.title}",
        normalize_text(text),
        pull.updated_at,
        acl,
        meta,
        source_key(org, repo),
    )


def issue_document(org: str, repo: str, issue: GhIssue, acl: frozenset[AclRef]) -> SourceDocument:
    text = f"# Issue #{issue.number}: {issue.title}\n\n{issue.body.strip()}"
    text += _thread("Comments", issue.comments)
    meta: Mapping[str, object] = {
        "repo": repo,
        "number": issue.number,
        "state": issue.state,
        "author": issue.author,
        "labels": list(issue.labels),
        "decision_label": is_decision_pr(issue.labels),
    }
    return SourceDocument(
        "github",
        issue_id(org, repo, issue.number),
        "issue",
        f"Issue #{issue.number}: {issue.title}",
        normalize_text(text),
        issue.updated_at,
        acl,
        meta,
        source_key(org, repo),
    )


def markdown_document(org: str, repo: str, f: GhFile, acl: frozenset[AclRef]) -> SourceDocument:
    first = next((ln.lstrip("# ").strip() for ln in f.text.splitlines() if ln.startswith("#")), "")
    kind: SourceKind = "adr_file" if _ADR_PATH.search(f.path) else "doc"
    return SourceDocument(
        "github",
        file_id(org, repo, f.path),
        kind,
        first or f.path.rsplit("/", 1)[-1],
        normalize_text(f.text),
        f.updated_at,
        acl,
        {"repo": repo, "path": f.path},
        source_key(org, repo),
    )
