"""Unit tests for the scenario validator rules and the ``cairn-scenario`` CLI
(design §12; requirements 13.1 to 13.24).

``data/mini_scenario.yaml`` is a small scenario that passes every rule. Each case below
copies it, applies one change and checks which rule reports it (and where).
"""

import copy
import datetime as dt
import json
from pathlib import Path
from typing import Any

import pytest

from cairn_eval.scenario.access import PERSONA_IDS, personas, readers, team_members
from cairn_eval.scenario.cli import main as cli_main
from cairn_eval.scenario.loader import load_scenario_text
from cairn_eval.scenario.model import Decision, Scenario
from cairn_eval.scenario.rules import SEMANTIC_RULES, check_semantics, lifecycle_violations
from cairn_eval.scenario.validator import validate, validate_file
from cairn_eval.scenario.violation import Violation, finalize, pointer

MINI = Path(__file__).parent / "data" / "mini_scenario.yaml"

Op = tuple[str, str, Any]  # ("put" | "drop" | "append", JSON pointer, value)


def _load() -> dict[str, Any]:
    raw = load_scenario_text(MINI.read_text(encoding="utf-8"), source=str(MINI))
    assert isinstance(raw, dict)
    return raw


_BASE = _load()


def good() -> dict[str, Any]:
    """A fresh copy of the passing scenario."""
    return copy.deepcopy(_BASE)


def _locate(raw: dict[str, Any], path: str) -> tuple[Any, Any]:
    parts = path.strip("/").split("/")
    node: Any = raw
    for part in parts[:-1]:
        node = node[int(part)] if isinstance(node, list) else node[part]
    return node, (int(parts[-1]) if isinstance(node, list) else parts[-1])


def apply(raw: dict[str, Any], ops: list[Op]) -> dict[str, Any]:
    for op, path, value in ops:
        node, key = _locate(raw, path)
        if op == "put":
            node[key] = value
        elif op == "drop":
            del node[key]
        elif op == "append":
            node[key].append(value)
        else:  # pragma: no cover - a typo in a test case
            raise AssertionError(op)
    return raw


def rules_of(violations: list[Violation]) -> set[str]:
    return {v.rule for v in violations}


# --------------------------------------------------------------------------------------
# The passing scenario
# --------------------------------------------------------------------------------------


def test_mini_scenario_passes_every_rule() -> None:
    assert validate(good()) == []


def test_every_semantic_rule_function_is_listed_once() -> None:
    assert len(SEMANTIC_RULES) == len({rule.__name__ for rule in SEMANTIC_RULES}) == 19


def test_violations_sort_by_rule_then_path_without_duplicates() -> None:
    a, b, c = (
        Violation("TL-1", "/b", "x"),
        Violation("ID-1", "/z", "y"),
        Violation("TL-1", "/a", "z"),
    )
    assert finalize([a, b, c, a]) == [b, c, a]


def test_pointer_escapes_special_characters() -> None:
    assert pointer("a/b", "c~d", 3) == "/a~1b/c~0d/3"
    assert pointer() == ""


# --------------------------------------------------------------------------------------
# One focused example per rule: (case id, rule, operations, pointer of the violation or None)
# --------------------------------------------------------------------------------------

