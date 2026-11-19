"""Property test for the decision lifecycle state machine (task 12.4, requirement 13.10).

Feature: cairn-phase-0-foundations, Property 10: Lifecycle checking matches the state machine

**Validates: Requirements 13.10**

``lifecycle_violations`` (rule TL-2) is checked three ways: legal walks of ``TRANSITIONS``
(with annotation events mixed in) report nothing; a legal walk plus exactly one illegal step
reports exactly that step; and on arbitrary event sequences it agrees with a naive reference
written here, with its own copy of the transition table.
"""

from __future__ import annotations

import datetime as dt
from typing import Final

from hypothesis import assume, given
from hypothesis import strategies as st

from cairn_eval.scenario.model import Decision
from cairn_eval.scenario.oracle import TRANSITIONS, state_after
from cairn_eval.scenario.rules import lifecycle_violations

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
ALL_EVENTS: Final = LIFECYCLE + ANNOTATIONS
PATH: Final = "/d"

# The reference's own copy of the brainstorm section 6 table (not imported from the oracle).
_NEXT: Final[dict[str | None, set[str]]] = {
    None: {"proposed"},
    "proposed": {"accepted", "rejected", "withdrawn"},
    "accepted": {"implemented", "superseded", "deprecated"},
    "implemented": {"superseded", "reverted", "deprecated"},
    "reverted": {"reopened"},
    "rejected": set(),
    "withdrawn": set(),
    "superseded": set(),
    "deprecated": set(),
}


def decision_of(names: list[str]) -> Decision:
    """A decision whose timeline holds ``names`` in order, on consecutive days."""
    start = dt.date(2026, 1, 1)
    timeline = [
        {"event": name, "on": (start + dt.timedelta(days=i)).isoformat(), "source": "s"}
        for i, name in enumerate(names)
    ]
    return Decision.model_validate({"id": "ADR-001", "title": "t", "timeline": timeline})


def paths(names: list[str]) -> list[str]:
    return [v.path for v in lifecycle_violations(decision_of(names), PATH)]


def at(index: int) -> str:
    return f"{PATH}/timeline/{index}"


def reference_paths(names: list[str]) -> list[str]:
    """Naive TL-2: walk the events, remember the state, report what does not fit."""
    if all(n in ANNOTATIONS for n in names):  # also true for an empty timeline
        return [f"{PATH}/timeline"]
    found: list[str] = []
    state: str | None = None
    for i, name in enumerate(names):
        if name in ANNOTATIONS:
            if state is None:
                found.append(at(i))
            continue
        if name not in _NEXT[state]:
            found.append(at(i))
        state = "proposed" if name == "reopened" else name
    return found


def final_state(walk: list[str]) -> str | None:
    return state_after(walk[-1]) if walk else None


# --------------------------------------------------------------------------------------
# Generators
# --------------------------------------------------------------------------------------


@st.composite
def lifecycle_walks(
    draw: st.DrawFn, *, min_size: int = 0, max_size: int = 8, to_terminal: bool = False
) -> list[str]:
    """A legal walk of ``TRANSITIONS`` from the start. With ``to_terminal`` the walk runs
    until no move is left (capped at 40 steps; a longer walk is discarded)."""
    steps: list[str] = []
    state: str | None = None
    size = 40 if to_terminal else draw(st.integers(min_value=min_size, max_value=max_size))
    while len(steps) < size:
        options = sorted(TRANSITIONS[state])
        if not options:
            break
        event = draw(st.sampled_from(options))
        steps.append(event)
        state = state_after(event)
    if to_terminal:
        assume(not TRANSITIONS[state])
    return steps


def annotate(draw: st.DrawFn, walk: list[str]) -> list[str]:
    """Insert 0 to 2 annotation events after each lifecycle event (never before the first)."""
    out: list[str] = []
    for event in walk:
        out.append(event)
        out.extend(draw(st.lists(st.sampled_from(ANNOTATIONS), max_size=2)))
    return out


@st.composite
def annotated_walks(draw: st.DrawFn) -> list[str]:
    return annotate(draw, draw(lifecycle_walks(min_size=1)))


@st.composite
def walks_with_one_illegal_step(draw: st.DrawFn) -> tuple[list[str], int]:
    """A legal walk (possibly empty), then one lifecycle event the current state does not
    allow, then annotations. Returns the timeline and the index of the illegal step."""
    walk = draw(lifecycle_walks(to_terminal=draw(st.booleans())))
    state = final_state(walk)
    illegal = draw(st.sampled_from([e for e in LIFECYCLE if e not in TRANSITIONS[state]]))
    prefix = annotate(draw, walk)
    trailing = draw(st.lists(st.sampled_from(ANNOTATIONS), max_size=2))
    return [*prefix, illegal, *trailing], len(prefix)


