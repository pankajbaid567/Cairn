"""Semantic validator rules for a built :class:`Scenario` (design §12; requirement 13).

Each rule is a separate pure function ``Scenario -> Iterator[Violation]``. Rules never
raise on a broken scenario: a reference that does not resolve is REF-1's business, and
every other rule skips it. :data:`SEMANTIC_RULES` lists the functions in report order;
:func:`check_semantics` runs them all and returns the sorted, duplicate-free result.

| Rule  | What it checks |
|-------|----------------|
| ID-1  | ids are unique per collection; claim, conflict, PR and issue numbers per scope |
| REF-1 | every reference resolves to an entity of the expected kind |
| MEM-1 | membership intervals are ordered, not in the future and do not overlap |
| SRC-1 | source dates are ordered, not in the future; the location matches the kind |
| TL-1  | timeline events are in non-decreasing ``on`` order and not in the future |
| TL-2  | lifecycle events follow ``TRANSITIONS`` starting at ``proposed`` |
| TL-3  | an event is not older than its source; an ``implemented`` PR event is the merge date |
| TL-4  | ``effective`` differs from ``on`` only with the matching twist; valid order = list order |
| TL-5  | supersession links are symmetric and agree on dates |
| CLM-1 | claims have support; conflicts have two sides with different values |
| TW-1  | every twist has a gold question |
| TW-2  | a twist has the structure its kind needs (five kinds in v1) |
| CAN-1 | every canary is placed in a source |
| CAN-2 | canaries only appear in allowed scopes and only in source text |
| CAN-3 | token format, id prefix, no literal tokens; phrases unique, none inside another |
| CAN-4 | allowed scopes are restricted and exclude a persona |
| Q-1   | askers resolve; as-of dates are not in the future |
| Q-2   | ``must_not_contain`` askers cannot read the canary; each family has such a question |
| Q-3   | ``expected.lifecycle`` agrees with the oracle; ``must_not_cite`` not yet recorded |

SCH (structure) is not here: it is checked against the JSON Schema and the models
before a :class:`Scenario` exists (see ``cairn_eval.scenario.validator``).
"""

from __future__ import annotations

import datetime as dt
import re
from collections import defaultdict
from collections.abc import Callable, Iterator, Sequence
from typing import Any, Final

from cairn_eval.scenario.access import (
    is_restricted_scope,
    personas,
    readers,
    scope_exists,
)
from cairn_eval.scenario.canary import CANARY_FAMILIES, PLACEHOLDER_RE, canary_token
from cairn_eval.scenario.model import (
    Canary,
    Decision,
    PrincipalRef,
    Scenario,
    Source,
    Twist,
)
from cairn_eval.scenario.oracle import (
    TRANSITIONS,
    is_lifecycle,
    state_after,
    state_as_of,
    valid_time,
)
from cairn_eval.scenario.violation import Violation, finalize, pointer

RuleFn = Callable[[Scenario], Iterator[Violation]]

#: A token-shaped literal anywhere in the file (generated tokens must never be typed out).
_TOKEN_LITERAL_RE: Final = re.compile(r"KP-(?:HR|PARTNER|SECURITY|INCIDENT)-[A-Z2-7]{16}")
#: Canary id prefix -> family (``CAN-SEC-09`` is a security canary).
_PREFIX_FAMILY: Final = {"HR": "hr", "PARTNER": "partner", "SEC": "security", "INC": "incident"}
_DOC_KINDS: Final = ("doc", "wiki", "adr_file")


def _index(items: Sequence[Any]) -> dict[str, Any]:
    """Map id -> first item with that id (duplicates are ID-1's business)."""
    out: dict[str, Any] = {}
    for item in items:
        out.setdefault(item.id, item)
    return out


def _decision_twists(scenario: Scenario, decision_id: str, kind: str) -> list[Twist]:
    return [t for t in scenario.twists if t.kind == kind and decision_id in t.decisions]


# --------------------------------------------------------------------------------------
# ID-1
# --------------------------------------------------------------------------------------


def _duplicates(collection: tuple[str | int, ...], ids: Sequence[str]) -> Iterator[Violation]:
    seen: set[str] = set()
    for i, value in enumerate(ids):
        if value in seen:
            yield Violation(
                "ID-1", pointer(*collection, i, "id"), f"duplicate id {value!r} in {collection[-1]}"
            )
        seen.add(value)


