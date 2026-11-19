"""The decision lifecycle state machine and the as-of oracle (design §12; requirement 14).

Everything here is pure: no I/O, no clock, no randomness. The validator uses it for
rules TL-2, TL-4, TL-5 and Q-3, and Phase 2 reuses :func:`state_as_of` as the oracle
for the bitemporal tests (2.15, 2.16, 2.18).

Two time axes (brainstorm sections 6 and 10):

* **valid time**: when a thing held in reality. For an event it is ``effective`` if given,
  else ``on`` (:func:`valid_time`).
* **recorded time**: when Cairn could first know about it, the later of ``on`` and the
  date the backing source became available (:func:`recorded_time`).

Events come in two kinds. *Lifecycle* events change a decision's state; *annotation*
events (``discussed``, ``amended``, ``partially_reverted``) never do.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping
from typing import Final

from cairn_eval.scenario.model import (
    ANNOTATION_EVENTS,
    Decision,
    LifecycleState,
    Scenario,
    TimelineEvent,
)

#: Allowed lifecycle moves (brainstorm section 6). The key is the current state
#: (``None`` before the first event), the value the lifecycle events allowed next.
#: ``reopened`` is only legal after ``reverted`` and leads back to ``proposed``, so it is
#: a move, never a state. Terminal states map to an empty set.
TRANSITIONS: Final[Mapping[str | None, frozenset[str]]] = {
    None: frozenset({"proposed"}),
    "proposed": frozenset({"accepted", "rejected", "withdrawn"}),
    "accepted": frozenset({"implemented", "superseded", "deprecated"}),
    "implemented": frozenset({"superseded", "reverted", "deprecated"}),
    "reverted": frozenset({"reopened"}),
    "rejected": frozenset(),
    "withdrawn": frozenset(),
    "superseded": frozenset(),
    "deprecated": frozenset(),
}

#: Event names that never change a decision's state.
ANNOTATIONS: Final[frozenset[str]] = frozenset(ANNOTATION_EVENTS)

_STATE_AFTER: Final[Mapping[str, LifecycleState]] = {
    "proposed": "proposed",
    "accepted": "accepted",
    "rejected": "rejected",
    "withdrawn": "withdrawn",
    "implemented": "implemented",
    "superseded": "superseded",
    "deprecated": "deprecated",
    "reverted": "reverted",
    "reopened": "proposed",
}


def is_lifecycle(event: TimelineEvent) -> bool:
    """True for events that change state, false for annotations."""
    return event.event not in ANNOTATIONS


def state_after(event_name: str) -> LifecycleState:
    """The state a lifecycle event leads to (``reopened`` leads back to ``proposed``).

    Raises ``KeyError`` for annotation events and unknown names.
    """
    return _STATE_AFTER[event_name]


def valid_time(event: TimelineEvent) -> dt.date:
    """When the event held in reality: ``effective`` if set, else ``on``."""
    return event.effective if event.effective is not None else event.on


def recorded_time(event: TimelineEvent, scenario: Scenario) -> dt.date:
    """When Cairn could first know the event: the later of ``on`` and its source's
    availability date. Raises ``KeyError`` if the event's source does not exist."""
    return max(event.on, scenario.source(event.source).available_or_date)


def state_as_of(
    decision: Decision, valid: dt.date, recorded: dt.date, scenario: Scenario
) -> LifecycleState:
    """The decision's lifecycle state at ``valid`` as known at ``recorded``.

    Considers lifecycle events whose recorded time is on or before ``recorded`` and whose
    valid time is on or before ``valid``; the state is the one the latest such event (by
    valid time) leads to. Ties on valid time keep list order, so the later-listed event
    wins. Returns ``"absent"`` if no event qualifies. Annotation events are ignored.

    Monotone in ``recorded``: raising it only adds events to the set considered.
    Raises ``KeyError`` if a lifecycle event's source does not exist.
    """
    best_key: tuple[dt.date, int] | None = None
    best_state: LifecycleState = "absent"
    for index, event in enumerate(decision.timeline):
        if not is_lifecycle(event):
            continue
        event_valid = valid_time(event)
        if event_valid > valid or recorded_time(event, scenario) > recorded:
            continue
        key = (event_valid, index)
        if best_key is None or key > best_key:
            best_key = key
            best_state = state_after(event.event)
    return best_state