ORPHAN_TWIST = {"id": "TW-ORPHAN", "kind": "partial_revert", "decisions": ["ADR-007"]}
UNPLACED_CANARY = {
    "id": "CAN-INC-02",
    "family": "incident",
    "kind": "token",
    "allowed_scopes": ["channel:incidents-payments"],
}
PR_COPY = {
    "id": "pr-copy",
    "kind": "pull_request",
    "date": "2026-03-10",
    "location": {"repo": "payment-service"},
    "pr": {"number": 123, "state": "open"},
}
ONE_SIDED_CONFLICT = {
    "id": "K1",
    "subject": "database",
    "sides": [
        {"value": "postgres", "source": "doc-payment-db", "tier": "T3"},
        {"value": "postgres", "source": "mtg-arch", "tier": "T2"},
    ],
}
SUBSTRING_PHRASE_CANARY = {
    "id": "CAN-PARTNER-03",
    "family": "partner",
    "kind": "phrase",
    "phrase": "Larkspur",
    "allowed_scopes": ["meeting:mtg-arch"],
}
RULE_CASES: list[tuple[str, str, list[Op], str | None]] = [
    # SCH (JSON Schema)
    ("sch-missing-key", "SCH", [("drop", "/people", None)], ""),
    ("sch-unknown-key", "SCH", [("put", "/meta/extra", 1)], "/meta"),
    ("sch-bad-id", "SCH", [("put", "/decisions/0/id", "ADR-4")], "/decisions/0/id"),
    (
        "sch-impossible-date",
        "SCH",
        [("put", "/meta/reference_date", "2026-02-30")],
        "/meta/reference_date",
    ),
    # SCH (models only)
    (
        "sch-principal-needs-one",
        "SCH",
        [("put", "/repos/0/access/0", {"permission": "read"})],
        "/repos/0/access/0",
    ),
    (
        "sch-phrase-canary-needs-phrase",
        "SCH",
        [("drop", "/canaries/2/phrase", None)],
        "/canaries/2",
    ),
    (
        "sch-bad-timestamp",
        "SCH",
        [("put", "/sources/1/messages/0/at", "2026-13-45T00:00:00Z")],
        "/sources/1/messages/0/at",
    ),
    # ID-1
    ("id-duplicate-person", "ID-1", [("put", "/people/5/id", "asha")], "/people/5/id"),
    (
        "id-duplicate-claim",
        "ID-1",
        [("put", "/decisions/1/claims/1/id", "C1")],
        "/decisions/1/claims/1/id",
    ),
    ("id-duplicate-pr-number", "ID-1", [("append", "/sources", PR_COPY)], "/sources/12/pr/number"),
    # REF-1
    (
        "ref-membership-team",
        "REF-1",
        [("put", "/memberships/0/team", "ghosts")],
        "/memberships/0/team",
    ),
    (
        "ref-event-source",
        "REF-1",
        [("put", "/decisions/1/timeline/0/source", "nope")],
        "/decisions/1/timeline/0/source",
    ),
    (
        "ref-anchor-pr",
        "REF-1",
        [("put", "/decisions/1/anchors/prs/0", "payment-service#999")],
        "/decisions/1/anchors/prs/0",
    ),
    (
        "ref-canary-scope",
        "REF-1",
        [("put", "/canaries/0/allowed_scopes/0", "channel:ghost")],
        "/canaries/0/allowed_scopes/0",
    ),
    (
        "ref-question-twist",
        "REF-1",
        [("put", "/questions/0/twists/0", "TW-NOPE")],
        "/questions/0/twists/0",
    ),
    (
        "ref-wrong-kind",
        "REF-1",
        [("put", "/sources/3/location/repo", "incidents-payments")],
        "/sources/3/location/repo",
    ),
    # MEM-1
    (
        "mem-ends-before-start",
        "MEM-1",
        [("put", "/memberships/1/to", "2026-03-01")],
        "/memberships/1",
    ),
    (
        "mem-overlap",
        "MEM-1",
        [("append", "/memberships", {"person": "asha", "team": "payments", "from": "2026-06-01"})],
        "/memberships/6",
    ),
    (
        "mem-future-start",
        "MEM-1",
        [("append", "/memberships", {"person": "dev", "team": "mobile", "from": "2027-01-01"})],
        "/memberships/6",
    ),
    # SRC-1
    (
        "src-available-before-date",
        "SRC-1",
        [("put", "/sources/6/available", "2026-03-01")],
        "/sources/6/available",
    ),
    (
        "src-available-after-reference",
        "SRC-1",
        [("put", "/sources/2/available", "2027-01-01")],
        "/sources/2/available",
    ),
    (
        "src-deleted-before-date",
        "SRC-1",
        [("put", "/sources/11/deleted", "2026-01-01")],
        "/sources/11/deleted",
    ),
    (
        "src-edited-before-date",
        "SRC-1",
        [("put", "/sources/10/last_edited", "2026-01-01")],
        "/sources/10/last_edited",
    ),
    (
        "src-thread-in-a-repo",
        "SRC-1",
        [("put", "/sources/4/location", {"repo": "payment-service"})],
        "/sources/4/location",
    ),
    (
        "src-merged-without-time",
        "SRC-1",
        [("drop", "/sources/6/pr/merged_at", None)],
        "/sources/6/pr",
    ),
    # TL-1
    (
        "tl1-out-of-order",
        "TL-1",
        [("put", "/decisions/1/timeline/3/on", "2026-03-02")],
        "/decisions/1/timeline/3",
    ),
    (
        "tl1-future-event",
        "TL-1",
        [("put", "/decisions/1/timeline/3/on", "2027-01-01")],
        "/decisions/1/timeline/3",
    ),
    # TL-2
    (
        "tl2-repeated-state",
        "TL-2",
        [("put", "/decisions/1/timeline/3/event", "accepted")],
        "/decisions/1/timeline/3",
    ),
    (
        "tl2-annotation-first",
        "TL-2",
        [("put", "/decisions/1/timeline/0/event", "discussed")],
        "/decisions/1/timeline/0",
    ),
    ("tl2-empty-timeline", "TL-2", [("put", "/decisions/0/timeline", [])], "/decisions/0/timeline"),
    # TL-3
    (
        "tl3-event-before-source",
        "TL-3",
        [("put", "/decisions/1/timeline/2/on", "2026-03-06")],
        "/decisions/1/timeline/2",
    ),
    (
        "tl3-not-the-merge-date",
        "TL-3",
        [("put", "/decisions/1/timeline/3/on", "2026-03-13")],
        "/decisions/1/timeline/3",
    ),
    # TL-4
    (
        "tl4-backdated-without-twist",
        "TL-4",
        [("put", "/decisions/1/timeline/2/effective", "2026-03-01")],
        "/decisions/1/timeline/2",
    ),
    (
        "tl4-scheduled-without-twist",
        "TL-4",
        [("put", "/decisions/1/timeline/2/effective", "2026-03-20")],
        "/decisions/1/timeline/2",
    ),
    ("tl4-twist-removed", "TL-4", [("put", "/twists/1/kind", "drift")], "/decisions/2/timeline/1"),
    (
        "tl4-valid-order-differs",
        "TL-4",
        [("put", "/decisions/0/timeline/3/effective", "2025-01-01")],
        "/decisions/0/timeline/3",
    ),
    # TL-5
    (
        "tl5-one-sided-link",
        "TL-5",
        [("put", "/decisions/2/supersedes", [])],
        "/decisions/0/superseded_by/0",
    ),
    (
        "tl5-dates-differ",
        "TL-5",
        [("put", "/decisions/0/timeline/3/effective", "2026-06-02")],
        "/decisions/0/timeline/3",
    ),
    (
        "tl5-no-superseded-event",
        "TL-5",
        [("drop", "/decisions/0/timeline/3", None)],
        "/decisions/0/timeline",
    ),
    # CLM-1
    (
        "clm-no-support",
        "CLM-1",
        [("put", "/decisions/1/claims/0/support_sets", [])],
        "/decisions/1/claims/0",
    ),
    (
        "clm-empty-support-set",
        "CLM-1",
        [("put", "/decisions/1/claims/0/support_sets", [[]])],
        "/decisions/1/claims/0/support_sets/0",
    ),
    (
        "clm-one-sided-conflict",
        "CLM-1",
        [("put", "/decisions/1/conflicts", [ONE_SIDED_CONFLICT])],
        "/decisions/1/conflicts/0",
    ),
    # TW-1 and TW-2
    ("tw1-orphan-twist", "TW-1", [("append", "/twists", ORPHAN_TWIST)], "/twists/8"),
    ("tw2-deleted-source", "TW-2", [("put", "/twists/2/sources", ["doc-payment-db"])], "/twists/2"),
    (
        "tw2-stale-doc-no-newer-t1",
        "TW-2",
        [("put", "/twists/3/sources", ["wiki-onboarding", "doc-payment-db"])],
        "/twists/3",
    ),
    (
        "tw2-stale-doc-no-doc",
        "TW-2",
        [("put", "/twists/3/sources", ["pr-payment-service-123"])],
        "/twists/3",
    ),
    ("tw2-team-move", "TW-2", [("put", "/twists/4/people", ["asha"])], "/twists/4"),
    (
        "tw2-injected-private-repo",
        "TW-2",
        [("put", "/sources/9/location/repo", "payment-service")],
        "/twists/5",
    ),
    (
        "tw2-restricted-in-public",
        "TW-2",
        [("put", "/decisions/3/timeline/0/source", "thread-platform")],
        "/twists/6",
    ),
    # CAN-1 .. CAN-4
    ("can1-unplaced", "CAN-1", [("append", "/canaries", UNPLACED_CANARY)], "/canaries/4"),
    (
        "can2-outside-allowed-scope",
        "CAN-2",
        [("put", "/sources/3/body", "Leak {{canary:CAN-INC-01}}")],
        "/sources/3/body",
    ),
    (
        "can2-unknown-placeholder",
        "CAN-2",
        [("put", "/sources/3/body", "x {{canary:CAN-HR-09}}")],
        "/sources/3/body",
    ),
    (
        "can2-placeholder-in-question",
        "CAN-2",
        [("put", "/questions/0/text", "About {{canary:CAN-INC-01}}?")],
        "/questions/0/text",
    ),
    (
        "can2-phrase-in-question",
        "CAN-2",
        [("put", "/questions/0/text", "Who is larkspur freight?")],
        "/questions/0/text",
    ),
    (
        "can3-literal-token",
        "CAN-3",
        [("put", "/sources/3/body", "KP-INCIDENT-AAAAAAAAAAAAAAAA")],
        "/sources/3/body",
    ),
    ("can3-family-mismatch", "CAN-3", [("put", "/canaries/0/family", "hr")], "/canaries/0"),
    (
        "can3-phrase-inside-phrase",
        "CAN-3",
        [("append", "/canaries", SUBSTRING_PHRASE_CANARY)],
        "/canaries/4/phrase",
    ),
    (
        "can4-public-scope",
        "CAN-4",
        [("put", "/canaries/0/allowed_scopes", ["channel:payments-eng"])],
        "/canaries/0/allowed_scopes/0",
    ),
    (
        "can4-everyone-can-read",
        "CAN-4",
        [
            (
                "put",
                "/sources/5/location/meeting/attendees",
                ["asha", "ben", "dev", "priya", "carla"],
            )
        ],
        "/canaries/2/allowed_scopes/0",
    ),
    # Q-1 .. Q-3
    ("q1-unknown-asker", "Q-1", [("put", "/questions/0/asker", "nobody")], "/questions/0/asker"),
    (
        "q1-unknown-agent-person",
        "Q-1",
        [("put", "/questions/6/asker", "agent:nobody")],
        "/questions/6/asker",
    ),
    (
        "q1-future-as-of",
        "Q-1",
        [("put", "/questions/0/as_of/valid", "2027-01-01")],
        "/questions/0/as_of/valid",
    ),
    (
        "q2-asker-can-read",
        "Q-2",
        [("put", "/questions/7/asker", "priya")],
        "/questions/7/expected/must_not_contain/0",
    ),
    (
        "q2-family-uncovered",
        "Q-2",
        [("put", "/questions/8/expected/must_not_contain", [])],
        "/canaries/1",
    ),
    (
        "q3-wrong-lifecycle",
        "Q-3",
        [("put", "/questions/1/expected/lifecycle/ADR-004", "implemented")],
        "/questions/1/expected/lifecycle/ADR-004",
    ),
    (
        "q3-must-not-cite-already-recorded",
        "Q-3",
        [("put", "/questions/0/as_of/recorded", "2026-03-12")],
        "/questions/0/expected/must_not_cite/0",
    ),
]