def check_ids(s: Scenario) -> Iterator[Violation]:
    """ID-1: unique ids per collection; claim and conflict ids per decision; PR and issue
    numbers per repo."""
    for name in (
        "people",
        "teams",
        "repos",
        "channels",
        "sources",
        "decisions",
        "twists",
        "canaries",
        "questions",
    ):
        items = getattr(s, name)
        yield from _duplicates((name,), [item.id for item in items])
    for i, d in enumerate(s.decisions):
        yield from _duplicates(("decisions", i, "claims"), [c.id for c in d.claims])
        yield from _duplicates(("decisions", i, "conflicts"), [k.id for k in d.conflicts])
    seen: set[tuple[str, str, int]] = set()
    for i, src in enumerate(s.sources):
        repo = src.location.repo
        if repo is None:
            continue
        for kind, number in (
            ("pr", src.pr.number if src.pr else None),
            ("issue", src.issue.number if src.issue else None),
        ):
            if number is None:
                continue
            key = (kind, repo, number)
            if key in seen:
                label = "pull request" if kind == "pr" else "issue"
                yield Violation(
                    "ID-1",
                    pointer("sources", i, kind, "number"),
                    f"duplicate {label} number {number} in repo {repo!r}",
                )
            seen.add(key)


# --------------------------------------------------------------------------------------
# REF-1
# --------------------------------------------------------------------------------------


def _principal_refs(
    path: tuple[str | int, ...], ref: PrincipalRef
) -> Iterator[tuple[str, str, str]]:
    if ref.team is not None:
        yield pointer(*path, "team"), "team", ref.team
    if ref.person is not None:
        yield pointer(*path, "person"), "person", ref.person


def _references(s: Scenario) -> Iterator[tuple[str, str, str]]:
    """Every reference in the scenario as (pointer, expected kind, value)."""
    for i, m in enumerate(s.memberships):
        yield pointer("memberships", i, "person"), "person", m.person
        yield pointer("memberships", i, "team"), "team", m.team
    for i, repo in enumerate(s.repos):
        for j, grant in enumerate(repo.access):
            yield from _principal_refs(("repos", i, "access", j), grant)
    for i, channel in enumerate(s.channels):
        for j, member in enumerate(channel.members):
            yield from _principal_refs(("channels", i, "members", j), member)
    for i, src in enumerate(s.sources):
        loc = src.location
        if loc.repo is not None:
            yield pointer("sources", i, "location", "repo"), "repo", loc.repo
        if loc.channel is not None:
            yield pointer("sources", i, "location", "channel"), "channel", loc.channel
        if loc.meeting is not None:
            for j, who in enumerate(loc.meeting.attendees):
                yield pointer("sources", i, "location", "meeting", "attendees", j), "person", who
            for j, shared in enumerate(loc.meeting.share_with):
                yield from _principal_refs(
                    ("sources", i, "location", "meeting", "share_with", j), shared
                )
        if src.author is not None:
            yield pointer("sources", i, "author"), "person", src.author
        for j, message in enumerate(src.messages):
            yield pointer("sources", i, "messages", j, "author"), "person", message.author
    for i, d in enumerate(s.decisions):
        base = ("decisions", i)
        for field in ("decision_makers", "approvers"):
            for j, who in enumerate(getattr(d, field)):
                yield pointer(*base, field, j), "person", who
        for field in ("consulted", "informed"):
            for j, ref in enumerate(getattr(d, field)):
                yield from _principal_refs((*base, field, j), ref)
        if d.anchors is not None:
            for j, repo_id in enumerate(d.anchors.repos):
                yield pointer(*base, "anchors", "repos", j), "repo", repo_id
            for j, pr_ref in enumerate(d.anchors.prs):
                yield pointer(*base, "anchors", "prs", j), "pr", pr_ref
        for j, event in enumerate(d.timeline):
            yield pointer(*base, "timeline", j, "source"), "source", event.source
            if event.by is not None:
                yield pointer(*base, "timeline", j, "by"), "decision", event.by
        for j, claim in enumerate(d.claims):
            for k, support in enumerate(claim.support_sets):
                for n, source_id in enumerate(support):
                    yield (
                        pointer(*base, "claims", j, "support_sets", k, n),
                        "source",
                        source_id,
                    )
        for j, conflict in enumerate(d.conflicts):
            for k, side in enumerate(conflict.sides):
                yield pointer(*base, "conflicts", j, "sides", k, "source"), "source", side.source
        for field in ("supersedes", "superseded_by"):
            for j, other in enumerate(getattr(d, field)):
                yield pointer(*base, field, j), "decision", other
    for i, t in enumerate(s.twists):
        for field, kind in (("decisions", "decision"), ("people", "person"), ("sources", "source")):
            for j, value in enumerate(getattr(t, field)):
                yield pointer("twists", i, field, j), kind, value
    for i, c in enumerate(s.canaries):
        for j, scope in enumerate(c.allowed_scopes):
            yield pointer("canaries", i, "allowed_scopes", j), "scope", scope
    for i, q in enumerate(s.questions):
        for j, twist_id in enumerate(q.twists):
            yield pointer("questions", i, "twists", j), "twist", twist_id
        exp = ("questions", i, "expected")
        for j, decision_id in enumerate(q.expected.decisions):
            yield pointer(*exp, "decisions", j), "decision", decision_id
        for j, source_id in enumerate(q.expected.evidence):
            yield pointer(*exp, "evidence", j), "source", source_id
        for decision_id in q.expected.lifecycle:
            yield pointer(*exp, "lifecycle", decision_id), "decision", decision_id
        for j, canary_id in enumerate(q.expected.must_not_contain):
            yield pointer(*exp, "must_not_contain", j), "canary", canary_id
        for j, source_id in enumerate(q.expected.must_not_cite):
            yield pointer(*exp, "must_not_cite", j), "source", source_id


