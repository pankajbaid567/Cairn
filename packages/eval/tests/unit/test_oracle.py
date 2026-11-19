"""Unit tests for the lifecycle state machine and the as-of oracle
(design §12; requirements 14.1 to 14.6)."""

import copy
import datetime as dt
from pathlib import Path
from typing import Any

import pytest

from cairn_eval.scenario.loader import load_scenario_text
from cairn_eval.scenario.model import (
    ANNOTATION_EVENTS,
    LIFECYCLE_EVENTS,
    LIFECYCLE_STATES,
    Decision,
    Scenario,
    TimelineEvent,
)
from cairn_eval.scenario.oracle import (
    ANNOTATIONS,
    TRANSITIONS,
    is_lifecycle,
    recorded_time,
    state_after,
    state_as_of,
    valid_time,
)

MINI = Path(__file__).parent / "data" / "mini_scenario.yaml"


def _raw() -> dict[str, Any]:
    raw = load_scenario_text(MINI.read_text(encoding="utf-8"), source=str(MINI))
    assert isinstance(raw, dict)
    return copy.deepcopy(raw)


def _scenario(raw: dict[str, Any] | None = None) -> Scenario:
    return Scenario.model_validate(raw if raw is not None else _raw())


def _decision(s: Scenario, decision_id: str) -> Decision:
    return next(d for d in s.decisions if d.id == decision_id)


def _day(text: str) -> dt.date:
    return dt.date.fromisoformat(text)


# --------------------------------------------------------------------------------------
# TRANSITIONS and event helpers
# --------------------------------------------------------------------------------------


def test_transitions_follow_the_section_6_state_machine() -> None:
    assert TRANSITIONS[None] == {"proposed"}
    assert TRANSITIONS["proposed"] == {"accepted", "rejected", "withdrawn"}
    assert TRANSITIONS["accepted"] == {"implemented", "superseded", "deprecated"}
    assert TRANSITIONS["implemented"] == {"superseded", "reverted", "deprecated"}
    assert TRANSITIONS["reverted"] == {"reopened"}
    for terminal in ("rejected", "withdrawn", "superseded", "deprecated"):
        assert TRANSITIONS[terminal] == frozenset()


def test_transitions_only_use_known_lifecycle_events() -> None:
    targets = set().union(*TRANSITIONS.values())
    assert targets <= set(LIFECYCLE_EVENTS)
    # every lifecycle event can be reached; "reopened" is a move, not a state
    assert targets == set(LIFECYCLE_EVENTS)
    assert "reopened" not in TRANSITIONS
    assert set(TRANSITIONS) - {None} <= set(LIFECYCLE_STATES)


def test_annotations_match_the_model_vocabulary() -> None:
    assert set(ANNOTATION_EVENTS) == ANNOTATIONS
    assert not ANNOTATIONS & set(LIFECYCLE_EVENTS)


def test_state_after_maps_events_to_states() -> None:
    assert state_after("implemented") == "implemented"
    assert state_after("reopened") == "proposed"
    with pytest.raises(KeyError):
        state_after("discussed")


def test_is_lifecycle_separates_annotations() -> None:
    event = TimelineEvent(event="discussed", on=_day("2026-03-05"), source="s")
    assert not is_lifecycle(event)
    assert is_lifecycle(event.model_copy(update={"event": "accepted"}))


# --------------------------------------------------------------------------------------
# valid_time and recorded_time
# --------------------------------------------------------------------------------------


def test_valid_time_prefers_effective_over_on() -> None:
    adr14 = _decision(_scenario(), "ADR-014")
    proposed, accepted = adr14.timeline
    assert valid_time(proposed) == _day("2026-05-28")
    assert valid_time(accepted) == _day("2026-06-01")  # effective, not on (2026-06-10)


def test_recorded_time_is_the_later_of_on_and_source_availability() -> None:
    s = _scenario()
    adr14 = _decision(s, "ADR-014")
    proposed, accepted = adr14.timeline
    assert recorded_time(proposed, s) == _day("2026-05-28")  # source available on its date
    assert recorded_time(accepted, s) == _day("2026-06-11")  # notes imported a day later


def test_recorded_time_never_precedes_the_event_date() -> None:
    s = _scenario()
    pr_event = _decision(s, "ADR-007").timeline[3]  # on 03-12; the PR source is available 03-12
    assert recorded_time(pr_event, s) == _day("2026-03-12")
    early = pr_event.model_copy(update={"on": _day("2026-04-01")})
    assert recorded_time(early, s) == _day("2026-04-01")


