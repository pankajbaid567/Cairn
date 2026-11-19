"""Property test for the mutation catalog (task 12.8; requirements 13.5, 13.6, 13.7, 13.9,
13.12, 13.15, 13.17, 13.18, 13.22 and 13.23).

Feature: cairn-phase-0-foundations, Property 11: Every single-defect mutation is reported by
its rule

**Validates: Requirements 13.5, 13.6, 13.7, 13.9, 13.12, 13.15, 13.17, 13.18, 13.22, 13.23**

Two valid scenarios serve as bases: the small ``data/mini_scenario.yaml`` and the real
``scenarios/kestrel_pay/scenario.yaml``. Each catalog entry is a Hypothesis strategy that
finds its targets in the raw document (nothing is addressed by a fixed index), applies exactly
one defect to a deep copy, and says which rule must report it and where:

=================  ======  ==========================================================
catalog entry      rule    defect
=================  ======  ==========================================================
duplicate-id       ID-1    an id repeated in a collection (or among a decision's claims)
dangling-ref       REF-1   a reference pointed at an id that does not exist
membership         MEM-1   an interval that ends before it starts, or overlaps another
reordered-timeline TL-1    two events with different dates swapped in the list
unexplained-valid  TL-4    ``effective`` set on an event without the matching twist
orphan-twist       TW-1    every question of a twist deleted
unplaced-canary    CAN-1   every placement of a canary removed from the source text
misplaced-canary   CAN-2   a canary placed in a source outside its allowed scopes
reader-asker       Q-2     a must-not-contain question asked by a reader of the scope
shifted-event      Q-3     a visible event moved past the recorded date of an as-of question
=================  ======  ==========================================================

Three things are written here instead of borrowed from ``cairn_eval`` so that the test does
not agree with the validator by construction: the reader sets (Q-2), the as-of replay (Q-3) and
the list of reference slots (REF-1). The check is that the rule named in the catalog is among
the reported rules and that one of its violations sits at the pointer the defect touched; other
rules may report the same defect as well (a duplicated id also breaks things downstream).
"""

from __future__ import annotations

import copy
import datetime as dt
import re
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from cairn_eval.scenario.loader import load_scenario, load_scenario_text
from cairn_eval.scenario.validator import validate

REPO_ROOT: Final = Path(__file__).resolve().parents[4]
MINI: Final = Path(__file__).parent / "data" / "mini_scenario.yaml"
KESTREL: Final = REPO_ROOT / "scenarios" / "kestrel_pay" / "scenario.yaml"

#: The rules named by the design's mutation catalog.
CATALOG_RULES: Final = frozenset(
    {"ID-1", "REF-1", "MEM-1", "TL-1", "TL-4", "TW-1", "CAN-1", "CAN-2", "Q-2", "Q-3"}
)
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
ID_COLLECTIONS: Final = (
    "people",
    "teams",
    "repos",
    "channels",
    "sources",
    "decisions",
    "twists",
    "canaries",
    "questions",
)
GHOST: Final = "zz-ghost"

Raw = dict[str, Any]
Parts = tuple[str | int, ...]


@dataclass(frozen=True)
class Base:
    name: str
    raw: Raw


@dataclass(frozen=True)
class Defect:
    kind: str
    rule: str
    raw: Raw
    #: Pointers at which the rule may report the defect (at least one must appear).
    paths: frozenset[str]


def _load_bases() -> tuple[Base, Base]:
    mini = load_scenario_text(MINI.read_text(encoding="utf-8"), source=str(MINI))
    kestrel = load_scenario(KESTREL)
    assert isinstance(mini, dict)
    assert isinstance(kestrel, dict)
    return Base("mini", mini), Base("kestrel", kestrel)


MINI_BASE, KESTREL_BASE = _load_bases()


def pointer_of(parts: Parts) -> str:
    return "".join(f"/{part}" for part in parts)


def fresh(base: Base) -> Raw:
    return copy.deepcopy(base.raw)


def at(raw: Raw, parts: Parts) -> Any:
    node: Any = raw
    for part in parts:
        node = node[part]
    return node


def put(raw: Raw, parts: Parts, value: Any) -> None:
    at(raw, parts[:-1])[parts[-1]] = value


