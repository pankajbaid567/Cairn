"""Property test for validator determinism and neutral rewrites (task 12.9).

Feature: cairn-phase-0-foundations, Property 12: Validation is deterministic and unchanged
by neutral rewrites

**Validates: Requirements 13.2, 13.24**

Two bases are used: ``data/mini_scenario.yaml`` and the real Kestrel Pay scenario. Both
pass every rule.

* Determinism: random structural mutations (delete, replace, graft a string from elsewhere
  in the file, swap, duplicate) are applied to a base, and arbitrary JSON values are fed in
  as well. Validating the same value twice must give equal lists that are sorted, free of
  duplicates and made of ``Violation`` records, and the input must not be modified.
* Neutral rewrites of the valid file: consistently renaming ids (definitions and every
  reference, including ``{{canary:ID}}`` placeholders, ``agent:`` askers, scopes and
  ``repo#N`` PR refs) and reordering the top-level collections, the top-level keys and the
  keys inside every mapping must leave zero violations. The persona ids of
  ``access.PERSONA_IDS`` are reserved names that the access rules look for, so they are
  not renamed.
"""

from __future__ import annotations

import copy
import random
import re
from pathlib import Path
from typing import Any, Final

import pytest
from hypothesis import event, given, settings
from hypothesis import strategies as st

from cairn_eval.scenario.access import PERSONA_IDS
from cairn_eval.scenario.loader import load_scenario, load_scenario_text
from cairn_eval.scenario.validator import validate
from cairn_eval.scenario.violation import Violation

HERE: Final = Path(__file__).resolve().parent
REPO_ROOT: Final = HERE.parents[3]
MINI: Final = HERE / "data" / "mini_scenario.yaml"
KESTREL: Final = REPO_ROOT / "scenarios" / "kestrel_pay" / "scenario.yaml"

PLACEHOLDER: Final = re.compile(r"\{\{canary:([^}]+)\}\}")
JsonPath = tuple[str | int, ...]


def _load(path: Path) -> dict[str, Any]:
    raw = load_scenario(path)
    assert isinstance(raw, dict)
    return raw


BASES: Final[dict[str, dict[str, Any]]] = {
    "mini": _load(MINI),
    "kestrel": _load(KESTREL),
}
base_names = st.sampled_from(sorted(BASES))


# --------------------------------------------------------------------------------------
# Structural mutations
# --------------------------------------------------------------------------------------
def _paths(node: Any, prefix: JsonPath = ()) -> list[JsonPath]:
    """Every path below the root, parents before children."""
    found: list[JsonPath] = []
    if isinstance(node, dict):
        for key, value in node.items():
            found.append((*prefix, key))
            found.extend(_paths(value, (*prefix, key)))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            found.append((*prefix, index))
            found.extend(_paths(value, (*prefix, index)))
    return found


def _strings(node: Any) -> list[str]:
    if isinstance(node, str):
        return [node]
    if isinstance(node, dict):
        return [s for value in node.values() for s in _strings(value)]
    if isinstance(node, list):
        return [s for value in node for s in _strings(value)]
    return []


PATHS: Final = {name: _paths(raw) for name, raw in BASES.items()}
STRINGS: Final = {name: sorted(set(_strings(raw))) for name, raw in BASES.items()}

DATE_LIKE = ("2026-02-30", "2026-10-28", "2025-01-01", "2026-03-10T10:00:00Z", "not a date")
json_values = st.recursive(
    st.none()
    | st.booleans()
    | st.integers(-5, 400)
    | st.floats(allow_nan=False, allow_infinity=False)
    | st.text(max_size=12)
    | st.sampled_from(DATE_LIKE),
    lambda inner: (
        st.lists(inner, max_size=3) | st.dictionaries(st.text(max_size=6), inner, max_size=3)
    ),
    max_leaves=6,
)

mutation_kinds = ("delete", "replace", "graft", "swap", "duplicate")
mutations = st.tuples(
    st.sampled_from(mutation_kinds),
    st.integers(0, 10**6),  # which path
    st.integers(0, 10**6),  # which string / element
    json_values,
)