def _pr_exists(s: Scenario, value: str) -> bool:
    repo, _, number = value.partition("#")
    return any(
        src.kind == "pull_request"
        and src.location.repo == repo
        and src.pr is not None
        and str(src.pr.number) == number
        for src in s.sources
    )


def check_references(s: Scenario) -> Iterator[Violation]:
    """REF-1: every reference resolves to an entity of the expected kind."""
    known = {
        "person": {p.id for p in s.people},
        "team": {t.id for t in s.teams},
        "repo": {r.id for r in s.repos},
        "channel": {c.id for c in s.channels},
        "source": {src.id for src in s.sources},
        "decision": {d.id for d in s.decisions},
        "twist": {t.id for t in s.twists},
        "canary": {c.id for c in s.canaries},
    }
    for path, kind, value in _references(s):
        if kind == "scope":
            ok = scope_exists(s, value)
        elif kind == "pr":
            ok = _pr_exists(s, value)
        else:
            ok = value in known[kind]
        if not ok:
            what = "pull request source" if kind == "pr" else kind
            yield Violation("REF-1", path, f"no {what} {value!r}")


# --------------------------------------------------------------------------------------
# MEM-1
# --------------------------------------------------------------------------------------


def check_memberships(s: Scenario) -> Iterator[Violation]:
    """MEM-1: ``from < to``; ``from`` not after the reference date; no overlap per
    (person, team). Intervals are half-open, so back-to-back intervals are fine."""
    reference = s.meta.reference_date
    by_pair: dict[tuple[str, str], list[tuple[dt.date, dt.date, int]]] = defaultdict(list)
    for i, m in enumerate(s.memberships):
        path = pointer("memberships", i)
        if m.from_ > reference:
            yield Violation(
                "MEM-1", path, f"starts {m.from_}, after the reference date {reference}"
            )
        if m.to is not None and m.to <= m.from_:
            yield Violation("MEM-1", path, f"ends {m.to}, which is not after it starts {m.from_}")
            continue
        by_pair[(m.person, m.team)].append((m.from_, m.to or dt.date.max, i))
    for (person, team), intervals in by_pair.items():
        intervals.sort()
        latest_end = dt.date.min
        for start, end, i in intervals:
            if start < latest_end:
                yield Violation(
                    "MEM-1",
                    pointer("memberships", i),
                    f"overlaps an earlier membership of {person!r} in {team!r}",
                )
            latest_end = max(latest_end, end)


# --------------------------------------------------------------------------------------
# SRC-1
# --------------------------------------------------------------------------------------


def check_sources(s: Scenario) -> Iterator[Violation]:
    """SRC-1: ``date <= available <= reference date``; ``deleted > date``;
    ``last_edited >= date``; the location and the pr/issue block match the kind."""
    reference = s.meta.reference_date
    for i, src in enumerate(s.sources):
        path = pointer("sources", i)
        if src.date > reference:
            yield Violation("SRC-1", path, f"date {src.date} is after the reference date")
        if src.available is not None:
            if src.available < src.date:
                yield Violation(
                    "SRC-1",
                    pointer("sources", i, "available"),
                    f"available {src.available} is before date {src.date}",
                )
            if src.available > reference:
                yield Violation(
                    "SRC-1",
                    pointer("sources", i, "available"),
                    f"available {src.available} is after the reference date {reference}",
                )
        if src.deleted is not None and src.deleted <= src.date:
            yield Violation(
                "SRC-1",
                pointer("sources", i, "deleted"),
                f"deleted {src.deleted} is not after date {src.date}",
            )
        if src.last_edited is not None and src.last_edited < src.date:
            yield Violation(
                "SRC-1",
                pointer("sources", i, "last_edited"),
                f"last_edited {src.last_edited} is before date {src.date}",
            )
        yield from _check_location(i, src)
        yield from _check_pr_and_issue(i, src)


def _check_location(i: int, src: Source) -> Iterator[Violation]:
    loc = src.location
    path = pointer("sources", i, "location")
    placed = [name for name in ("repo", "channel", "meeting") if getattr(loc, name) is not None]
    if src.kind == "chat_thread":
        wanted = "channel"
    elif src.kind == "meeting_note":
        wanted = "meeting"
    else:
        wanted = "repo"
    if placed != [wanted]:
        found = ", ".join(placed) or "nothing"
        yield Violation(
            "SRC-1", path, f"a {src.kind} needs exactly a {wanted} location, found {found}"
        )
    if loc.path is not None and loc.repo is None:
        yield Violation("SRC-1", pointer("sources", i, "location", "path"), "path needs a repo")


