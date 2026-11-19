"""Unit tests for the scenario models, the timestamp-free loader and the committed schema
(design §11, §12; requirements 11.1, 13.28)."""

import copy
import datetime as dt
from pathlib import Path
from typing import Any

import pytest
import yaml
from jsonschema import Draft202012Validator, FormatChecker
from pydantic import ValidationError

from cairn_eval.scenario.loader import ScenarioLoadError, load_scenario, load_scenario_text
from cairn_eval.scenario.model import (
    ANNOTATION_EVENTS,
    LIFECYCLE_EVENTS,
    TWIST_KINDS,
    Canary,
    PrincipalRef,
    Scenario,
    Source,
)
from cairn_eval.scenario.schema import (
    generate_schema,
    render_schema,
    schema_path,
)
from cairn_eval.scenario.schema import (
    main as schema_main,
)

SAMPLE_YAML = """\
schema_version: 1
meta: {id: tiny, name: Tiny, reference_date: 2026-10-27, canary_seed: tiny-seed}
people:
  - {id: asha, name: Asha, title: Engineer}
teams:
  - {id: payments}
memberships:
  - {person: asha, team: payments, from: 2026-03-02, to: 2026-09-01}
repos:
  - {id: payment-service, visibility: private, access: [{team: payments, permission: write}]}
channels:
  - {id: incidents-payments, private: true, members: [{team: payments}, {person: asha}]}
sources:
  - id: thread-1
    kind: chat_thread
    date: 2026-02-18
    location: {channel: incidents-payments}
    messages:
      - author: asha
        at: 2026-02-18T09:12:00Z
        text: "Second double refund. {{canary:CAN-INC-01}}"
  - id: pr-1
    kind: pull_request
    date: 2026-03-10
    available: 2026-03-11
    location: {repo: payment-service}
    pr: {number: 123, state: merged, merged_at: 2026-03-12T15:04:00Z, labels: [decision]}
decisions:
  - id: ADR-007
    title: Migrate to PostgreSQL
    timeline:
      - {event: proposed, on: 2026-03-03, source: thread-1}
      - {event: accepted, on: 2026-03-07, effective: 2026-03-01, source: thread-1}
    claims:
      - {id: C1, text: Consistency matters., tier: T3, support_sets: [[thread-1]]}
twists:
  - {id: TW-ADR007-ARCHAEOLOGY, kind: cross_source_archaeology, decisions: [ADR-007]}
canaries:
  - id: CAN-INC-01
    family: incident
    kind: token
    allowed_scopes: ["channel:incidents-payments"]
questions:
  - id: Q001
    split: dev
    category: as_of
    asker: asha
    text: What was the plan on 10 Mar?
    as_of: {valid: 2026-03-10, recorded: 2026-03-10}
    twists: [TW-ADR007-ARCHAEOLOGY]
    expected: {decisions: [ADR-007], lifecycle: {ADR-007: accepted}}
"""


def sample_raw() -> dict[str, Any]:
    raw = load_scenario_text(SAMPLE_YAML)
    assert isinstance(raw, dict)
    return raw


# ---------------------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------------------


def test_loader_keeps_dates_timestamps_and_on_as_strings() -> None:
    raw = load_scenario_text("d: 2026-03-07\nt: 2026-02-18T09:12:00Z\nev: {on: 2026-03-07}\n")
    assert raw == {"d": "2026-03-07", "t": "2026-02-18T09:12:00Z", "ev": {"on": "2026-03-07"}}


def test_loader_only_treats_true_and_false_as_booleans() -> None:
    raw = load_scenario_text("a: true\nb: False\nc: yes\nd: off\ne: no\nf: ~\ng: 12\n")
    assert raw == {"a": True, "b": False, "c": "yes", "d": "off", "e": "no", "f": None, "g": 12}


def test_loader_does_not_change_stock_safe_load() -> None:
    assert yaml.safe_load("d: 2026-03-07") == {"d": dt.date(2026, 3, 7)}
    assert yaml.safe_load("a: on") == {"a": True}


def test_loader_rejects_duplicate_keys_with_a_position() -> None:
    with pytest.raises(ScenarioLoadError, match="duplicate key 'id'") as info:
        load_scenario_text("people:\n  - id: a\n    id: b\n", source="x.yaml")
    assert (info.value.source, info.value.line) == ("x.yaml", 3)


def test_loader_reports_syntax_errors_with_a_position() -> None:
    with pytest.raises(ScenarioLoadError) as info:
        load_scenario_text("a: [1, 2\nb: 3\n", source="bad.yaml")
    assert info.value.line is not None
    assert info.value.column is not None
    assert str(info.value).startswith("bad.yaml:")


