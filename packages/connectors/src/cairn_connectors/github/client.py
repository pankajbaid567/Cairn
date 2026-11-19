"""The GitHub client contract (plan 3.5 to 3.7, 3.10). Two implementations: the real HTTP client
and an in-memory fake that tests, the timing harness and the demo use.

Everything is synchronous: the drainer runs in a worker thread, so a blocking client keeps it simple.
"""  # noqa: E501

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol

from cairn_connectors.github.models import (
    GhCollaborator,
    GhDelivery,
    GhFile,
    GhIssue,
    GhPull,
    GhRepo,
    GhRepoTeam,
    GhTeam,
)


class GitHubError(Exception):
    """A GitHub call failed. ``status`` is the HTTP status, 0 for a network failure."""

    def __init__(self, message: str, status: int = 0) -> None:
        super().__init__(message)
        self.status = status


class NotFound(GitHubError):
    def __init__(self, what: str) -> None:
        super().__init__(f"not found: {what}", 404)


class GitHubClient(Protocol):
    def list_org_members(self, org: str) -> Sequence[str]: ...
    def list_teams(self, org: str) -> Sequence[GhTeam]: ...
    def list_team_members(self, org: str, slug: str) -> Sequence[str]: ...
    def list_repos(self, org: str) -> Sequence[GhRepo]: ...
    def list_repo_teams(self, org: str, repo: str) -> Sequence[GhRepoTeam]: ...
    def list_collaborators(self, org: str, repo: str) -> Sequence[GhCollaborator]: ...
    def get_pull(self, org: str, repo: str, number: int) -> GhPull: ...
    def get_issue(self, org: str, repo: str, number: int) -> GhIssue: ...
    def list_pulls(self, org: str, repo: str, since: datetime | None = None) -> Sequence[int]: ...
    def list_issues(self, org: str, repo: str, since: datetime | None = None) -> Sequence[int]: ...
    def list_markdown(self, org: str, repo: str) -> Sequence[GhFile]: ...
    def list_deliveries(self, since: datetime | None = None) -> Sequence[GhDelivery]: ...
    def redeliver(self, delivery_id: str) -> None: ...