def expand(node: Any, template: Sequence[str], prefix: Parts = ()) -> Iterator[Parts]:
    """Concrete paths for a template such as ``("memberships", "*", "team")``; ``*`` stands
    for every list index. Paths that do not exist in ``node`` are skipped."""
    if not template:
        yield prefix
        return
    head, rest = template[0], template[1:]
    if head == "*":
        if isinstance(node, list):
            for i, item in enumerate(node):
                yield from expand(item, rest, (*prefix, i))
    elif isinstance(node, dict) and head in node:
        yield from expand(node[head], rest, (*prefix, head))


def day(text: str) -> dt.date:
    return dt.date.fromisoformat(text)


# --------------------------------------------------------------------------------------
# Independent helpers: readers of a scope (Q-2) and the as-of replay (Q-3)
# --------------------------------------------------------------------------------------


def _members(raw: Raw, grants: Sequence[dict[str, Any]], on: str) -> set[str]:
    found: set[str] = set()
    for grant in grants:
        if "person" in grant:
            found.add(grant["person"])
        if "team" in grant:
            found |= {
                m["person"]
                for m in raw["memberships"]
                if m["team"] == grant["team"]
                and m["from"] <= on
                and ("to" not in m or on < m["to"])
            }
    return found


def readers_of(raw: Raw, scope: str, on: str) -> set[str]:
    """The people who can read ``scope`` on the date ``on`` (design §11, written afresh)."""
    kind, _, name = scope.partition(":")
    if kind == "repo":
        repo = next(r for r in raw["repos"] if r["id"] == name)
        if repo["visibility"] == "public":
            return {p["id"] for p in raw["people"]}
        return _members(raw, repo.get("access", []), on)
    if kind == "channel":
        channel = next(c for c in raw["channels"] if c["id"] == name)
        if not channel.get("private", False):
            return {p["id"] for p in raw["people"] if not p.get("external", False)}
        return _members(raw, channel.get("members", []), on)
    meeting = next(s for s in raw["sources"] if s["id"] == name)["location"]["meeting"]
    return set(meeting.get("attendees", [])) | _members(raw, meeting.get("share_with", []), on)


def replay(raw: Raw, decision: Raw, valid: str, recorded: str) -> str:
    """The lifecycle state of ``decision`` as known at ``recorded`` for the day ``valid``."""
    sources = {s["id"]: s for s in raw["sources"]}
    seen: list[tuple[str, int, str]] = []
    for index, event in enumerate(decision["timeline"]):
        if event["event"] not in LIFECYCLE:
            continue
        source = sources[event["source"]]
        valid_time = event.get("effective", event["on"])
        recorded_time = max(event["on"], source.get("available", source["date"]))
        if valid_time <= valid and recorded_time <= recorded:
            seen.append((valid_time, index, event["event"]))
    if not seen:
        return "absent"
    latest = max(seen)[2]
    return "proposed" if latest == "reopened" else latest


def source_scope(source: Raw) -> str:
    location = source["location"]
    if "meeting" in location:
        return f"meeting:{source['id']}"
    if "channel" in location:
        return f"channel:{location['channel']}"
    return f"repo:{location['repo']}"


def text_fields(source: Raw) -> list[Parts]:
    """Paths (inside the source) of the text that may hold a canary."""
    found: list[Parts] = [(name,) for name in ("title", "body") if name in source]
    found += [("messages", j, "text") for j, _ in enumerate(source.get("messages", []))]
    return found


# --------------------------------------------------------------------------------------
# The catalog: one strategy per defect
# --------------------------------------------------------------------------------------


@st.composite
def duplicate_ids(draw: st.DrawFn, base: Base) -> Defect:
    pools: list[tuple[Parts, int]] = [
        ((name,), len(base.raw[name])) for name in ID_COLLECTIONS if len(base.raw[name]) >= 2
    ]
    pools += [
        (("decisions", d, "claims"), len(decision["claims"]))
        for d, decision in enumerate(base.raw["decisions"])
        if len(decision.get("claims", [])) >= 2
    ]
    where, size = draw(st.sampled_from(pools))
    i = draw(st.integers(0, size - 1))
    j = draw(st.integers(0, size - 1).filter(lambda n: n != i))
    raw = fresh(base)
    put(raw, (*where, j, "id"), at(raw, (*where, i, "id")))
    return Defect("duplicate-id", "ID-1", raw, frozenset({pointer_of((*where, max(i, j), "id"))}))