def _check_pr_and_issue(i: int, src: Source) -> Iterator[Violation]:
    if src.kind == "pull_request" and src.pr is None:
        yield Violation("SRC-1", pointer("sources", i), "a pull_request source needs a 'pr' block")
    if src.kind != "pull_request" and src.pr is not None:
        yield Violation(
            "SRC-1", pointer("sources", i, "pr"), "'pr' is only for pull_request sources"
        )
    if src.kind == "issue" and src.issue is None:
        yield Violation("SRC-1", pointer("sources", i), "an issue source needs an 'issue' block")
    if src.kind != "issue" and src.issue is not None:
        yield Violation(
            "SRC-1", pointer("sources", i, "issue"), "'issue' is only for issue sources"
        )
    if src.pr is not None:
        merged = src.pr.merged_at
        if src.pr.state == "merged" and merged is None:
            yield Violation(
                "SRC-1", pointer("sources", i, "pr"), "a merged pull request needs merged_at"
            )
        if src.pr.state != "merged" and merged is not None:
            yield Violation(
                "SRC-1", pointer("sources", i, "pr", "merged_at"), "merged_at needs state merged"
            )
        if merged is not None and merged.date() < src.date:
            yield Violation(
                "SRC-1",
                pointer("sources", i, "pr", "merged_at"),
                f"merged_at {merged.date()} is before date {src.date}",
            )


# --------------------------------------------------------------------------------------
# TL-1 .. TL-5
# --------------------------------------------------------------------------------------


def check_timeline_order(s: Scenario) -> Iterator[Violation]:
    """TL-1: events are listed in non-decreasing ``on`` order, none after the reference date."""
    reference = s.meta.reference_date
    for i, d in enumerate(s.decisions):
        previous: dt.date | None = None
        for j, event in enumerate(d.timeline):
            path = pointer("decisions", i, "timeline", j)
            if event.on > reference:
                yield Violation(
                    "TL-1", path, f"{event.event} on {event.on} is after the reference date"
                )
            if previous is not None and event.on < previous:
                yield Violation(
                    "TL-1",
                    path,
                    f"{event.event} on {event.on} is listed after an event on {previous}",
                )
            previous = event.on if previous is None else max(previous, event.on)


def lifecycle_violations(d: Decision, path: str) -> Iterator[Violation]:
    """TL-2 for one decision (design §12 pseudocode). ``path`` is the decision's pointer.

    Pure and usable on its own (the lifecycle property test, task 12.4, calls it with
    hand-built decisions)."""
    state: str | None = None
    if not any(is_lifecycle(e) for e in d.timeline):
        yield Violation(
            "TL-2", path + pointer("timeline"), "no lifecycle events; must start with proposed"
        )
        return
    for j, event in enumerate(d.timeline):
        here = path + pointer("timeline", j)
        if not is_lifecycle(event):
            if state is None:
                yield Violation("TL-2", here, f"{event.event} before proposed")
            continue
        if event.event not in TRANSITIONS.get(state, frozenset()):
            yield Violation("TL-2", here, f"{state or 'start'} -> {event.event} is not allowed")
        state = state_after(event.event)


def check_lifecycle(s: Scenario) -> Iterator[Violation]:
    """TL-2: lifecycle events form a path of ``TRANSITIONS`` starting at ``proposed``."""
    for i, d in enumerate(s.decisions):
        yield from lifecycle_violations(d, pointer("decisions", i))


def check_event_sources(s: Scenario) -> Iterator[Violation]:
    """TL-3: an event is not dated before its source; an ``implemented`` event backed by a
    pull request is dated on the merge date (the PR must be merged)."""
    sources = _index(s.sources)
    for i, d in enumerate(s.decisions):
        for j, event in enumerate(d.timeline):
            src = sources.get(event.source)
            if src is None:
                continue
            path = pointer("decisions", i, "timeline", j)
            if event.on < src.date:
                yield Violation(
                    "TL-3",
                    path,
                    f"{event.event} on {event.on} is before its source {src.id!r} ({src.date})",
                )
            if event.event == "implemented" and src.kind == "pull_request" and src.pr is not None:
                merged = src.pr.merged_at
                if merged is None:
                    yield Violation("TL-3", path, f"implemented, but {src.id!r} is not merged")
                elif event.on != merged.date():
                    yield Violation(
                        "TL-3",
                        path,
                        f"implemented on {event.on}, but {src.id!r} merged on {merged.date()}",
                    )


