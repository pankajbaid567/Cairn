"""Plain data returned by a GitHub client (plan 3.5 to 3.7). No HTTP types leak past this layer."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True)
class GhRepo:
    id: int
    name: str
    private: bool
    archived: bool = False

    def full_name(self, org: str) -> str:
        return f"{org}/{self.name}"


@dataclass(frozen=True)
class GhTeam:
    slug: str
    name: str


@dataclass(frozen=True)
class GhCollaborator:
    login: str
    permission: str  # pull, triage, push, maintain, admin
    outside: bool


@dataclass(frozen=True)
class GhRepoTeam:
    slug: str
    permission: str


@dataclass(frozen=True)
class GhComment:
    author: str
    body: str
    at: datetime


@dataclass(frozen=True)
class GhPull:
    number: int
    title: str
    body: str
    state: str  # open, closed
    merged: bool
    author: str
    updated_at: datetime
    labels: tuple[str, ...] = ()
    comments: tuple[GhComment, ...] = ()
    reviews: tuple[GhComment, ...] = ()


@dataclass(frozen=True)
class GhIssue:
    number: int
    title: str
    body: str
    state: str
    author: str
    updated_at: datetime
    labels: tuple[str, ...] = ()
    comments: tuple[GhComment, ...] = ()


@dataclass(frozen=True)
class GhFile:
    path: str
    text: str
    updated_at: datetime


@dataclass(frozen=True)
class GhDelivery:
    id: str
    event: str
    status_code: int
    delivered_at: datetime
    redelivery: bool = False


@dataclass
class ApiBudget:
    """Counts requests so tests and the timing harness can show conditional requests save calls."""

    requests: int = 0
    not_modified: int = 0
    by_path: dict[str, int] = field(default_factory=dict)