#: (template, replacement): every place that refers to another entity by id.
REFERENCE_SLOTS: Final[tuple[tuple[tuple[str, ...], str], ...]] = (
    (("memberships", "*", "person"), GHOST),
    (("memberships", "*", "team"), GHOST),
    (("repos", "*", "access", "*", "team"), GHOST),
    (("repos", "*", "access", "*", "person"), GHOST),
    (("channels", "*", "members", "*", "team"), GHOST),
    (("channels", "*", "members", "*", "person"), GHOST),
    (("sources", "*", "location", "repo"), GHOST),
    (("sources", "*", "location", "channel"), GHOST),
    (("sources", "*", "location", "meeting", "attendees", "*"), GHOST),
    (("sources", "*", "author"), GHOST),
    (("sources", "*", "messages", "*", "author"), GHOST),
    (("decisions", "*", "decision_makers", "*"), GHOST),
    (("decisions", "*", "approvers", "*"), GHOST),
    (("decisions", "*", "anchors", "repos", "*"), GHOST),
    (("decisions", "*", "anchors", "prs", "*"), "zz-ghost#999"),
    (("decisions", "*", "timeline", "*", "source"), GHOST),
    (("decisions", "*", "claims", "*", "support_sets", "*", "*"), GHOST),
    (("decisions", "*", "conflicts", "*", "sides", "*", "source"), GHOST),
    (("decisions", "*", "supersedes", "*"), "ADR-999"),
    (("decisions", "*", "superseded_by", "*"), "ADR-999"),
    (("twists", "*", "decisions", "*"), "ADR-999"),
    (("twists", "*", "people", "*"), GHOST),
    (("twists", "*", "sources", "*"), GHOST),
    (("canaries", "*", "allowed_scopes", "*"), "channel:zz-ghost"),
    (("questions", "*", "twists", "*"), "TW-GHOST"),
    (("questions", "*", "expected", "decisions", "*"), "ADR-999"),
    (("questions", "*", "expected", "evidence", "*"), GHOST),
    (("questions", "*", "expected", "must_not_contain", "*"), "CAN-HR-99"),
    (("questions", "*", "expected", "must_not_cite", "*"), GHOST),
)


@st.composite
def dangling_references(draw: st.DrawFn, base: Base) -> Defect:
    slots = [
        (path, replacement)
        for template, replacement in REFERENCE_SLOTS
        for path in expand(base.raw, template)
    ]
    path, replacement = draw(st.sampled_from(slots))
    raw = fresh(base)
    put(raw, path, replacement)
    return Defect("dangling-ref", "REF-1", raw, frozenset({pointer_of(path)}))


@st.composite
def membership_defects(draw: st.DrawFn, base: Base) -> Defect:
    memberships = base.raw["memberships"]
    i = draw(st.integers(0, len(memberships) - 1))
    member = memberships[i]
    raw = fresh(base)
    if draw(st.booleans()):  # reversed (or empty) interval
        back = draw(st.integers(0, 400))
        put(
            raw,
            ("memberships", i, "to"),
            (day(member["from"]) - dt.timedelta(days=back)).isoformat(),
        )
        return Defect("membership", "MEM-1", raw, frozenset({f"/memberships/{i}"}))
    # an interval of the same person and team that starts inside the existing one
    end = day(member["to"]) if "to" in member else day(base.raw["meta"]["reference_date"])
    span = (end - day(member["from"])).days
    offset = draw(st.integers(0, max(span - 1, 0)))
    extra: Raw = {
        "person": member["person"],
        "team": member["team"],
        "from": (day(member["from"]) + dt.timedelta(days=offset)).isoformat(),
    }
    raw["memberships"].append(extra)
    return Defect(
        "membership", "MEM-1", raw, frozenset({f"/memberships/{len(raw['memberships']) - 1}"})
    )


@st.composite
def reordered_timelines(draw: st.DrawFn, base: Base) -> Defect:
    swaps = [
        (d, a, b)
        for d, decision in enumerate(base.raw["decisions"])
        for a, first in enumerate(decision["timeline"])
        for b, second in enumerate(decision["timeline"])
        if a < b and first["on"] < second["on"]
    ]
    d, a, b = draw(st.sampled_from(swaps))
    raw = fresh(base)
    timeline = raw["decisions"][d]["timeline"]
    timeline[a], timeline[b] = timeline[b], timeline[a]
    # the event moved to slot a is later than everything up to slot b-1 that is earlier
    # than it, so the first out-of-order position lies in (a, b]
    return Defect(
        "reordered-timeline",
        "TL-1",
        raw,
        frozenset(f"/decisions/{d}/timeline/{k}" for k in range(a + 1, b + 1)),
    )