def check_valid_time(s: Scenario) -> Iterator[Violation]:
    """TL-4: ``effective < on`` needs a backdated_valid_time twist on the decision,
    ``effective > on`` a scheduled_change twist; and ordering lifecycle events by valid
    time gives the same order as listing them (by ``on``)."""
    for i, d in enumerate(s.decisions):
        backdated = bool(_decision_twists(s, d.id, "backdated_valid_time"))
        scheduled = bool(_decision_twists(s, d.id, "scheduled_change"))
        previous_valid: dt.date | None = None
        for j, event in enumerate(d.timeline):
            path = pointer("decisions", i, "timeline", j)
            if event.effective is not None:
                if event.effective < event.on and not backdated:
                    yield Violation(
                        "TL-4",
                        path,
                        f"effective {event.effective} is before on {event.on}; "
                        "needs a backdated_valid_time twist",
                    )
                if event.effective > event.on and not scheduled:
                    yield Violation(
                        "TL-4",
                        path,
                        f"effective {event.effective} is after on {event.on}; "
                        "needs a scheduled_change twist",
                    )
            if not is_lifecycle(event):
                continue
            current = valid_time(event)
            if previous_valid is not None and current < previous_valid:
                yield Violation(
                    "TL-4",
                    path,
                    f"valid time {current} sorts before an earlier-listed event ({previous_valid})",
                )
            previous_valid = current if previous_valid is None else max(previous_valid, current)


def check_supersession(s: Scenario) -> Iterator[Violation]:
    """TL-5: ``supersedes`` and ``superseded_by`` mirror each other, the older decision
    has a ``superseded`` event, and its valid time equals the newer decision's accepted
    valid time."""
    decisions = _index(s.decisions)
    for i, d in enumerate(s.decisions):
        for j, old_id in enumerate(d.supersedes):
            old = decisions.get(old_id)
            if old is not None and d.id not in old.superseded_by:
                yield Violation(
                    "TL-5",
                    pointer("decisions", i, "supersedes", j),
                    f"{old_id} does not list {d.id} in superseded_by",
                )
        for j, new_id in enumerate(d.superseded_by):
            new = decisions.get(new_id)
            if new is not None and d.id not in new.supersedes:
                yield Violation(
                    "TL-5",
                    pointer("decisions", i, "superseded_by", j),
                    f"{new_id} does not list {d.id} in supersedes",
                )
    for i, d in enumerate(s.decisions):
        superseding = [n for n in s.decisions if d.id in n.supersedes or n.id in d.superseded_by]
        events = [(j, e) for j, e in enumerate(d.timeline) if e.event == "superseded"]
        if not events:
            if superseding:
                yield Violation(
                    "TL-5",
                    pointer("decisions", i, "timeline"),
                    f"superseded by {superseding[0].id}, but has no superseded event",
                )
            continue
        j, event = events[0]
        path = pointer("decisions", i, "timeline", j)
        if not superseding:
            yield Violation("TL-5", path, "superseded event, but no decision supersedes this one")
            continue
        if event.by is not None and event.by not in {n.id for n in superseding}:
            yield Violation(
                "TL-5", path, f"superseded by {event.by}, which does not list {d.id} in supersedes"
            )
        for newer in superseding:
            accepted = [e for e in newer.timeline if e.event == "accepted"]
            if not accepted:
                yield Violation(
                    "TL-5", path, f"superseding decision {newer.id} has no accepted event"
                )
            elif valid_time(accepted[-1]) != valid_time(event):
                yield Violation(
                    "TL-5",
                    path,
                    f"superseded valid {valid_time(event)} differs from {newer.id} "
                    f"accepted valid {valid_time(accepted[-1])}",
                )


# --------------------------------------------------------------------------------------
# CLM-1, TW-1, TW-2
# --------------------------------------------------------------------------------------


def check_claims(s: Scenario) -> Iterator[Violation]:
    """CLM-1: every claim has a non-empty support set; every conflict has at least two
    sides with different values."""
    for i, d in enumerate(s.decisions):
        for j, claim in enumerate(d.claims):
            path = pointer("decisions", i, "claims", j)
            if not claim.support_sets:
                yield Violation("CLM-1", path, f"claim {claim.id} has no support set")
            for k, support in enumerate(claim.support_sets):
                if not support:
                    yield Violation(
                        "CLM-1",
                        pointer("decisions", i, "claims", j, "support_sets", k),
                        "empty support set",
                    )
        for j, conflict in enumerate(d.conflicts):
            if len({side.value for side in conflict.sides}) < 2:
                yield Violation(
                    "CLM-1",
                    pointer("decisions", i, "conflicts", j),
                    f"conflict {conflict.id} needs two sides with different values",
                )


def check_twist_questions(s: Scenario) -> Iterator[Violation]:
    """TW-1: every twist is referenced by at least one gold question."""
    used = {t for q in s.questions for t in q.twists}
    for i, twist in enumerate(s.twists):
        if twist.id not in used:
            yield Violation("TW-1", pointer("twists", i), f"no gold question exercises {twist.id}")