def mutate(base: str, edits: list[tuple[str, int, int, Any]]) -> dict[str, Any]:
    """A copy of ``base`` with the edits applied. An edit whose path no longer exists
    (an earlier edit removed it) is skipped."""
    raw = copy.deepcopy(BASES[base])
    for kind, path_pick, aux, payload in edits:
        path = PATHS[base][path_pick % len(PATHS[base])]
        try:
            parent: Any = raw
            for part in path[:-1]:
                parent = parent[part]
            last = path[-1]
            if kind == "delete":
                del parent[last]
            elif kind == "replace":
                parent[last] = copy.deepcopy(payload)
            elif kind == "graft":
                parent[last] = STRINGS[base][aux % len(STRINGS[base])]
            elif parent[last].__class__ is list and parent[last]:
                items = parent[last]
                if kind == "swap":
                    i, j = aux % len(items), (aux // 7 + 1) % len(items)
                    items[i], items[j] = items[j], items[i]
                else:  # duplicate
                    items.append(copy.deepcopy(items[aux % len(items)]))
        except (KeyError, IndexError, TypeError):
            continue
    return raw


mutated_scenarios = st.builds(mutate, base_names, st.lists(mutations, min_size=1, max_size=4))


# --------------------------------------------------------------------------------------
# Determinism
# --------------------------------------------------------------------------------------
def assert_deterministic(value: Any) -> list[Violation]:
    before = copy.deepcopy(value)
    first = validate(value)
    second = validate(copy.deepcopy(value))
    assert first == second
    assert first == sorted(set(first)), "violations must be sorted and free of duplicates"
    assert all(isinstance(v, Violation) for v in first)
    assert value == before, "validate must not modify its input"
    return first


@given(mutated_scenarios)
@settings(deadline=None)
def test_validating_a_mutated_scenario_twice_gives_the_same_sorted_violations(
    raw: dict[str, Any],
) -> None:
    violations = assert_deterministic(raw)
    event("violations" if violations else "still valid")


@given(json_values)
def test_validating_any_json_value_twice_gives_the_same_sorted_violations(value: Any) -> None:
    assert_deterministic(value)


def test_the_bases_are_valid_and_a_known_defect_is_reported_identically_each_time() -> None:
    for name, raw in BASES.items():
        assert validate(raw) == [], name
        broken = copy.deepcopy(raw)
        broken["decisions"][0]["timeline"][0]["source"] = "no-such-source"
        broken["decisions"].append(copy.deepcopy(broken["decisions"][0]))
        violations = assert_deterministic(broken)
        assert {"ID-1", "REF-1"} <= {v.rule for v in violations}, name


# --------------------------------------------------------------------------------------
# Reordering
# --------------------------------------------------------------------------------------
def reorder(raw: dict[str, Any], rnd: random.Random) -> dict[str, Any]:
    """Shuffle the top-level keys, the elements of every top-level collection and the key
    order of every mapping. Lists inside an entity (timelines, support sets) keep their
    order: a timeline is a sequence, not a collection."""

    def shuffle_keys(node: Any) -> Any:
        if isinstance(node, dict):
            keys = list(node)
            rnd.shuffle(keys)
            return {k: shuffle_keys(node[k]) for k in keys}
        if isinstance(node, list):
            return [shuffle_keys(v) for v in node]
        return node

    out = copy.deepcopy(raw)
    for value in out.values():
        if isinstance(value, list):
            rnd.shuffle(value)
    return shuffle_keys(out)


@given(base_names, st.randoms(use_true_random=False))
@settings(deadline=None)
def test_reordering_the_collections_keeps_the_scenario_valid(base: str, rnd: random.Random) -> None:
    assert validate(reorder(BASES[base], rnd)) == []


# --------------------------------------------------------------------------------------
# Consistent renaming of ids
# --------------------------------------------------------------------------------------
TOP_LEVEL_NAMESPACE: Final = {
    "people": "person",
    "teams": "team",
    "repos": "repo",
    "channels": "channel",
    "sources": "source",
    "decisions": "decision",
    "twists": "twist",
    "canaries": "canary",
    "questions": "question",
}
# Keys whose string value (or, for a list, string elements) refers to an entity.
REFERENCE_NAMESPACE: Final = {
    "person": "person",
    "team": "team",
    "repo": "repo",
    "channel": "channel",
    "author": "person",
    "source": "source",
    "by": "decision",
    "attendees": "person",
    "decision_makers": "person",
    "approvers": "person",
    "people": "person",
    "repos": "repo",
    "sources": "source",
    "decisions": "decision",
    "twists": "twist",
    "supersedes": "decision",
    "superseded_by": "decision",
    "evidence": "source",
    "must_not_cite": "source",
    "must_not_contain": "canary",
}
Renaming = dict[str, dict[str, str]]  # namespace -> old id -> new id


def defined_ids(raw: dict[str, Any]) -> dict[str, list[str]]:
    """The ids the file defines, per namespace, in file order (personas excluded)."""
    ids: dict[str, list[str]] = {
        ns: [] for ns in (*TOP_LEVEL_NAMESPACE.values(), "claim", "conflict")
    }
    for collection, ns in TOP_LEVEL_NAMESPACE.items():
        ids[ns] = [item["id"] for item in raw[collection]]
    ids["person"] = [i for i in ids["person"] if i not in PERSONA_IDS]
    for decision in raw["decisions"]:
        ids["claim"] += [c["id"] for c in decision.get("claims", [])]
        ids["conflict"] += [c["id"] for c in decision.get("conflicts", [])]
    return ids


def apply_renaming(raw: dict[str, Any], ren: Renaming) -> dict[str, Any]:
    """Rename definitions and every reference. Unknown ids (personas) stay as they are."""

    def name(ns: str, old: str) -> str:
        return ren[ns].get(old, old)

    def scope(old: str) -> str:
        kind, _, target = old.partition(":")
        ns = "source" if kind == "meeting" else kind  # meeting scopes name a source
        return f"{kind}:{name(ns, target)}"

    def asker(old: str) -> str:
        prefix = "agent:" if old.startswith("agent:") else ""
        return prefix + name("person", old.removeprefix("agent:"))

    def pr_ref(old: str) -> str:
        repo, _, number = old.partition("#")
        return f"{name('repo', repo)}#{number}"

    def text(old: str) -> str:
        return PLACEHOLDER.sub(lambda m: "{{canary:" + name("canary", m.group(1)) + "}}", old)

    def refs(ns: str, value: Any) -> Any:
        if isinstance(value, str):
            return name(ns, value)
        if isinstance(value, list):
            return [refs(ns, v) if isinstance(v, str) else walk(v) for v in value]
        return walk(value)

    def walk(node: Any) -> Any:
        if isinstance(node, str):
            return text(node)
        if isinstance(node, list):
            return [walk(v) for v in node]
        if not isinstance(node, dict):
            return node
        out: dict[str, Any] = {}
        for key, value in node.items():
            if key in REFERENCE_NAMESPACE and not isinstance(value, dict):
                out[key] = refs(REFERENCE_NAMESPACE[key], value)
            elif key == "support_sets":
                out[key] = [[name("source", s) for s in group] for group in value]
            elif key == "asker":
                out[key] = asker(value)
            elif key == "allowed_scopes":
                out[key] = [scope(s) for s in value]
            elif key == "prs":
                out[key] = [pr_ref(s) for s in value]
            elif key == "lifecycle":
                out[key] = {name("decision", d): state for d, state in value.items()}
            else:
                out[key] = walk(value)
        return out

    renamed: dict[str, Any] = walk(raw)
    for collection, ns in TOP_LEVEL_NAMESPACE.items():
        for item in renamed[collection]:
            item["id"] = name(ns, item["id"])
    for decision in renamed["decisions"]:
        for claim in decision.get("claims", []):
            claim["id"] = name("claim", claim["id"])
        for conflict in decision.get("conflicts", []):
            conflict["id"] = name("conflict", conflict["id"])
    return renamed


LETTERS: Final = "abcdefghijklmnopqrstuvwxyz"
STEMS: Final = st.builds(
    lambda head, tail: head + tail,
    st.sampled_from(LETTERS),
    st.text(alphabet=LETTERS + "0123456789", max_size=8),
)
UPPER_STEMS: Final = st.text(
    alphabet="ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789", min_size=1, max_size=8
)
COPRIME_STEPS: Final = (1, 3, 7, 9, 11, 13, 17, 19, 21, 23, 27, 29, 31, 33, 37, 39, 41, 43, 47)


def _unique_names(
    stem: st.SearchStrategy[str], sep: str, count: int, prefix: str = ""
) -> st.SearchStrategy[list[str]]:
    """Distinct names by construction: ``<prefix><stem><sep><index>`` is parsed back by its
    last separator, so no two of them are equal."""
    return stem.map(lambda s: [f"{prefix}{s}{sep}{i}" for i in range(count)])


def _numbers(count: int, modulus: int) -> st.SearchStrategy[list[int]]:
    """Distinct numbers below ``modulus`` (an arithmetic progression with a coprime step)."""
    return st.tuples(st.integers(0, modulus - 1), st.sampled_from(COPRIME_STEPS)).map(
        lambda parts: [(parts[0] + i * parts[1]) % modulus for i in range(count)]
    )


@st.composite
def renamings(draw: st.DrawFn, base: str) -> Renaming:
    ids = defined_ids(BASES[base])
    families = {c["id"]: c["family"] for c in BASES[base]["canaries"]}
    fam_prefix = {"hr": "HR", "partner": "PARTNER", "security": "SEC", "incident": "INC"}

    def count(ns: str) -> int:
        return len(ids[ns])

    # Each new name keeps the shape its pattern needs: people have no hyphen (ASKER_PATTERN)
    # and never equal a reserved persona id (they end in "_<n>", the reserved ids do not);
    # repos have no underscore (PR_REF_PATTERN); the rest are free slugs.
    new: dict[str, list[str]] = {
        "person": draw(_unique_names(STEMS, "_", count("person"))),
        "team": draw(_unique_names(STEMS, "-", count("team"))),
        "repo": draw(_unique_names(STEMS, "-", count("repo"))),
        "channel": draw(_unique_names(STEMS, "-", count("channel"))),
        "source": draw(_unique_names(STEMS, "-", count("source"))),
        "twist": draw(_unique_names(UPPER_STEMS, "-", count("twist"), prefix="TW-")),
        "decision": [f"ADR-{n:03d}" for n in draw(_numbers(count("decision"), 1000))],
        "question": [f"Q{n:03d}" for n in draw(_numbers(count("question"), 1000))],
        "claim": [f"C{n}" for n in draw(_numbers(count("claim"), 1000))],
        "conflict": [f"K{n}" for n in draw(_numbers(count("conflict"), 1000))],
        "canary": [
            f"CAN-{fam_prefix[families[old]]}-{n:02d}"
            for old, n in zip(ids["canary"], draw(_numbers(count("canary"), 100)), strict=True)
        ],
    }
    assert all(set(new[ns]).isdisjoint(PERSONA_IDS) for ns in new)
    return {ns: dict(zip(ids[ns], new[ns], strict=True)) for ns in new}


@given(st.data())
@settings(deadline=None)
def test_consistently_renaming_ids_keeps_the_scenario_valid(data: st.DataObject) -> None:
    base = data.draw(base_names)
    renamed = apply_renaming(BASES[base], data.draw(renamings(base)))
    assert validate(renamed) == []


@given(st.data())
@settings(deadline=None)
def test_renaming_and_reordering_together_keep_the_scenario_valid(data: st.DataObject) -> None:
    base = data.draw(base_names)
    renamed = apply_renaming(BASES[base], data.draw(renamings(base)))
    assert validate(reorder(renamed, data.draw(st.randoms(use_true_random=False)))) == []


# Guards against a vacuous rename: the helper must change ids, and a rename that skips the
# references (only the definitions) must be caught by the validator.
@pytest.mark.parametrize("base", sorted(BASES))
def test_the_rename_helper_changes_ids_and_a_partial_rename_is_reported(base: str) -> None:
    raw = BASES[base]
    ids = defined_ids(raw)
    ren: Renaming = {ns: {old: f"{old}x" for old in olds} for ns, olds in ids.items()}
    ren["decision"] = {old: "ADR-" + str(900 + i) for i, old in enumerate(ids["decision"])}
    renamed = apply_renaming(raw, ren)
    assert renamed != raw
    assert renamed["decisions"][0]["id"] == ren["decision"][raw["decisions"][0]["id"]]

    partial = copy.deepcopy(raw)
    partial["sources"][0]["id"] = "renamed-without-references"
    assert "REF-1" in {v.rule for v in validate(partial)}


def test_the_mini_text_form_of_a_rename_agrees_with_the_helper() -> None:
    text = MINI.read_text(encoding="utf-8").replace("thread-platform", "thread-roadmap")
    by_text = load_scenario_text(text)
    by_helper = apply_renaming(
        BASES["mini"],
        {"source": {"thread-platform": "thread-roadmap"}}
        | {
            ns: {}
            for ns in (
                "person",
                "team",
                "repo",
                "channel",
                "twist",
                "decision",
                "question",
                "claim",
                "conflict",
                "canary",
            )
        },
    )
    assert by_text == by_helper