@st.composite
def unexplained_valid_times(draw: st.DrawFn, base: Base) -> Defect:
    def twisted(decision_id: str, kind: str) -> bool:
        return any(
            t["kind"] == kind and decision_id in t.get("decisions", []) for t in base.raw["twists"]
        )

    spots = [
        (d, j, sign)
        for d, decision in enumerate(base.raw["decisions"])
        for j, event in enumerate(decision["timeline"])
        if "effective" not in event
        for sign, kind in ((-1, "backdated_valid_time"), (1, "scheduled_change"))
        if not twisted(decision["id"], kind)
    ]
    d, j, sign = draw(st.sampled_from(spots))
    shift = draw(st.integers(1, 120))
    raw = fresh(base)
    event = raw["decisions"][d]["timeline"][j]
    event["effective"] = (day(event["on"]) + dt.timedelta(days=sign * shift)).isoformat()
    return Defect("unexplained-valid", "TL-4", raw, frozenset({f"/decisions/{d}/timeline/{j}"}))


@st.composite
def orphan_twists(draw: st.DrawFn, base: Base) -> Defect:
    t = draw(st.integers(0, len(base.raw["twists"]) - 1))
    twist_id = base.raw["twists"][t]["id"]
    raw = fresh(base)
    raw["questions"] = [q for q in raw["questions"] if twist_id not in q.get("twists", [])]
    return Defect("orphan-twist", "TW-1", raw, frozenset({f"/twists/{t}"}))


def _unplace(raw: Raw, canary: Raw) -> None:
    if canary["kind"] == "token":
        pattern = re.compile(re.escape("{{canary:" + canary["id"] + "}}"))
    else:
        pattern = re.compile(re.escape(canary["phrase"]), re.IGNORECASE)
    for source in raw["sources"]:
        for field in text_fields(source):
            put(source, field, pattern.sub("[redacted]", at(source, field)))


@st.composite
def unplaced_canaries(draw: st.DrawFn, base: Base) -> Defect:
    c = draw(st.integers(0, len(base.raw["canaries"]) - 1))
    raw = fresh(base)
    _unplace(raw, raw["canaries"][c])
    return Defect("unplaced-canary", "CAN-1", raw, frozenset({f"/canaries/{c}"}))


@st.composite
def misplaced_canaries(draw: st.DrawFn, base: Base) -> Defect:
    spots = [
        (c, s, field)
        for c, canary in enumerate(base.raw["canaries"])
        for s, source in enumerate(base.raw["sources"])
        if source_scope(source) not in canary["allowed_scopes"]
        for field in text_fields(source)
    ]
    c, s, field = draw(st.sampled_from(spots))
    canary = base.raw["canaries"][c]
    marker = "{{canary:" + canary["id"] + "}}" if canary["kind"] == "token" else canary["phrase"]
    raw = fresh(base)
    put(raw["sources"][s], field, at(raw["sources"][s], field) + f" {marker}")
    return Defect("misplaced-canary", "CAN-2", raw, frozenset({pointer_of(("sources", s, *field))}))


@st.composite
def reader_askers(draw: st.DrawFn, base: Base) -> Defect:
    reference = base.raw["meta"]["reference_date"]
    canaries = {c["id"]: c for c in base.raw["canaries"]}
    spots = []
    for q, question in enumerate(base.raw["questions"]):
        for j, canary_id in enumerate(question.get("expected", {}).get("must_not_contain", [])):
            readers: set[str] = set()
            for scope in canaries[canary_id]["allowed_scopes"]:
                readers |= readers_of(base.raw, scope, reference)
            # an asker is a person id without hyphens, or ``agent:<person id>``
            spots += [(q, j, person) for person in sorted(readers) if "-" not in person]
    q, j, person = draw(st.sampled_from(spots))
    raw = fresh(base)
    raw["questions"][q]["asker"] = ("agent:" if draw(st.booleans()) else "") + person
    return Defect(
        "reader-asker",
        "Q-2",
        raw,
        frozenset({f"/questions/{q}/expected/must_not_contain/{j}"}),
    )