def _tier_sources(s: Scenario, tier: str) -> set[str]:
    """Sources cited at this tier by a claim support set or a conflict side."""
    found: set[str] = set()
    for d in s.decisions:
        for claim in d.claims:
            if claim.tier == tier:
                found.update(src for support in claim.support_sets for src in support)
        for conflict in d.conflicts:
            found.update(side.source for side in conflict.sides if side.tier == tier)
    return found


def _named_sources(s: Scenario, twist: Twist) -> list[Source]:
    sources = _index(s.sources)
    return [sources[sid] for sid in twist.sources if sid in sources]


def _tw2_deleted_source(s: Scenario, twist: Twist) -> str | None:
    if any(src.deleted is not None for src in _named_sources(s, twist)):
        return None
    return "must name a source that has a 'deleted' date"


def _tw2_stale_doc(s: Scenario, twist: Twist) -> str | None:
    named = _named_sources(s, twist)
    t1 = _tier_sources(s, "T1")
    stale = [d for d in named if d.kind in _DOC_KINDS and d.last_edited is not None]
    if not stale:
        return "must name a doc or wiki source with 'last_edited'"
    for doc in stale:
        edited = doc.last_edited
        if edited is not None and any(
            n.id != doc.id and n.id in t1 and n.date > edited for n in named
        ):
            return None
    return (
        "must name a T1 source (cited by a T1 claim or conflict side) "
        "dated after the stale doc's last_edited"
    )


def _tw2_team_move(s: Scenario, twist: Twist) -> str | None:
    for person in twist.people:
        mine = [m for m in s.memberships if m.person == person]
        if any(
            a.to is not None and a.team != b.team and a.to == b.from_ for a in mine for b in mine
        ):
            return None
    return (
        "must name a person with one membership ending the day a membership in another team starts"
    )


def _tw2_injected_instruction(s: Scenario, twist: Twist) -> str | None:
    repos = _index(s.repos)
    for src in _named_sources(s, twist):
        repo = repos.get(src.location.repo or "")
        if src.kind == "issue" and repo is not None and repo.visibility == "public":
            return None
    return "must name an issue source in a public repo"


def _tw2_restricted_decision(s: Scenario, twist: Twist) -> str | None:
    decisions = _index(s.decisions)
    sources = _index(s.sources)
    named = [decisions[d] for d in twist.decisions if d in decisions]
    if not named:
        return "must name a decision"
    open_ones: list[str] = []
    for decision in named:
        cited = [e.source for e in decision.timeline]
        cited += [sid for c in decision.claims for support in c.support_sets for sid in support]
        for sid in dict.fromkeys(cited):
            src = sources.get(sid)
            if src is not None and not (src.scope and is_restricted_scope(s, src.scope)):
                open_ones.append(f"{decision.id} cites {sid} outside a restricted scope")
    return "; ".join(open_ones) or None


_TW2_CHECKS: Final[dict[str, Callable[[Scenario, Twist], str | None]]] = {
    "deleted_source": _tw2_deleted_source,
    "stale_doc": _tw2_stale_doc,
    "team_move": _tw2_team_move,
    "injected_instruction": _tw2_injected_instruction,
    "restricted_decision": _tw2_restricted_decision,
}


def check_twist_structure(s: Scenario) -> Iterator[Violation]:
    """TW-2: kind-specific integrity for deleted_source, stale_doc, team_move,
    injected_instruction and restricted_decision. Other kinds have no extra structure in v1."""
    for i, twist in enumerate(s.twists):
        check = _TW2_CHECKS.get(twist.kind)
        problem = check(s, twist) if check is not None else None
        if problem is not None:
            yield Violation(
                "TW-2", pointer("twists", i), f"{twist.kind} twist {twist.id}: {problem}"
            )


# --------------------------------------------------------------------------------------
# CAN-1 .. CAN-4
# --------------------------------------------------------------------------------------


def _source_text_fields(i: int, src: Source) -> Iterator[tuple[str, str]]:
    if src.title is not None:
        yield pointer("sources", i, "title"), src.title
    if src.body is not None:
        yield pointer("sources", i, "body"), src.body
    for j, message in enumerate(src.messages):
        yield pointer("sources", i, "messages", j, "text"), message.text


def _walk_strings(value: object, parts: tuple[str | int, ...]) -> Iterator[tuple[str, str]]:
    if isinstance(value, str):
        yield pointer(*parts), value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _walk_strings(item, (*parts, key))
    elif isinstance(value, list):
        for j, item in enumerate(value):
            yield from _walk_strings(item, (*parts, j))