@pytest.mark.parametrize(
    ("rule", "ops", "path"),
    [pytest.param(rule, ops, path, id=case) for case, rule, ops, path in RULE_CASES],
)
def test_rule_reports_its_defect(rule: str, ops: list[Op], path: str | None) -> None:
    violations = validate(apply(good(), ops))
    reported = [v for v in violations if v.rule == rule]
    assert reported, f"{rule} not reported; got {violations}"
    if path is not None:
        assert path in {v.path for v in reported}, f"{rule} at {[v.path for v in reported]}"


def test_every_rule_id_has_a_focused_example() -> None:
    expected = {
        "SCH", "ID-1", "REF-1", "MEM-1", "SRC-1", "TL-1", "TL-2", "TL-3", "TL-4", "TL-5",
        "CLM-1", "TW-1", "TW-2", "CAN-1", "CAN-2", "CAN-3", "CAN-4", "Q-1", "Q-2", "Q-3",
    }  # fmt: skip
    assert {rule for _, rule, _, _ in RULE_CASES} == expected


# --------------------------------------------------------------------------------------
# Details that the table cannot show
# --------------------------------------------------------------------------------------


def test_schema_errors_stop_before_the_semantic_rules() -> None:
    raw = apply(good(), [("put", "/meta/extra", 1), ("put", "/memberships/1/to", "2026-03-01")])
    assert rules_of(validate(raw)) == {"SCH"}