def test_loader_rejects_python_object_tags() -> None:
    with pytest.raises(ScenarioLoadError):
        load_scenario_text("a: !!python/object/apply:os.getcwd []\n")


def test_loader_rejects_multiple_documents() -> None:
    with pytest.raises(ScenarioLoadError):
        load_scenario_text("a: 1\n---\nb: 2\n")


def test_loader_missing_file_is_a_load_error(tmp_path: Path) -> None:
    with pytest.raises(ScenarioLoadError, match="cannot read file"):
        load_scenario(tmp_path / "missing.yaml")


def test_loader_reads_a_file(tmp_path: Path) -> None:
    file = tmp_path / "s.yaml"
    file.write_text(SAMPLE_YAML, encoding="utf-8")
    assert load_scenario(file) == sample_raw()


def test_loader_empty_document_is_none() -> None:
    assert load_scenario_text("") is None


# ---------------------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------------------


def test_sample_parses_into_typed_models() -> None:
    s = Scenario.model_validate(sample_raw())
    assert s.meta.reference_date == dt.date(2026, 10, 27)
    assert s.memberships[0].from_ == dt.date(2026, 3, 2)
    assert s.memberships[0].to == dt.date(2026, 9, 1)
    adr = s.decisions[0]
    assert (adr.timeline[1].on, adr.timeline[1].effective) == (
        dt.date(2026, 3, 7),
        dt.date(2026, 3, 1),
    )
    assert s.sources[0].messages[0].at == dt.datetime(2026, 2, 18, 9, 12, tzinfo=dt.UTC)
    assert s.sources[1].pr is not None
    assert s.sources[1].pr.merged_at == dt.datetime(2026, 3, 12, 15, 4, tzinfo=dt.UTC)
    assert s.questions[0].expected.lifecycle == {"ADR-007": "accepted"}
    assert s.questions[0].expected.abstain is False


def test_models_are_frozen() -> None:
    s = Scenario.model_validate(sample_raw())
    with pytest.raises(ValidationError):
        s.meta.name = "Other"  # type: ignore[misc]


def test_unknown_keys_are_rejected() -> None:
    raw = sample_raw()
    raw["people"][0]["nickname"] = "A"
    with pytest.raises(ValidationError, match="nickname"):
        Scenario.model_validate(raw)


def test_wrong_schema_version_is_rejected() -> None:
    raw = sample_raw()
    raw["schema_version"] = 2
    with pytest.raises(ValidationError):
        Scenario.model_validate(raw)


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("people", 0, "id"), "Asha"),
        (("sources", 0, "id"), "Thread_1"),
        (("decisions", 0, "id"), "ADR-7"),
        (("twists", 0, "id"), "tw-lower"),
        (("canaries", 0, "id"), "CAN-XX-01"),
        (("questions", 0, "id"), "Q42"),
        (("questions", 0, "asker"), "Agent:asha"),
        (("canaries", 0, "allowed_scopes", 0), "team:payments"),
    ],
)
def test_defining_ids_and_patterns_are_enforced(path: tuple[str | int, ...], value: str) -> None:
    raw = sample_raw()
    target: Any = raw
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    with pytest.raises(ValidationError):
        Scenario.model_validate(raw)


def test_references_are_not_pattern_checked() -> None:
    """A dangling or odd reference parses; the validator reports it as REF-1."""
    raw = sample_raw()
    raw["memberships"][0]["team"] = "No Such Team"
    Scenario.model_validate(raw)


@pytest.mark.parametrize("bad", [20260307, "7 Mar 2026", "2026-3-7", "2026-03-07T00:00:00Z", True])
def test_dates_must_be_iso_strings(bad: object) -> None:
    raw = sample_raw()
    raw["meta"]["reference_date"] = bad
    with pytest.raises(ValidationError):
        Scenario.model_validate(raw)


def test_impossible_calendar_date_is_rejected() -> None:
    raw = sample_raw()
    raw["meta"]["reference_date"] = "2026-02-30"
    with pytest.raises(ValidationError):
        Scenario.model_validate(raw)


def test_python_date_objects_are_accepted_but_datetimes_are_not() -> None:
    raw = sample_raw()
    raw["meta"]["reference_date"] = dt.date(2026, 10, 27)
    Scenario.model_validate(raw)
    raw["meta"]["reference_date"] = dt.datetime(2026, 10, 27, tzinfo=dt.UTC)
    with pytest.raises(ValidationError):
        Scenario.model_validate(raw)