def _non_source_strings(s: Scenario) -> Iterator[tuple[str, str]]:
    """Every string outside ``sources`` and ``canaries`` (canaries may not appear there)."""
    dumped = s.model_dump(mode="json", by_alias=True)
    for key, value in dumped.items():
        if key not in {"sources", "canaries"}:
            yield from _walk_strings(value, (key,))


def _mentions(text: str, canary: Canary) -> bool:
    if canary.kind == "phrase" and canary.phrase is not None:
        return canary.phrase.casefold() in text.casefold()
    return False


def _placements(s: Scenario) -> dict[str, list[tuple[str, str | None]]]:
    """Canary id -> [(pointer of the text field, scope of its source)] for every placement
    in a source. Includes unknown ids used in placeholders."""
    placed: dict[str, list[tuple[str, str | None]]] = defaultdict(list)
    for i, src in enumerate(s.sources):
        for path, text in _source_text_fields(i, src):
            for match in PLACEHOLDER_RE.finditer(text):
                placed[match.group(1)].append((path, src.scope))
            for canary in s.canaries:
                if _mentions(text, canary):
                    placed[canary.id].append((path, src.scope))
    return placed


def check_canary_placed(s: Scenario) -> Iterator[Violation]:
    """CAN-1: every canary is placed in at least one source."""
    placed = _placements(s)
    for i, canary in enumerate(s.canaries):
        if not placed.get(canary.id):
            yield Violation(
                "CAN-1", pointer("canaries", i), f"{canary.id} is not placed in any source"
            )


def check_canary_scopes(s: Scenario) -> Iterator[Violation]:
    """CAN-2: placements sit inside the allowed scopes; placeholders name known canaries;
    no placeholder or phrase appears outside source text."""
    known = _index(s.canaries)
    for canary_id, places in _placements(s).items():
        canary = known.get(canary_id)
        for path, scope in places:
            if canary is None:
                yield Violation("CAN-2", path, f"unknown canary {canary_id!r}")
            elif scope not in canary.allowed_scopes:
                yield Violation(
                    "CAN-2",
                    path,
                    f"{canary_id} placed in {scope}; allowed: {', '.join(canary.allowed_scopes)}",
                )
    for path, text in _non_source_strings(s):
        if PLACEHOLDER_RE.search(text):
            yield Violation("CAN-2", path, "canary placeholders may only appear in source text")
        for canary in s.canaries:
            if _mentions(text, canary):
                yield Violation("CAN-2", path, f"canary {canary.id} may only appear in source text")


def check_canary_values(s: Scenario) -> Iterator[Violation]:
    """CAN-3: generated tokens match the format and are distinct; ids carry the right
    family prefix; no token-shaped literal appears in the file; phrases are unique and none
    contains another."""
    tokens: dict[str, str] = {}
    for i, canary in enumerate(s.canaries):
        path = pointer("canaries", i)
        prefix = canary.id.split("-")[1] if canary.id.count("-") >= 2 else ""
        if _PREFIX_FAMILY.get(prefix) != canary.family:
            yield Violation("CAN-3", path, f"{canary.id} does not match family {canary.family!r}")
        if canary.kind == "token":
            token = canary_token(s.meta.canary_seed, canary.id, canary.family)
            if not re.fullmatch(r"KP-(HR|PARTNER|SECURITY|INCIDENT)-[A-Z2-7]{16}", token):
                yield Violation("CAN-3", path, f"generated token {token} breaks the token format")
            if token in tokens and tokens[token] != canary.id:
                yield Violation(
                    "CAN-3", path, f"{canary.id} and {tokens[token]} generate the same token"
                )
            tokens.setdefault(token, canary.id)
    dumped = s.model_dump(mode="json", by_alias=True)
    for path, text in _walk_strings(dumped, ()):
        if _TOKEN_LITERAL_RE.search(text):
            yield Violation(
                "CAN-3", path, "a canary token is written out; use a {{canary:ID}} placeholder"
            )
    phrases = [(i, c.phrase.casefold()) for i, c in enumerate(s.canaries) if c.phrase is not None]
    for n, (i, phrase) in enumerate(phrases):
        for m, (_, other) in enumerate(phrases):
            if n == m:
                continue
            if phrase == other and n > m:
                yield Violation("CAN-3", pointer("canaries", i, "phrase"), "duplicate phrase")
            elif phrase != other and phrase in other:
                yield Violation(
                    "CAN-3",
                    pointer("canaries", i, "phrase"),
                    "phrase is contained in another canary's phrase",
                )


def check_canary_scope_exposure(s: Scenario) -> Iterator[Violation]:
    """CAN-4: every allowed scope is restricted and, on the reference date, leaves out at
    least one persona (so a leak test can fail)."""
    everyone = personas(s)
    for i, canary in enumerate(s.canaries):
        for j, scope in enumerate(canary.allowed_scopes):
            if not scope_exists(s, scope):
                continue
            path = pointer("canaries", i, "allowed_scopes", j)
            if not is_restricted_scope(s, scope):
                yield Violation("CAN-4", path, f"{scope} is not a restricted scope")
            elif not everyone - readers(s, scope, s.meta.reference_date):
                yield Violation(
                    "CAN-4", path, f"every persona can read {scope}; a leak test could not fail"
                )