def test_a_document_that_is_not_a_mapping_is_one_schema_violation_at_the_root() -> None:
    assert [(v.rule, v.path) for v in validate([])] == [("SCH", "")]
    assert [v.rule for v in validate(None)] == ["SCH"]


def test_a_single_defect_reports_only_the_expected_rules() -> None:
    raw = apply(good(), [("put", "/decisions/1/timeline/3/on", "2026-03-02")])
    assert rules_of(validate(raw)) <= {"TL-1", "TL-3", "TL-4"}


def test_back_to_back_memberships_do_not_overlap() -> None:
    raw = apply(
        good(),
        [("append", "/memberships", {"person": "ben", "team": "mobile", "from": "2026-09-01"})],
    )
    # mobile [03-02, 09-01) then mobile from 09-01: adjacent, so allowed
    assert validate(raw) == []


def test_validation_is_deterministic_sorted_and_duplicate_free() -> None:
    raw = apply(
        good(),
        [
            ("put", "/decisions/1/timeline/3/on", "2026-03-02"),
            ("put", "/memberships/1/to", "2026-03-01"),
            ("put", "/questions/0/asker", "nobody"),
            ("append", "/twists", ORPHAN_TWIST),
        ],
    )
    first = validate(copy.deepcopy(raw))
    second = validate(copy.deepcopy(raw))
    assert first == second
    assert first == sorted(set(first))
    assert len(rules_of(first)) >= 4