@st.composite
def arbitrary_sequences(draw: st.DrawFn) -> list[str]:
    """Uniform random events, or a legal walk damaged by a few replaced, inserted or
    deleted events, so that both mostly-legal and wholly random timelines are covered."""
    if draw(st.booleans()):
        return draw(st.lists(st.sampled_from(ALL_EVENTS), max_size=12))
    seq = annotate(draw, draw(lifecycle_walks(max_size=8)))
    for _ in range(draw(st.integers(min_value=1, max_value=3))):
        op = draw(st.sampled_from(["replace", "insert", "delete"]))
        if op == "insert" or not seq:
            index = draw(st.integers(min_value=0, max_value=len(seq)))
            seq.insert(index, draw(st.sampled_from(ALL_EVENTS)))
        else:
            index = draw(st.integers(min_value=0, max_value=len(seq) - 1))
            if op == "replace":
                seq[index] = draw(st.sampled_from(ALL_EVENTS))
            else:
                del seq[index]
    return seq


# --------------------------------------------------------------------------------------
# Properties
# --------------------------------------------------------------------------------------


@given(annotated_walks())
def test_legal_walks_with_annotations_have_no_violation(names: list[str]) -> None:
    """Feature: cairn-phase-0-foundations, Property 10: Lifecycle checking matches the
    state machine (legal walks pass)."""
    assert paths(names) == []


@given(walks_with_one_illegal_step())
def test_one_illegal_step_is_reported_at_its_index_and_nowhere_else(
    case: tuple[list[str], int],
) -> None:
    """Feature: cairn-phase-0-foundations, Property 10: Lifecycle checking matches the
    state machine (one illegal step is reported at its index, none before it)."""
    names, index = case
    violations = list(lifecycle_violations(decision_of(names), PATH))
    assert [v.path for v in violations] == [at(index)]
    assert violations[0].rule == "TL-2"


@given(lifecycle_walks(to_terminal=True), st.sampled_from(LIFECYCLE))
def test_any_lifecycle_event_after_a_terminal_state_is_reported(
    walk: list[str], extra: str
) -> None:
    """Feature: cairn-phase-0-foundations, Property 10: Lifecycle checking matches the
    state machine (nothing follows a terminal state)."""
    assert paths([*walk, extra]) == [at(len(walk))]


@given(
    lifecycle_walks().filter(lambda w: final_state(w) != "reverted"),
    st.integers(min_value=0, max_value=2),
)
def test_reopened_is_only_allowed_after_reverted(walk: list[str], notes: int) -> None:
    """Feature: cairn-phase-0-foundations, Property 10: Lifecycle checking matches the
    state machine (reopened needs reverted)."""
    names = [*walk, "reopened", *["discussed"] * notes]
    assert paths(names) == [at(len(walk))]


@given(st.lists(st.sampled_from(ANNOTATIONS), max_size=6))
def test_empty_or_annotation_only_timelines_are_reported(names: list[str]) -> None:
    """Feature: cairn-phase-0-foundations, Property 10: Lifecycle checking matches the
    state machine (no lifecycle events)."""
    assert paths(names) == [f"{PATH}/timeline"]


@given(
    st.lists(st.sampled_from(ANNOTATIONS), min_size=1, max_size=3),
    lifecycle_walks(min_size=1),
)
def test_annotations_before_proposed_are_each_reported(lead: list[str], walk: list[str]) -> None:
    """Feature: cairn-phase-0-foundations, Property 10: Lifecycle checking matches the
    state machine (annotation before proposed)."""
    assert paths([*lead, *walk]) == [at(i) for i in range(len(lead))]


@given(arbitrary_sequences())
def test_checker_agrees_with_the_naive_reference_on_arbitrary_sequences(names: list[str]) -> None:
    """Feature: cairn-phase-0-foundations, Property 10: Lifecycle checking matches the
    state machine (same violating indexes as the reference)."""
    assert paths(names) == reference_paths(names)


@given(arbitrary_sequences())
def test_lifecycle_violations_is_deterministic_and_does_not_mutate_its_input(
    names: list[str],
) -> None:
    """Feature: cairn-phase-0-foundations, Property 10: Lifecycle checking matches the
    state machine (pure)."""
    decision = decision_of(names)
    before = decision.model_dump()
    first = list(lifecycle_violations(decision, PATH))
    second = list(lifecycle_violations(decision, PATH))
    assert first == second
    assert decision.model_dump() == before
