"""An in-memory GitHub organisation for tests, the timing harness and the demo (plan 3.13).

Mutation methods change state the way a person would on github.com and return the webhook
deliveries GitHub would send (headers and payload), so a test can feed them to a receiver. Nothing
touches the network.
"""

from __future__ import annotations

import itertools
import json
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any

from cairn_connectors.github.client import GitHubError, NotFound
from cairn_connectors.github.models import (
    ApiBudget,
    GhCollaborator,
    GhComment,
    GhDelivery,
    GhFile,
    GhIssue,
    GhPull,
    GhRepo,
    GhRepoTeam,
    GhTeam,
)
from cairn_core.webhook import sign


@dataclass
class Delivery:
    """What GitHub would POST: ``headers`` (lower-case) and the raw ``body``."""

    headers: dict[str, str]
    body: bytes


@dataclass
class FakeGitHub:
    org: str = "kestrel-pay-demo"
    secret: str = "kestrel-test-webhook-secret"  # noqa: S105 - a published test value
    members: set[str] = field(default_factory=set)
    teams: dict[str, set[str]] = field(default_factory=dict)  # slug -> logins
    repos: dict[str, GhRepo] = field(default_factory=dict)
    repo_teams: dict[str, dict[str, str]] = field(
        default_factory=dict
    )  # repo -> slug -> permission
    collaborators: dict[str, dict[str, GhCollaborator]] = field(default_factory=dict)
    pulls: dict[tuple[str, int], GhPull] = field(default_factory=dict)
    issues: dict[tuple[str, int], GhIssue] = field(default_factory=dict)
    files: dict[tuple[str, str], GhFile] = field(default_factory=dict)
    budget: ApiBudget = field(default_factory=ApiBudget)
    fail_next: int = 0  # the next N reads raise GitHubError(503)
    redelivered: list[str] = field(default_factory=list)
    delivery_log: list[GhDelivery] = field(default_factory=list)
    _ids: Any = field(default_factory=lambda: itertools.count(1000))
    _clock: datetime = datetime(2026, 11, 1, tzinfo=UTC)

    # --- reads (the GitHubClient protocol) -----------------------------------------------

    def _read(self, path: str) -> None:
        self.budget.requests += 1
        self.budget.by_path[path] = self.budget.by_path.get(path, 0) + 1
        if self.fail_next > 0:
            self.fail_next -= 1
            raise GitHubError("service unavailable", 503)

    def list_org_members(self, org: str) -> Sequence[str]:
        self._read("members")
        return sorted(self.members)

    def list_teams(self, org: str) -> Sequence[GhTeam]:
        self._read("teams")
        return [GhTeam(slug, slug.title()) for slug in sorted(self.teams)]

    def list_team_members(self, org: str, slug: str) -> Sequence[str]:
        self._read("team_members")
        if slug not in self.teams:
            raise NotFound(f"team {slug}")
        return sorted(self.teams[slug])

    def list_repos(self, org: str) -> Sequence[GhRepo]:
        self._read("repos")
        return sorted(self.repos.values(), key=lambda r: r.name)

    def list_repo_teams(self, org: str, repo: str) -> Sequence[GhRepoTeam]:
        self._read("repo_teams")
        return [GhRepoTeam(s, p) for s, p in sorted(self.repo_teams.get(repo, {}).items())]

    def list_collaborators(self, org: str, repo: str) -> Sequence[GhCollaborator]:
        self._read("collaborators")
        return sorted(self.collaborators.get(repo, {}).values(), key=lambda c: c.login)

    def get_pull(self, org: str, repo: str, number: int) -> GhPull:
        self._read("pull")
        try:
            return self.pulls[(repo, number)]
        except KeyError:
            raise NotFound(f"{repo}#{number}") from None

    def get_issue(self, org: str, repo: str, number: int) -> GhIssue:
        self._read("issue")
        try:
            return self.issues[(repo, number)]
        except KeyError:
            raise NotFound(f"{repo}#{number}") from None

    def list_pulls(self, org: str, repo: str, since: datetime | None = None) -> Sequence[int]:
        self._read("pulls")
        return sorted(
            n
            for (r, n), p in self.pulls.items()
            if r == repo and (since is None or p.updated_at >= since)
        )

    def list_issues(self, org: str, repo: str, since: datetime | None = None) -> Sequence[int]:
        self._read("issues")
        return sorted(
            n
            for (r, n), i in self.issues.items()
            if r == repo and (since is None or i.updated_at >= since)
        )

    def list_markdown(self, org: str, repo: str) -> Sequence[GhFile]:
        self._read("markdown")
        return sorted((f for (r, _), f in self.files.items() if r == repo), key=lambda f: f.path)

    def list_deliveries(self, since: datetime | None = None) -> Sequence[GhDelivery]:
        self._read("deliveries")
        return [d for d in self.delivery_log if since is None or d.delivered_at >= since]

    def redeliver(self, delivery_id: str) -> None:
        self._read("redeliver")
        self.redelivered.append(delivery_id)

    # --- setup and mutations (each returns the deliveries GitHub would send) --------------

    def now(self) -> datetime:
        self._clock += timedelta(seconds=1)
        return self._clock

    def _delivery(self, event: str, payload: dict[str, Any]) -> Delivery:
        body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        did = f"fake-{next(self._ids)}-{event}"
        self.delivery_log.append(GhDelivery(did, event, 200, self.now()))
        return Delivery(
            {
                "x-github-event": event,
                "x-github-delivery": did,
                "x-hub-signature-256": sign(self.secret.encode(), body),
                "content-type": "application/json",
            },
            body,
        )

    def _ctx(self, repo: str | None = None) -> dict[str, Any]:
        out: dict[str, Any] = {
            "organization": {"id": 7001, "login": self.org},
            "installation": {"id": 5003},
        }
        if repo is not None:
            out["repository"] = {"id": self.repos[repo].id, "full_name": f"{self.org}/{repo}"}
        return out

    def add_repo(self, name: str, *, private: bool = True) -> GhRepo:
        repo = GhRepo(next(self._ids), name, private)
        self.repos[name] = repo
        return repo

    def add_member(self, login: str, teams: Sequence[str] = ()) -> None:
        self.members.add(login)
        for slug in teams:
            self.teams.setdefault(slug, set()).add(login)

    def grant_team(self, repo: str, slug: str, permission: str = "push") -> None:
        self.repo_teams.setdefault(repo, {})[slug] = permission

    def add_collaborator(
        self, repo: str, login: str, permission: str = "pull", *, outside: bool = True
    ) -> Delivery:
        self.collaborators.setdefault(repo, {})[login] = GhCollaborator(login, permission, outside)
        return self._delivery(
            "member", {"action": "added", "member": {"login": login}, **self._ctx(repo)}
        )

    def remove_collaborator(self, repo: str, login: str) -> Delivery:
        self.collaborators.get(repo, {}).pop(login, None)
        return self._delivery(
            "member", {"action": "removed", "member": {"login": login}, **self._ctx(repo)}
        )

    def remove_team_member(self, slug: str, login: str) -> Delivery:
        self.teams.get(slug, set()).discard(login)
        return self._delivery(
            "membership",
            {
                "action": "removed",
                "scope": "team",
                "member": {"login": login},
                "team": {"slug": slug},
                **self._ctx(),
            },
        )

    def add_team_member(self, slug: str, login: str) -> Delivery:
        self.members.add(login)
        self.teams.setdefault(slug, set()).add(login)
        return self._delivery(
            "membership",
            {
                "action": "added",
                "scope": "team",
                "member": {"login": login},
                "team": {"slug": slug},
                **self._ctx(),
            },
        )

    def remove_org_member(self, login: str) -> Delivery:
        self.members.discard(login)
        for logins in self.teams.values():
            logins.discard(login)
        return self._delivery(
            "organization",
            {"action": "member_removed", "membership": {"user": {"login": login}}, **self._ctx()},
        )

    def set_visibility(self, repo: str, *, private: bool) -> Delivery:
        self.repos[repo] = replace(self.repos[repo], private=private)
        return self._delivery(
            "repository", {"action": "privatized" if private else "publicized", **self._ctx(repo)}
        )

    def delete_repo(self, repo: str) -> Delivery:
        ctx = self._ctx(repo)
        del self.repos[repo]
        for store in (self.pulls, self.issues):
            for key in [k for k in store if k[0] == repo]:
                del store[key]
        for file_key in [k for k in self.files if k[0] == repo]:
            del self.files[file_key]
        return self._delivery("repository", {"action": "deleted", **ctx})

    def put_file(self, repo: str, path: str, text: str) -> Delivery:
        self.files[(repo, path)] = GhFile(path, text, self.now())
        return self._delivery("push", {"ref": "refs/heads/main", **self._ctx(repo)})

    def delete_file(self, repo: str, path: str) -> Delivery:
        self.files.pop((repo, path), None)
        return self._delivery("push", {"ref": "refs/heads/main", **self._ctx(repo)})

    def put_pull(
        self,
        repo: str,
        number: int,
        title: str,
        body: str,
        *,
        author: str = "asha",
        comments: Sequence[GhComment] = (),
        reviews: Sequence[GhComment] = (),
        labels: Sequence[str] = (),
        merged: bool = False,
    ) -> Delivery:
        existed = (repo, number) in self.pulls
        self.pulls[(repo, number)] = GhPull(
            number,
            title,
            body,
            "closed" if merged else "open",
            merged,
            author,
            self.now(),
            tuple(labels),
            tuple(comments),
            tuple(reviews),
        )
        return self._delivery(
            "pull_request",
            {
                "action": "edited" if existed else "opened",
                "number": number,
                "pull_request": {"number": number},
                **self._ctx(repo),
            },
        )

    def put_issue(
        self,
        repo: str,
        number: int,
        title: str,
        body: str,
        *,
        author: str = "asha",
        comments: Sequence[GhComment] = (),
    ) -> Delivery:
        self.issues[(repo, number)] = GhIssue(
            number, title, body, "open", author, self.now(), (), tuple(comments)
        )
        return self._delivery(
            "issues", {"action": "opened", "issue": {"number": number}, **self._ctx(repo)}
        )
