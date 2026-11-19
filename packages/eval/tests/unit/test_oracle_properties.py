"""Property test for the as-of oracle (task 12.5, requirements 14.1 to 14.3).

Feature: cairn-phase-0-foundations, Property 13: The as-of oracle agrees with a naive replay
and is monotone

**Validates: Requirements 14.1, 14.2, 14.3**

``state_as_of`` is compared with a naive replay written in this file. The replay works on
plain event specs and its own copies of the rules (valid time is ``effective`` or ``on``;
recorded time is ``max(on, source availability)``; ``reopened`` leads to ``proposed``), so it
shares no code with ``cairn_eval.scenario.oracle``. Decisions are generated freely: random
lifecycle and annotation events in any order (the oracle must not depend on the timeline
being a legal walk), random ``on`` dates, optional ``effective`` dates before or after
``on``, and sources with random dates and optional later availability. Narrow date ranges
make ties on valid time and on the as-of boundaries common.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from hypothesis import given
from hypothesis import strategies as st

from cairn_eval.scenario.loader import load_scenario_text
from cairn_eval.scenario.model import Decision, Scenario
from cairn_eval.scenario.oracle import state_as_of

BASE: Final = dt.date(2026, 1, 1)
LIFECYCLE: Final = (
    "proposed",
    "accepted",
    "rejected",
    "withdrawn",
    "implemented",
    "superseded",
    "deprecated",
    "reverted",
    "reopened",
)
ANNOTATIONS: Final = ("discussed", "amended", "partially_reverted")
REPO_ROOT: Final = Path(__file__).resolve().parents[4]
KESTREL: Final = REPO_ROOT / "scenarios" / "kestrel_pay" / "scenario.yaml"


def day(offset: int) -> dt.date:
    return BASE + dt.timedelta(days=offset)


# --------------------------------------------------------------------------------------
# Plain specs, the scenario builder and the naive replay
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class EventSpec:
    name: str
    on: dt.date
    effective: dt.date | None
    source: int  # index into World.sources


@dataclass(frozen=True)
class SourceSpec:
    date: dt.date
    available: dt.date | None


@dataclass(frozen=True)
class World:
    sources: tuple[SourceSpec, ...]
    events: tuple[EventSpec, ...]

    def with_events(self, events: tuple[EventSpec, ...]) -> World:
        return World(self.sources, events)


def build(world: World) -> tuple[Scenario, Decision]:
    """A real ``Scenario`` holding the sources and one decision with the events."""
    sources: list[dict[str, Any]] = []
    for i, src in enumerate(world.sources):
        entry: dict[str, Any] = {
            "id": f"s{i}",
            "kind": "doc",
            "date": src.date,
            "location": {"repo": "r"},
        }
        if src.available is not None:
            entry["available"] = src.available
        sources.append(entry)
    timeline: list[dict[str, Any]] = []
    for ev in world.events:
        item: dict[str, Any] = {"event": ev.name, "on": ev.on, "source": f"s{ev.source}"}
        if ev.effective is not None:
            item["effective"] = ev.effective
        timeline.append(item)
    raw: dict[str, Any] = {
        "schema_version": 1,
        "meta": {
            "id": "gen",
            "name": "Gen",
            "reference_date": day(400),
            "canary_seed": "gen-seed",
        },
        "people": [],
        "teams": [],
        "memberships": [],
        "repos": [],
        "channels": [],
        "sources": sources,
        "decisions": [{"id": "ADR-001", "title": "t", "timeline": timeline}],
        "twists": [],
        "canaries": [],
        "questions": [],
    }
    scenario = Scenario.model_validate(raw)
    return scenario, scenario.decisions[0]


def event_valid(ev: EventSpec) -> dt.date:
    return ev.effective if ev.effective is not None else ev.on


def event_recorded(world: World, ev: EventSpec) -> dt.date:
    src = world.sources[ev.source]
    return max(ev.on, src.available if src.available is not None else src.date)


def considered(world: World, valid: dt.date, recorded: dt.date) -> frozenset[int]:
    """Indexes of the lifecycle events the oracle may look at."""
    return frozenset(
        i
        for i, ev in enumerate(world.events)
        if ev.name not in ANNOTATIONS
        and event_valid(ev) <= valid
        and event_recorded(world, ev) <= recorded
    )


def replay(world: World, indexes: frozenset[int]) -> str:
    """Sort by (valid time, list index), take the last, map it to a state."""
    if not indexes:
        return "absent"
    order = sorted((event_valid(world.events[i]), i) for i in indexes)
    name = world.events[order[-1][1]].name
    return "proposed" if name == "reopened" else name


def oracle(world: World, valid: dt.date, recorded: dt.date) -> str:
    scenario, decision = build(world)
    return state_as_of(decision, valid, recorded, scenario)


def lifecycle_indexes(world: World) -> frozenset[int]:
    return frozenset(i for i, ev in enumerate(world.events) if ev.name not in ANNOTATIONS)


# --------------------------------------------------------------------------------------
# Generators
# --------------------------------------------------------------------------------------

as_of_dates = st.integers(min_value=-5, max_value=75).map(day)


@st.composite
def event_specs(draw: st.DrawFn, source_count: int) -> EventSpec:
    name = draw(st.sampled_from([*LIFECYCLE, *LIFECYCLE, *ANNOTATIONS]))
    on = draw(st.integers(min_value=0, max_value=40))
    shift = draw(st.one_of(st.none(), st.integers(min_value=-20, max_value=20)))
    effective = None if shift is None else day(on + shift)
    return EventSpec(name, day(on), effective, draw(st.integers(0, source_count - 1)))


@st.composite
def worlds(draw: st.DrawFn, *, max_events: int = 8) -> World:
    """1 to 4 sources (date 0..40, optionally available up to 30 days later) and 0 to
    ``max_events`` events in arbitrary order."""
    sources: list[SourceSpec] = []
    for _ in range(draw(st.integers(min_value=1, max_value=4))):
        offset = draw(st.integers(min_value=0, max_value=40))
        delay = draw(st.one_of(st.none(), st.integers(min_value=0, max_value=30)))
        sources.append(SourceSpec(day(offset), None if delay is None else day(offset + delay)))
    events = draw(st.lists(event_specs(len(sources)), max_size=max_events))
    return World(tuple(sources), tuple(events))


# --------------------------------------------------------------------------------------
# Properties
# --------------------------------------------------------------------------------------


@given(worlds(), as_of_dates, as_of_dates)
def test_oracle_agrees_with_the_naive_replay(
    world: World, valid: dt.date, recorded: dt.date
) -> None:
    """Feature: cairn-phase-0-foundations, Property 13: The as-of oracle agrees with a naive
    replay and is monotone (agreement; requirements 14.1, 14.2)."""
    assert oracle(world, valid, recorded) == replay(world, considered(world, valid, recorded))


@given(worlds(), as_of_dates, as_of_dates, as_of_dates)
def test_raising_the_recorded_date_only_adds_events(
    world: World, valid: dt.date, r1: dt.date, r2: dt.date
) -> None:
    """Feature: cairn-phase-0-foundations, Property 13: The as-of oracle agrees with a naive
    replay and is monotone (recorded axis; requirement 14.3)."""
    low, high = sorted((r1, r2))
    small, large = considered(world, valid, low), considered(world, valid, high)
    assert small <= large
    before, after = oracle(world, valid, low), oracle(world, valid, high)
    assert before == replay(world, small)
    assert after == replay(world, large)
    if small == large:
        assert before == after  # the answer changes only when a later event qualifies
    if before != "absent":
        assert after != "absent"
    assert (before == "absent") == (not small)


@given(worlds(), as_of_dates, as_of_dates, as_of_dates)
def test_raising_the_valid_date_only_adds_events(
    world: World, recorded: dt.date, v1: dt.date, v2: dt.date
) -> None:
    """Feature: cairn-phase-0-foundations, Property 13: The as-of oracle agrees with a naive
    replay and is monotone (valid axis)."""
    low, high = sorted((v1, v2))
    small, large = considered(world, low, recorded), considered(world, high, recorded)
    assert small <= large
    before, after = oracle(world, low, recorded), oracle(world, high, recorded)
    assert before == replay(world, small)
    assert after == replay(world, large)
    if small == large:
        assert before == after
    if before != "absent":
        assert after != "absent"
    assert (before == "absent") == (not small)


@given(worlds(), as_of_dates, as_of_dates)
def test_annotation_events_never_change_the_answer(
    world: World, valid: dt.date, recorded: dt.date
) -> None:
    """Feature: cairn-phase-0-foundations, Property 13: The as-of oracle agrees with a naive
    replay and is monotone (annotations are ignored)."""
    bare = world.with_events(tuple(ev for ev in world.events if ev.name not in ANNOTATIONS))
    assert oracle(world, valid, recorded) == oracle(bare, valid, recorded)


@st.composite
def invisible_events(
    draw: st.DrawFn, world: World, valid: dt.date, recorded: dt.date
) -> tuple[World, EventSpec, int]:
    """A lifecycle event recorded after ``recorded`` but valid by ``valid``, with a world
    that has a source for it, and the list position to insert it at. Either its source
    becomes available late or the event itself is dated after ``recorded``."""
    name = draw(st.sampled_from(LIFECYCLE))
    late = recorded + dt.timedelta(days=draw(st.integers(min_value=1, max_value=10)))
    if draw(st.booleans()):
        sources = (*world.sources, SourceSpec(BASE, late))
        event = EventSpec(name, draw(as_of_dates), valid, len(world.sources))
    else:
        sources = world.sources
        event = EventSpec(name, late, valid, draw(st.integers(0, len(sources) - 1)))
    position = draw(st.integers(min_value=0, max_value=len(world.events)))
    return World(sources, world.events), event, position


@given(st.data(), worlds(), as_of_dates, as_of_dates)
def test_an_event_recorded_after_the_as_of_date_is_invisible(
    data: st.DataObject, world: World, valid: dt.date, recorded: dt.date
) -> None:
    """Feature: cairn-phase-0-foundations, Property 13: The as-of oracle agrees with a naive
    replay and is monotone (a not-yet-recorded event cannot change the answer)."""
    base, event, position = data.draw(invisible_events(world, valid, recorded))
    assert event_recorded(base, event) > recorded
    assert event_valid(event) <= valid
    events = (*base.events[:position], event, *base.events[position:])
    extended = base.with_events(events)
    assert oracle(extended, valid, recorded) == oracle(base, valid, recorded)
    # the same event does count once the recorded date reaches it
    reached = event_recorded(extended, event)
    assert position in considered(extended, valid, reached)


@given(worlds(), st.integers(1, 10), st.integers(1, 10))
def test_before_every_event_the_state_is_absent(world: World, dv: int, dr: int) -> None:
    """Feature: cairn-phase-0-foundations, Property 13: The as-of oracle agrees with a naive
    replay and is monotone (before everything: absent)."""
    lifecycle = [world.events[i] for i in sorted(lifecycle_indexes(world))]
    first_valid = min((event_valid(ev) for ev in lifecycle), default=BASE)
    first_recorded = min((event_recorded(world, ev) for ev in lifecycle), default=BASE)
    valid = first_valid - dt.timedelta(days=dv)
    recorded = first_recorded - dt.timedelta(days=dr)
    assert oracle(world, valid, recorded) == "absent"
    # early on one axis only is enough as well
    assert oracle(world, valid, first_recorded + dt.timedelta(days=400)) == "absent"
    assert oracle(world, first_valid + dt.timedelta(days=400), recorded) == "absent"


@given(worlds(), st.integers(0, 10), st.integers(0, 10))
def test_after_every_event_the_state_is_that_of_the_last_event(
    world: World, dv: int, dr: int
) -> None:
    """Feature: cairn-phase-0-foundations, Property 13: The as-of oracle agrees with a naive
    replay and is monotone (after everything: the last event by valid time and position)."""
    lifecycle = lifecycle_indexes(world)
    valid = max((event_valid(world.events[i]) for i in lifecycle), default=BASE)
    recorded = max((event_recorded(world, world.events[i]) for i in lifecycle), default=BASE)
    valid += dt.timedelta(days=dv)
    recorded += dt.timedelta(days=dr)
    assert considered(world, valid, recorded) == lifecycle
    if not lifecycle:
        expected = "absent"
    else:
        last = sorted((event_valid(world.events[i]), i) for i in lifecycle)[-1][1]
        name = world.events[last].name
        expected = "proposed" if name == "reopened" else name
    assert oracle(world, valid, recorded) == expected


# --------------------------------------------------------------------------------------
# The dated cases of requirements 14.4 to 14.6 on the real Kestrel Pay scenario
# --------------------------------------------------------------------------------------


def _kestrel() -> Scenario:
    raw = load_scenario_text(KESTREL.read_text(encoding="utf-8"), source=str(KESTREL))
    return Scenario.model_validate(raw)


def _decision(s: Scenario, decision_id: str) -> Decision:
    return next(d for d in s.decisions if d.id == decision_id)


def test_kestrel_backdated_flag_vendor_move_as_recorded_today() -> None:
    """Requirement 14.4: ADR-014 is accepted and ADR-004 superseded on 5 Jun 2026."""
    s = _kestrel()
    valid = dt.date(2026, 6, 5)
    assert state_as_of(_decision(s, "ADR-014"), valid, s.meta.reference_date, s) == "accepted"
    assert state_as_of(_decision(s, "ADR-004"), valid, s.meta.reference_date, s) == "superseded"


def test_kestrel_backdated_flag_vendor_move_as_recorded_on_8_june() -> None:
    """Requirement 14.5: before the notes were imported, ADR-014 is only proposed and
    ADR-004 still implemented."""
    s = _kestrel()
    valid, recorded = dt.date(2026, 6, 5), dt.date(2026, 6, 8)
    assert state_as_of(_decision(s, "ADR-014"), valid, recorded, s) == "proposed"
    assert state_as_of(_decision(s, "ADR-004"), valid, recorded, s) == "implemented"


def test_kestrel_adr007_on_10_march() -> None:
    """Requirement 14.6: ADR-007 is accepted, not yet implemented (the PR merged on 12 Mar)."""
    s = _kestrel()
    march_10 = dt.date(2026, 3, 10)
    assert state_as_of(_decision(s, "ADR-007"), march_10, march_10, s) == "accepted"