def test_validation_ignores_the_order_of_top_level_collections() -> None:
    raw = good()
    shuffled = {key: raw[key] for key in reversed(list(raw))}
    assert validate(shuffled) == []


def test_consistent_renaming_of_an_id_keeps_the_scenario_valid() -> None:
    text = MINI.read_text(encoding="utf-8").replace("thread-platform", "thread-roadmap")
    raw = load_scenario_text(text)
    assert validate(raw) == []


def test_check_semantics_runs_on_a_built_scenario() -> None:
    assert check_semantics(Scenario.model_validate(good())) == []


# --------------------------------------------------------------------------------------
# Lifecycle rule on hand-built decisions (TL-2)
# --------------------------------------------------------------------------------------


def _decision(events: list[str]) -> Decision:
    timeline = [
        {"event": name, "on": f"2026-01-{i + 1:02d}", "source": "s"}
        for i, name in enumerate(events)
    ]
    return Decision.model_validate({"id": "ADR-001", "title": "t", "timeline": timeline})


@pytest.mark.parametrize(
    "events",
    [
        ["proposed"],
        ["proposed", "accepted", "implemented", "superseded"],
        ["proposed", "discussed", "rejected", "amended"],
        ["proposed", "accepted", "implemented", "partially_reverted", "reverted", "reopened"],
        ["proposed", "accepted", "implemented", "reverted", "reopened", "accepted", "deprecated"],
    ],
)
def test_lifecycle_paths_of_the_state_machine_pass(events: list[str]) -> None:
    assert list(lifecycle_violations(_decision(events), "/d")) == []


@pytest.mark.parametrize(
    ("events", "position"),
    [
        (["accepted"], 0),
        (["proposed", "implemented"], 1),
        (["proposed", "rejected", "accepted"], 2),
        (["proposed", "accepted", "reopened"], 2),
        (["proposed", "accepted", "implemented", "reverted", "accepted"], 4),
    ],
)
def test_lifecycle_steps_outside_the_state_machine_are_reported_at_their_position(
    events: list[str], position: int
) -> None:
    found = list(lifecycle_violations(_decision(events), "/d"))
    assert [v.path for v in found] == [f"/d/timeline/{position}"]
    assert found[0].rule == "TL-2"


# --------------------------------------------------------------------------------------
# Access helpers
# --------------------------------------------------------------------------------------


def test_team_membership_is_time_aware_and_half_open() -> None:
    s = Scenario.model_validate(good())
    assert team_members(s, "mobile", dt.date(2026, 8, 31)) == {"ben"}
    assert team_members(s, "mobile", dt.date(2026, 9, 1)) == frozenset()
    assert "ben" in team_members(s, "payments", dt.date(2026, 9, 1))
    assert "ben" not in team_members(s, "payments", dt.date(2026, 8, 31))


