"""Who can read what in a scenario (design §11, "Scopes and canaries").

A source's *scope* comes from its location: ``repo:<id>``, ``channel:<id>`` or
``meeting:<source id>``. On a given date the readers of a scope are:

* ``repo:`` public repo: every person. Private repo: the people and the current members
  of the teams in its access list (any permission level reads).
* ``channel:`` public channel: every employee (everyone who is not ``external``).
  Private channel: its member people and the current members of its member teams.
* ``meeting:`` the attendees plus the people and teams in ``share_with``.

Team membership is time-aware: a membership counts on ``d`` when ``from <= d`` and
(``to`` is absent or ``d < to``).

A scope is *restricted* when it is a private repo, a private channel or a meeting note.
"""

from __future__ import annotations

import datetime as dt
from typing import Final

from cairn_eval.scenario.model import PrincipalRef, Scenario, Source

#: The five personas of brainstorm section 17 (Asha, Ben, Dev, Priya, Carla).
PERSONA_IDS: Final = ("asha", "ben", "dev", "priya", "carla")


def personas(scenario: Scenario) -> frozenset[str]:
    """Ids of the personas defined in the scenario.

    If a scenario defines none of :data:`PERSONA_IDS` (a small test scenario), every
    person counts as a persona, so rules that need "somebody who cannot read X" still work.
    """
    defined = {p.id for p in scenario.people}
    chosen = defined & set(PERSONA_IDS)
    return frozenset(chosen or defined)


def team_members(scenario: Scenario, team: str, on: dt.date) -> frozenset[str]:
    """People who belong to ``team`` on the date ``on``."""
    return frozenset(
        m.person
        for m in scenario.memberships
        if m.team == team and m.from_ <= on and (m.to is None or on < m.to)
    )


def principal_members(scenario: Scenario, ref: PrincipalRef, on: dt.date) -> frozenset[str]:
    """The people a ``{team: x}`` or ``{person: y}`` reference stands for on ``on``."""
    if ref.team is not None:
        return team_members(scenario, ref.team, on)
    if ref.person is not None:
        return frozenset({ref.person})
    return frozenset()


def _meeting_source(scenario: Scenario, source_id: str) -> Source | None:
    for src in scenario.sources:
        if src.id == source_id and src.location.meeting is not None:
            return src
    return None


def scope_exists(scenario: Scenario, scope: str) -> bool:
    """True if ``scope`` names a real repo, channel or meeting-note source."""
    kind, _, name = scope.partition(":")
    if kind == "repo":
        return any(r.id == name for r in scenario.repos)
    if kind == "channel":
        return any(c.id == name for c in scenario.channels)
    if kind == "meeting":
        return _meeting_source(scenario, name) is not None
    return False


def is_restricted_scope(scenario: Scenario, scope: str) -> bool:
    """True for a private repo, a private channel or a meeting note; false otherwise
    (including unknown scopes, which REF-1 reports separately)."""
    kind, _, name = scope.partition(":")
    if kind == "repo":
        return any(r.id == name and r.visibility == "private" for r in scenario.repos)
    if kind == "channel":
        return any(c.id == name and c.private for c in scenario.channels)
    if kind == "meeting":
        return _meeting_source(scenario, name) is not None
    return False


def readers(scenario: Scenario, scope: str, on: dt.date) -> frozenset[str]:
    """Ids of the people who can read ``scope`` on ``on`` (empty for an unknown scope)."""
    kind, _, name = scope.partition(":")
    everyone = frozenset(p.id for p in scenario.people)
    if kind == "repo":
        for repo in scenario.repos:
            if repo.id == name:
                if repo.visibility == "public":
                    return everyone
                found: set[str] = set()
                for grant in repo.access:
                    found |= principal_members(scenario, grant, on)
                return frozenset(found)
    elif kind == "channel":
        for channel in scenario.channels:
            if channel.id == name:
                if not channel.private:
                    return frozenset(p.id for p in scenario.people if not p.external)
                found = set()
                for member in channel.members:
                    found |= principal_members(scenario, member, on)
                return frozenset(found)
    elif kind == "meeting":
        meeting_source = _meeting_source(scenario, name)
        if meeting_source is not None and meeting_source.location.meeting is not None:
            meeting = meeting_source.location.meeting
            found = set(meeting.attendees)
            for shared in meeting.share_with:
                found |= principal_members(scenario, shared, on)
            return frozenset(found)
    return frozenset()