@st.composite
def shifted_events(draw: st.DrawFn, base: Base) -> Defect:
    spots = []
    for q, question in enumerate(base.raw["questions"]):
        as_of = question.get("as_of")
        if as_of is None:
            continue
        for decision_id, expected in question["expected"].get("lifecycle", {}).items():
            d = next(i for i, x in enumerate(base.raw["decisions"]) if x["id"] == decision_id)
            decision = base.raw["decisions"][d]
            assert replay(base.raw, decision, as_of["valid"], as_of["recorded"]) == expected
            for e, event in enumerate(decision["timeline"]):
                if event["event"] not in LIFECYCLE:
                    continue
                # Moving the event's ``on`` past the recorded date hides it from the question
                # whatever the size of the move; keep the moves that change the answer.
                moved = copy.deepcopy(decision)
                moved["timeline"][e]["on"] = (
                    day(as_of["recorded"]) + dt.timedelta(days=1)
                ).isoformat()
                visible = replay(base.raw, decision, as_of["valid"], as_of["recorded"])
                hidden = replay(base.raw, moved, as_of["valid"], as_of["recorded"])
                if hidden != visible:
                    spots.append((q, decision_id, d, e))
    q, decision_id, d, e = draw(st.sampled_from(spots))
    recorded = base.raw["questions"][q]["as_of"]["recorded"]
    raw = fresh(base)
    raw["decisions"][d]["timeline"][e]["on"] = (
        day(recorded) + dt.timedelta(days=draw(st.integers(1, 400)))
    ).isoformat()
    return Defect(
        "shifted-event",
        "Q-3",
        raw,
        frozenset({f"/questions/{q}/expected/lifecycle/{decision_id}"}),
    )


CATALOG: Final[dict[str, tuple[str, Callable[[Base], st.SearchStrategy[Defect]]]]] = {
    "duplicate-id": ("ID-1", duplicate_ids),
    "dangling-ref": ("REF-1", dangling_references),
    "membership": ("MEM-1", membership_defects),
    "reordered-timeline": ("TL-1", reordered_timelines),
    "unexplained-valid": ("TL-4", unexplained_valid_times),
    "orphan-twist": ("TW-1", orphan_twists),
    "unplaced-canary": ("CAN-1", unplaced_canaries),
    "misplaced-canary": ("CAN-2", misplaced_canaries),
    "reader-asker": ("Q-2", reader_askers),
    "shifted-event": ("Q-3", shifted_events),
}


def any_defect(base: Base) -> st.SearchStrategy[Defect]:
    return st.one_of(*(make(base) for _, make in CATALOG.values()))


# --------------------------------------------------------------------------------------
# The property
# --------------------------------------------------------------------------------------


def assert_reported(base: Base, defect: Defect) -> None:
    assert defect.raw != base.raw, f"{defect.kind} left the scenario unchanged"
    hits = [v for v in validate(defect.raw) if v.rule == defect.rule]
    assert hits, f"{defect.kind}: {defect.rule} not reported"
    assert {v.path for v in hits} & defect.paths, (
        f"{defect.kind}: {defect.rule} reported at {sorted(v.path for v in hits)}, "
        f"expected one of {sorted(defect.paths)}"
    )


@pytest.mark.parametrize("kind", CATALOG)
@settings(deadline=None)
@given(data=st.data())
def test_mini_scenario_defect_is_reported_by_its_rule(kind: str, data: st.DataObject) -> None:
    _, make = CATALOG[kind]
    assert_reported(MINI_BASE, data.draw(make(MINI_BASE)))


@pytest.mark.parametrize("kind", CATALOG)
@settings(deadline=None, max_examples=25)
@given(data=st.data())
def test_kestrel_pay_defect_is_reported_by_its_rule(kind: str, data: st.DataObject) -> None:
    _, make = CATALOG[kind]
    assert_reported(KESTREL_BASE, data.draw(make(KESTREL_BASE)))


@settings(deadline=None)
@given(defect=any_defect(MINI_BASE))
def test_any_defect_from_the_catalog_is_reported_by_its_rule(defect: Defect) -> None:
    assert_reported(MINI_BASE, defect)


# --------------------------------------------------------------------------------------
# Guards on the test itself
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("base", [MINI_BASE, KESTREL_BASE], ids=lambda b: b.name)
def test_the_base_scenarios_are_valid(base: Base) -> None:
    assert validate(base.raw) == []


def test_the_catalog_covers_exactly_the_rules_named_by_the_design() -> None:
    assert {rule for rule, _ in CATALOG.values()} == CATALOG_RULES


def test_defects_and_validation_leave_the_base_scenario_untouched() -> None:
    before = copy.deepcopy(MINI_BASE.raw)

    @settings(max_examples=30, deadline=None)
    @given(defect=any_defect(MINI_BASE))
    def run(defect: Defect) -> None:
        assert defect.raw is not MINI_BASE.raw
        validate(defect.raw)

    run()
    assert MINI_BASE.raw == before