# --------------------------------------------------------------------------------------
# Q-1 .. Q-3
# --------------------------------------------------------------------------------------


def _asker_person(asker: str) -> str:
    return asker.removeprefix("agent:")


def check_questions(s: Scenario) -> Iterator[Violation]:
    """Q-1: the asker is a person or ``agent:<person>``; as-of dates are not in the future."""
    people = {p.id for p in s.people}
    reference = s.meta.reference_date
    for i, q in enumerate(s.questions):
        if _asker_person(q.asker) not in people:
            yield Violation(
                "Q-1",
                pointer("questions", i, "asker"),
                f"asker {q.asker!r} is not a person or agent:<person>",
            )
        if q.as_of is not None:
            for name, day in (("valid", q.as_of.valid), ("recorded", q.as_of.recorded)):
                if day > reference:
                    yield Violation(
                        "Q-1",
                        pointer("questions", i, "as_of", name),
                        f"{name} date {day} is after the reference date",
                    )


def check_question_secrecy(s: Scenario) -> Iterator[Violation]:
    """Q-2: the asker of a ``must_not_contain`` question cannot read any allowed scope of
    that canary; every canary family in the scenario has at least one such question."""
    people = {p.id for p in s.people}
    canaries = _index(s.canaries)
    covered: set[str] = set()
    for i, q in enumerate(s.questions):
        asker = _asker_person(q.asker)
        if asker not in people:
            continue
        for j, canary_id in enumerate(q.expected.must_not_contain):
            canary = canaries.get(canary_id)
            if canary is None:
                continue
            leaked = [
                scope
                for scope in canary.allowed_scopes
                if asker in readers(s, scope, s.meta.reference_date)
            ]
            if leaked:
                yield Violation(
                    "Q-2",
                    pointer("questions", i, "expected", "must_not_contain", j),
                    f"{q.asker} can read {leaked[0]}, so {canary_id} may legitimately appear",
                )
            else:
                covered.add(canary.family)
    for family in CANARY_FAMILIES:
        first = next((i for i, c in enumerate(s.canaries) if c.family == family), None)
        if first is not None and family not in covered:
            yield Violation(
                "Q-2",
                pointer("canaries", first),
                f"no question asks as someone who cannot read a {family} canary",
            )


def check_question_expectations(s: Scenario) -> Iterator[Violation]:
    """Q-3: ``expected.lifecycle`` agrees with the as-of oracle; ``must_not_cite`` sources
    were not yet recorded at the question's recorded date."""
    decisions = _index(s.decisions)
    sources = _index(s.sources)
    for i, q in enumerate(s.questions):
        valid = q.as_of.valid if q.as_of is not None else s.meta.reference_date
        recorded = q.as_of.recorded if q.as_of is not None else s.meta.reference_date
        for decision_id, expected in q.expected.lifecycle.items():
            decision = decisions.get(decision_id)
            if decision is None:
                continue
            try:
                actual = state_as_of(decision, valid, recorded, s)
            except KeyError:
                continue  # an event cites a missing source; REF-1 reports it
            if actual != expected:
                yield Violation(
                    "Q-3",
                    pointer("questions", i, "expected", "lifecycle", decision_id),
                    f"{decision_id} is {actual} at valid {valid}, recorded {recorded}; "
                    f"expected {expected}",
                )
        for j, source_id in enumerate(q.expected.must_not_cite):
            src = sources.get(source_id)
            if src is not None and src.available_or_date <= recorded:
                yield Violation(
                    "Q-3",
                    pointer("questions", i, "expected", "must_not_cite", j),
                    f"{source_id} was recorded {src.available_or_date}, on or before "
                    f"the question's recorded date {recorded}",
                )


#: Every semantic rule function, in report order.
SEMANTIC_RULES: Final[tuple[RuleFn, ...]] = (
    check_ids,
    check_references,
    check_memberships,
    check_sources,
    check_timeline_order,
    check_lifecycle,
    check_event_sources,
    check_valid_time,
    check_supersession,
    check_claims,
    check_twist_questions,
    check_twist_structure,
    check_canary_placed,
    check_canary_scopes,
    check_canary_values,
    check_canary_scope_exposure,
    check_questions,
    check_question_secrecy,
    check_question_expectations,
)


def check_semantics(scenario: Scenario) -> list[Violation]:
    """Run every semantic rule; the result is sorted by rule id, then pointer, and has no
    duplicates (requirements 13.2, 13.24)."""
    return finalize(v for rule in SEMANTIC_RULES for v in rule(scenario))