def test_readers_of_each_scope_kind() -> None:
    s = Scenario.model_validate(good())
    today = s.meta.reference_date
    assert readers(s, "repo:kestrel-sdk", today) == {p.id for p in s.people}  # public repo
    assert readers(s, "repo:security-policies", today) == {"priya"}
    assert readers(s, "channel:payments-eng", today) == {"asha", "ben", "dev", "priya", "carla"}
    assert readers(s, "channel:incidents-payments", today) == {"asha", "ben", "dev"}
    assert readers(s, "meeting:mtg-arch", today) == {"asha", "ben", "dev"}
    assert readers(s, "channel:ghost", today) == frozenset()
    # Ben was in mobile, not payments, in July
    assert "ben" not in readers(s, "channel:incidents-payments", dt.date(2026, 7, 1))


def test_personas_fall_back_to_all_people_when_none_is_defined() -> None:
    s = Scenario.model_validate(good())
    assert personas(s) == set(PERSONA_IDS)
    renamed = good()
    for i, person in enumerate(renamed["people"]):
        person["id"] = f"person{chr(97 + i)}"
    assert personas(Scenario.model_validate(renamed)) == {f"person{c}" for c in "abcdef"}


# --------------------------------------------------------------------------------------
# validate_file and the CLI
# --------------------------------------------------------------------------------------


def test_validate_file_reads_the_scenario() -> None:
    assert validate_file(MINI) == []


def test_cli_exits_0_with_a_summary_for_a_valid_scenario(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert cli_main(["validate", str(MINI)]) == 0
    out = capsys.readouterr().out
    assert "OK" in out
    assert "12 sources" in out


def test_cli_exits_1_and_lists_violations_sorted(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    raw = apply(
        good(),
        [("put", "/decisions/1/timeline/3/on", "2026-03-02"), ("append", "/twists", ORPHAN_TWIST)],
    )
    scenario = tmp_path / "scenario.yaml"
    scenario.write_text(json.dumps(raw), encoding="utf-8")  # JSON is YAML
    assert cli_main(["validate", str(scenario)]) == 1
    lines = capsys.readouterr().out.splitlines()
    assert "violation(s) in" in lines[-1]
    rules = [line.split()[0] for line in lines[:-1]]
    assert rules == sorted(rules)
    assert "TL-1" in rules
    assert "TW-1" in rules
    assert any("/decisions/1/timeline/3" in line for line in lines)


def test_cli_json_format_reports_violations(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    raw = apply(good(), [("append", "/twists", ORPHAN_TWIST)])
    scenario = tmp_path / "scenario.yaml"
    scenario.write_text(json.dumps(raw), encoding="utf-8")
    assert cli_main(["validate", str(scenario), "--format", "json"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["ok"] is False
    assert report["violations"] == [
        {"rule": "TW-1", "path": "/twists/8", "message": "no gold question exercises TW-ORPHAN"}
    ]


def test_cli_json_format_for_a_valid_scenario(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli_main(["validate", str(MINI), "--format", "json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["ok"] is True
    assert report["violations"] == []
    assert report["summary"]["decisions"] == 4


def test_cli_exits_2_for_a_missing_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli_main(["validate", str(tmp_path / "missing.yaml")]) == 2
    assert "cannot read file" in capsys.readouterr().err


def test_cli_exits_2_for_yaml_syntax_errors_and_reports_the_position(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text("people: [a, b\nteams: []\n", encoding="utf-8")
    assert cli_main(["validate", str(bad)]) == 2
    err = capsys.readouterr().err
    assert str(bad) in err
    assert f"{bad}:2:6:" in err  # 1-based line and column of the parser's complaint


def test_cli_json_format_for_an_unreadable_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text("a: 1\na: 2\n", encoding="utf-8")
    assert cli_main(["validate", str(bad), "--format", "json"]) == 2
    report = json.loads(capsys.readouterr().out)
    assert report["ok"] is False
    assert report["error"]["line"] == 2
    assert "duplicate key" in report["error"]["message"]


def test_cli_reports_an_empty_document_as_a_schema_violation(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    empty = tmp_path / "empty.yaml"
    empty.write_text("", encoding="utf-8")
    # an empty file parses to None: a schema violation, not an unreadable file
    assert cli_main(["validate", str(empty)]) == 1
    assert "SCH" in capsys.readouterr().out


def test_cli_usage_error_exits_2() -> None:
    with pytest.raises(SystemExit) as info:
        cli_main(["validate"])
    assert info.value.code == 2