def test_recorded_time_with_a_missing_source_raises_key_error() -> None:
    s = _scenario()
    ghost = TimelineEvent(event="proposed", on=_day("2026-03-03"), source="no-such-source")
    with pytest.raises(KeyError):
        recorded_time(ghost, s)


# --------------------------------------------------------------------------------------
# state_as_of: the examples of requirements 14.4 to 14.6
# --------------------------------------------------------------------------------------


def test_adr014_backdating_at_the_reference_date() -> None:
    """Requirement 14.4."""
    s = _scenario()
    today = s.meta.reference_date
    assert state_as_of(_decision(s, "ADR-014"), _day("2026-06-05"), today, s) == "accepted"
    assert state_as_of(_decision(s, "ADR-004"), _day("2026-06-05"), today, s) == "superseded"


def test_adr014_backdating_as_recorded_before_the_notes_were_imported() -> None:
    """Requirement 14.5."""
    s = _scenario()
    recorded = _day("2026-06-08")
    assert state_as_of(_decision(s, "ADR-014"), _day("2026-06-05"), recorded, s) == "proposed"
    assert state_as_of(_decision(s, "ADR-004"), _day("2026-06-05"), recorded, s) == "implemented"


def test_adr007_archaeology_question() -> None:
    """Requirement 14.6."""
    s = _scenario()
    march_10 = _day("2026-03-10")
    assert state_as_of(_decision(s, "ADR-007"), march_10, march_10, s) == "accepted"


def test_state_is_absent_before_the_first_event() -> None:
    s = _scenario()
    adr7 = _decision(s, "ADR-007")
    assert state_as_of(adr7, _day("2026-03-02"), s.meta.reference_date, s) == "absent"
    # valid on 03-03, but recorded only on 03-03
    assert state_as_of(adr7, _day("2026-03-03"), _day("2026-03-02"), s) == "absent"
    assert state_as_of(adr7, _day("2026-03-03"), _day("2026-03-03"), s) == "proposed"


def test_annotation_events_never_change_the_state() -> None:
    s = _scenario()
    adr7 = _decision(s, "ADR-007")  # has "discussed" on 03-05 between proposed and accepted
    assert state_as_of(adr7, _day("2026-03-05"), _day("2026-03-05"), s) == "proposed"


def test_events_on_the_boundary_dates_are_included() -> None:
    s = _scenario()
    adr7 = _decision(s, "ADR-007")
    assert state_as_of(adr7, _day("2026-03-07"), _day("2026-03-07"), s) == "accepted"
    assert state_as_of(adr7, _day("2026-03-06"), _day("2026-03-07"), s) == "proposed"


def test_ties_on_valid_time_keep_list_order() -> None:
    raw = _raw()
    adr7 = raw["decisions"][1]
    # accepted and implemented on the same valid day, both recorded by then
    adr7["timeline"][3]["on"] = "2026-03-07"
    adr7["timeline"][3]["source"] = "mtg-arch"
    s = _scenario(raw)
    day = _day("2026-03-07")
    assert state_as_of(_decision(s, "ADR-007"), day, day, s) == "implemented"


def test_reopened_leads_back_to_proposed() -> None:
    raw = _raw()
    raw["decisions"][1]["timeline"] += [
        {"event": "reverted", "on": "2026-04-01", "source": "doc-payment-db"},
        {"event": "reopened", "on": "2026-04-10", "source": "doc-payment-db"},
    ]
    s = _scenario(raw)
    adr7 = _decision(s, "ADR-007")
    assert state_as_of(adr7, _day("2026-04-05"), s.meta.reference_date, s) == "reverted"
    assert state_as_of(adr7, _day("2026-04-10"), s.meta.reference_date, s) == "proposed"


def test_recorded_axis_is_monotone_on_an_example() -> None:
    """Raising the recorded date only adds events, so a decision never goes back to absent."""
    s = _scenario()
    adr14 = _decision(s, "ADR-014")
    valid = _day("2026-06-05")
    states = [
        state_as_of(adr14, valid, _day(d), s)
        for d in ("2026-05-01", "2026-05-28", "2026-06-10", "2026-06-11", "2026-10-27")
    ]
    assert states == ["absent", "proposed", "proposed", "accepted", "accepted"]


def test_state_as_of_is_pure() -> None:
    s = _scenario()
    adr14 = _decision(s, "ADR-014")
    first = state_as_of(adr14, _day("2026-06-05"), _day("2026-06-30"), s)
    assert first == state_as_of(adr14, _day("2026-06-05"), _day("2026-06-30"), s)
    assert _decision(s, "ADR-014") == adr14