@pytest.mark.parametrize("bad", ["2026-02-18T09:12:00", "2026-02-18 09:12:00Z", "yesterday"])
def test_message_timestamps_need_a_zone(bad: str) -> None:
    raw = sample_raw()
    raw["sources"][0]["messages"][0]["at"] = bad
    with pytest.raises(ValidationError):
        Scenario.model_validate(raw)


def test_membership_accepts_field_name_and_alias() -> None:
    raw = sample_raw()
    member = copy.deepcopy(raw["memberships"][0])
    member["from_"] = member.pop("from")
    raw["memberships"].append(member)
    s = Scenario.model_validate(raw)
    assert s.memberships[1].from_ == s.memberships[0].from_


@pytest.mark.parametrize("data", [{}, {"team": "payments", "person": "asha"}])
def test_principal_ref_needs_exactly_one_target(data: dict[str, str]) -> None:
    with pytest.raises(ValidationError):
        PrincipalRef.model_validate(data)
    assert PrincipalRef.model_validate({"team": "payments"}).team == "payments"


def test_access_grant_needs_a_permission() -> None:
    raw = sample_raw()
    del raw["repos"][0]["access"][0]["permission"]
    with pytest.raises(ValidationError):
        Scenario.model_validate(raw)


def test_canary_phrase_must_match_kind() -> None:
    scopes = ["channel:partners-limits"]
    base = {"id": "CAN-PARTNER-02", "family": "partner", "allowed_scopes": scopes}
    assert Canary.model_validate({**base, "kind": "phrase", "phrase": "Larkspur Freight"})
    assert Canary.model_validate({**base, "kind": "token"})
    with pytest.raises(ValidationError):
        Canary.model_validate({**base, "kind": "phrase"})
    with pytest.raises(ValidationError):
        Canary.model_validate({**base, "kind": "token", "phrase": "Larkspur Freight"})


def test_event_vocabularies_follow_the_design() -> None:
    assert LIFECYCLE_EVENTS == (
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
    assert ANNOTATION_EVENTS == ("discussed", "amended", "partially_reverted")
    assert len(TWIST_KINDS) == 16


def test_source_scope_and_available_or_date() -> None:
    s = Scenario.model_validate(sample_raw())
    assert s.source("thread-1").scope == "channel:incidents-payments"
    assert s.source("pr-1").scope == "repo:payment-service"
    assert s.source("pr-1").available_or_date == dt.date(2026, 3, 11)
    assert s.source("thread-1").available_or_date == dt.date(2026, 2, 18)
    share = [{"team": "engineering"}]
    meeting = Source.model_validate(
        {
            "id": "mtg-1",
            "kind": "meeting_note",
            "date": "2026-06-10",
            "location": {"meeting": {"attendees": ["dev"], "share_with": share}},
        }
    )
    assert meeting.scope == "meeting:mtg-1"
    with pytest.raises(KeyError):
        s.source("nope")


# ---------------------------------------------------------------------------------------
# Committed JSON Schema (requirement 13.28)
# ---------------------------------------------------------------------------------------


def test_committed_schema_equals_the_generated_schema() -> None:
    committed = schema_path().read_text(encoding="utf-8")
    assert committed == render_schema(), (
        "scenario-v1.schema.json is out of date; run: uv run python -m cairn_eval.scenario.schema"
    )
    assert schema_main(["--check"]) == 0


def test_generated_schema_is_a_valid_draft_2020_12_schema() -> None:
    Draft202012Validator.check_schema(generate_schema())


def test_sample_validates_against_the_schema() -> None:
    validator = Draft202012Validator(generate_schema(), format_checker=FormatChecker())
    assert list(validator.iter_errors(sample_raw())) == []


def test_schema_uses_file_spellings_and_forbids_unknown_keys() -> None:
    defs = generate_schema()["$defs"]
    assert "from" in defs["Membership"]["properties"]
    assert "from_" not in defs["Membership"]["properties"]
    assert "on" in defs["TimelineEvent"]["properties"]
    assert defs["Person"]["additionalProperties"] is False


@pytest.mark.parametrize(
    "mutate",
    [
        lambda raw: raw["people"][0].update(nickname="A"),
        lambda raw: raw["meta"].update(reference_date="2026-02-30"),
        lambda raw: raw["meta"].update(reference_date="27 Oct 2026"),
        lambda raw: raw["decisions"][0]["timeline"][0].update(event="approved"),
        lambda raw: raw["decisions"][0].update(id="ADR-7"),
        lambda raw: raw.update(schema_version=2),
    ],
)
def test_schema_rejects_common_defects(mutate: Any) -> None:
    raw = sample_raw()
    mutate(raw)
    validator = Draft202012Validator(generate_schema(), format_checker=FormatChecker())
    assert list(validator.iter_errors(raw))
