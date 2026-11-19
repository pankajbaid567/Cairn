"""Property test for scripts/check_docs.py (design Property 18, Requirements 18.3 and 19.4).

Feature: cairn-phase-0-foundations, Property 18 (full tag in the comment below the docstring).

Each example starts from a complete valid sample document (all checks pass), removes
a random subset of the required elements, and expects exactly one finding per removed
element: the right code, a message that names the element, and nothing for the
elements that stay. Decoys (prose mentions of ids, near-miss ids, fenced copies of
headings and tables) are added to every example, so a checker that counted them would
fail. Link examples mix links that resolve, external links, anchors, code spans and
fenced code (which must stay silent) with links to missing files (one finding each,
on the right line).

Hypothesis does not reset function-scoped fixtures between examples, so these tests
use no fixtures: examples that need real files create a fresh temporary directory in
the test body. Filesystem examples set ``deadline=None``; ``max_examples`` comes from
the profiles in the root ``conftest.py``.
"""

# Feature: cairn-phase-0-foundations, Property 18: The docs checker reports every removed element and broken link  # noqa: E501

from __future__ import annotations

import tempfile
from collections.abc import Sequence
from pathlib import Path

from hypothesis import given, settings
from hypothesis import strategies as st

from scripts import check_docs
from scripts.check_docs import Finding

Expected = list[tuple[str, str]]  # (finding code, text the message must contain)

LOWER = "abcdefghijklmnopqrstuvwxyz0123456789"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def assert_exactly(found: Sequence[Finding], expected: Expected) -> None:
    """One finding per expected element, and no other findings."""
    assert len(found) == len(expected), (found, expected)
    for code, needle in expected:
        hits = [f for f in found if f.code == code and needle in f.message]
        assert len(hits) == 1, (code, needle, found)


def subsets(items: Sequence[str]) -> st.SearchStrategy[frozenset[str]]:
    return st.frozensets(st.sampled_from(items))


def write(root: Path, relative: str, text: str) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# ADR
# ---------------------------------------------------------------------------

ADR_KEYS = list(check_docs.ADR_REQUIRED_KEYS)
ADR_SECTIONS = list(check_docs.ADR_REQUIRED_SECTIONS)
# ADR number -> (finding label, phrase that satisfies it)
ADR_PHRASES: dict[int, tuple[tuple[str, str], ...]] = {
    2: (
        ("cairn_dbos", "cairn_dbos"),
        ("same-transaction enqueue question", "same-transaction"),
        ("enqueue", "enqueue"),
    ),
    4: (
        ("cairn_owner", "cairn_owner"),
        ("cairn_app", "cairn_app"),
        ("foreign-key rule", "foreign keys"),
    ),
    13: (
        ("websearch_to_tsquery", "websearch_to_tsquery"),
        ("ts_rank_cd", "ts_rank_cd"),
        ("BYPASSRLS", "BYPASSRLS"),
        ("neondb_owner", "neondb_owner"),
        ("0001_baseline", "0001_baseline"),
    ),
    14: (
        ("cairn_dbos", "cairn_dbos"),
        ("outbox", "outbox"),
        ("the 512 MB limit", "512"),
        ("freetier", "freetier"),
        ("healthz", "healthz"),
    ),
    15: (
        ("neuron", "neuron"),
        ("different model family", "different model family"),
        ("bge-small-en-v1.5", "bge-small-en-v1.5"),
    ),
}


def adr_doc(
    number: int,
    drop_keys: frozenset[str],
    drop_sections: frozenset[str],
    drop_labels: frozenset[str],
) -> str:
    status = check_docs.expected_adr_status(number)
    front = {"status": status, "date": "2026-10-27", "decision-makers": "Pankaj"}
    lines = ["---", *(f"{k}: {v}" for k, v in front.items() if k not in drop_keys), "---", ""]
    lines += ["# Sample ADR", ""]
    for section in ADR_SECTIONS:
        if section not in drop_sections:
            lines += [f"## {section}", "", "Text.", ""]
    kept = [phrase for label, phrase in ADR_PHRASES.get(number, ()) if label not in drop_labels]
    if kept:
        lines += ["Notes: " + "; ".join(kept) + ".", ""]
    # Decoy: a fenced copy of every dropped section heading must not count.
    if drop_sections:
        lines += ["```md", *(f"## {s}" for s in sorted(drop_sections)), "```", ""]
    return "\n".join(lines)


@given(
    number=st.sampled_from(check_docs.ADR_NUMBERS),
    drop_keys=subsets(ADR_KEYS),
    drop_sections=subsets(ADR_SECTIONS),
    data=st.data(),
)
def test_adr_reports_exactly_the_removed_elements(
    number: int, drop_keys: frozenset[str], drop_sections: frozenset[str], data: st.DataObject
) -> None:
    labels = [label for label, _ in ADR_PHRASES.get(number, ())]
    drop_labels = data.draw(subsets(labels)) if labels else frozenset()
    text = adr_doc(number, drop_keys, drop_sections, drop_labels)
    found = check_docs.check_adr(text, "docs/adr/ADR-000x.md", number)
    expected: Expected = (
        [("ADR-KEY", f"'{key}'") for key in sorted(drop_keys)]
        + [("ADR-SECTION", f"'{section}'") for section in sorted(drop_sections)]
        + [("ADR-KEYWORD", f"mention {label}") for label in sorted(drop_labels)]
    )
    assert_exactly(found, expected)
    assert found == check_docs.check_adr(text, "docs/adr/ADR-000x.md", number)


# ---------------------------------------------------------------------------
# Threat model and acceptance list
# ---------------------------------------------------------------------------


def threat_doc(drop: frozenset[str]) -> str:
    table = ["| Threat | Test id | Planned test | Phase |", "|---|---|---|---|"]
    for n, ident in enumerate(check_docs.THREAT_IDS, start=1):
        if ident not in drop:
            table.append(f"| Threat {n} | **{ident}** | test_threat_{n} | {n % 7} |")
    # Decoys: partial-cell mentions in a table, prose before and after, a fenced full table.
    notes = ["| Note | see " + ident + " |" for ident in sorted(drop)]
    prose = [f"See {ident} and TM-99, TM-1." for ident in check_docs.THREAT_IDS]
    full = ["| Threat | Test id | Planned test | Phase |", "|---|---|---|---|"] + [
        f"| T | {ident} | test_t | 1 |" for ident in check_docs.THREAT_IDS
    ]
    parts = ["# Threat model", "", *prose[:7], "", *table, "", *notes, "", *prose[7:], ""]
    parts += ["```md", *full, "```", ""]
    return "\n".join(parts)


@given(drop=subsets(list(check_docs.THREAT_IDS)))
def test_threat_model_reports_exactly_the_removed_ids(drop: frozenset[str]) -> None:
    text = threat_doc(drop)
    found = check_docs.check_threat_model(text)
    assert_exactly(found, [("TM-ID-MISSING", f"{ident}:") for ident in sorted(drop)])
    assert found == check_docs.check_threat_model(text)


def acceptance_doc(drop: frozenset[str]) -> str:
    table = ["| Id | Statement | Tests | Phase |", "|---|---|---|---|"]
    for ident in check_docs.ACCEPTANCE_IDS:
        if ident not in drop:
            test = "test_" + ident.lower().replace("-", "_")
            table.append(f"| {ident} | Statement | `{test}` | 1 |")
    notes = ["| Note | covers " + ident + " | test_note |" for ident in sorted(drop)]
    prose = [f"{ident} is described elsewhere; AT-US10 too." for ident in check_docs.ACCEPTANCE_IDS]
    full = ["| Id | Statement | Tests | Phase |", "|---|---|---|---|"] + [
        f"| {ident} | S | test_s | 1 |" for ident in check_docs.ACCEPTANCE_IDS
    ]
    parts = ["# Acceptance tests", "", *prose[:7], "", *table, "", *notes, "", *prose[7:], ""]
    parts += ["~~~md", *full, "~~~", ""]
    return "\n".join(parts)


@given(drop=subsets(list(check_docs.ACCEPTANCE_IDS)))
def test_acceptance_reports_exactly_the_removed_ids(drop: frozenset[str]) -> None:
    text = acceptance_doc(drop)
    found = check_docs.check_acceptance(text)
    assert_exactly(found, [("AT-ID-MISSING", f"{ident}:") for ident in sorted(drop)])
    assert found == check_docs.check_acceptance(text)


# ---------------------------------------------------------------------------
# PRD and CONTRIBUTING
# ---------------------------------------------------------------------------

# (finding label, heading text that satisfies it)
PRD_SECTIONS: tuple[tuple[str, str], ...] = (
    ("Problem", "Problem"),
    ("Personas", "Personas and key moments"),
    ("Jobs", "Jobs to be done"),
    ("User stories", "User stories"),
    ("MVP scope", "MVP scope"),
    ("Non-goals", "Non-goals"),
    ("Journeys", "Journeys and screens"),
    ("Metrics", "Metrics"),
    ("MVP exit criteria", "MVP exit criteria"),
)
PRD_LABELS = [label for label, _ in PRD_SECTIONS]
CONTRIB_SECTIONS: tuple[tuple[str, str], ...] = (
    ("Setup or quick start", "Quick start"),
    ("Definition of done", "Definition of done"),
    ("Quality gates", "Quality gates"),
    ("AI-assisted workflow", "AI-assisted workflow"),
    ("ADR process", "ADR process"),
    ("Data rules", "Data rules"),
    ("User-owned steps", "User-owned steps"),
)
CONTRIB_LABELS = [label for label, _ in CONTRIB_SECTIONS]


def prd_doc(drop_headings: frozenset[str], drop_ids: frozenset[str]) -> str:
    kept_ids = [i for i in check_docs.PRD_IDS if i not in drop_ids]
    # Mentions live outside the headings' sections, so removing a heading never removes an id.
    # Decoys: "US70" never satisfies US7; a fenced copy of everything counts for nothing.
    decoys = [f"{ident}0" for ident in sorted(drop_ids)]
    parts = ["# PRD", "", "Stories and criteria: " + " ".join([*kept_ids, *decoys]) + ".", ""]
    for label, heading in PRD_SECTIONS:
        if label not in drop_headings:
            parts += [f"## {heading}", "", "Text.", ""]
    fenced = [f"## {heading}" for _, heading in PRD_SECTIONS] + list(check_docs.PRD_IDS)
    parts += ["```md", *fenced, "```", ""]
    return "\n".join(parts)


@given(drop_headings=subsets(PRD_LABELS), drop_ids=subsets(list(check_docs.PRD_IDS)))
def test_prd_reports_exactly_the_removed_headings_and_ids(
    drop_headings: frozenset[str], drop_ids: frozenset[str]
) -> None:
    text = prd_doc(drop_headings, drop_ids)
    found = check_docs.check_prd(text)
    expected: Expected = [("PRD-HEADING", f"'{label}'") for label in sorted(drop_headings)] + [
        ("PRD-ID", f"{ident} is not mentioned") for ident in sorted(drop_ids)
    ]
    assert_exactly(found, expected)
    assert found == check_docs.check_prd(text)


def contributing_doc(drop: frozenset[str]) -> str:
    parts = ["# Contributing", ""]
    for label, heading in CONTRIB_SECTIONS:
        if label not in drop:
            parts += [f"## {heading}", "", "Text.", ""]
    dropped = [heading for label, heading in CONTRIB_SECTIONS if label in drop]
    if dropped:
        parts += ["```md", *(f"## {heading}" for heading in dropped), "```", ""]
    return "\n".join(parts)


@given(drop=subsets(CONTRIB_LABELS))
def test_contributing_reports_exactly_the_removed_headings(drop: frozenset[str]) -> None:
    text = contributing_doc(drop)
    found = check_docs.check_contributing(text)
    assert_exactly(found, [("CONTRIB-SECTION", f"'{label}'") for label in sorted(drop)])
    assert found == check_docs.check_contributing(text)


def test_complete_documents_have_no_findings() -> None:
    none: frozenset[str] = frozenset()
    assert check_docs.check_threat_model(threat_doc(none)) == []
    assert check_docs.check_acceptance(acceptance_doc(none)) == []
    assert check_docs.check_prd(prd_doc(none, none)) == []
    assert check_docs.check_contributing(contributing_doc(none)) == []
    for number in check_docs.ADR_NUMBERS:
        assert check_docs.check_adr(adr_doc(number, none, none, none), "a.md", number) == []


# ---------------------------------------------------------------------------
# Links
# ---------------------------------------------------------------------------

# Files created next to docs/a.md; "with space.md" is linked as "with%20space.md".
EXISTING_FILES = ("docs/exists.md", "docs/sub/other.md", "docs/with space.md", "README.md")
RESOLVING_TARGETS = ("exists.md", "sub/other.md", "with%20space.md", "../README.md", "./exists.md")

words = st.text(alphabet=LOWER, min_size=1, max_size=8)
labels = st.text(alphabet="abc XYZ", max_size=6)
suffixes = st.sampled_from(["", "#sec", "#a-b", "?x=1"])
titles = st.sampled_from(["", ' "title"', " 'title'"])

# A line item is (text, raw target if the link is broken else None).
Item = tuple[str, str | None]


@st.composite
def silent_items(draw: st.DrawFn) -> Item:
    """Link-like text that must produce no finding."""
    kind = draw(st.sampled_from(["file", "image", "external", "anchor", "mailto", "span", "angle"]))
    label, word, suffix = draw(labels), draw(words), draw(suffixes)
    if kind == "file":
        return f"[{label}]({draw(st.sampled_from(RESOLVING_TARGETS))}{suffix}{draw(titles)})", None
    if kind == "image":
        return f"![{label}]({draw(st.sampled_from(RESOLVING_TARGETS))})", None
    if kind == "external":
        scheme = draw(
            st.sampled_from(["https://x.example/", "http://y.example/", "//cdn.example/"])
        )
        return f"[{label}]({scheme}{word}{suffix})", None
    if kind == "anchor":
        return f"[{label}](#{word})", None
    if kind == "mailto":
        return f"[{label}](mailto:{word}@x.example)", None
    if kind == "angle":
        return f"[{label}](<with space.md>)", None
    return f"`[{label}](nope_{word}.md)`", None  # inside a code span


@st.composite
def broken_items(draw: st.DrawFn) -> Item:
    """An inline link or image to a file that does not exist."""
    folder = draw(st.sampled_from(["", "sub/", "../", "../sub/"]))
    raw = f"{folder}nope_{draw(words)}.md{draw(suffixes)}"
    label = draw(labels)
    bang = draw(st.sampled_from(["", "!"]))
    return f"{bang}[{label}]({raw}{draw(titles)})", raw


@st.composite
def line_units(draw: st.DrawFn) -> tuple[list[str], list[tuple[int, str]]]:
    """One or more lines plus the (offset, raw target) of every broken link in them."""
    kind = draw(st.sampled_from(["items", "items", "reference", "fence", "prose"]))
    if kind == "prose":
        return [draw(st.sampled_from(["", "Plain text line.", "## Heading", "| a | b |"]))], []
    if kind == "reference":
        raw = f"nope_{draw(words)}.md{draw(suffixes)}"
        return [f"[ref-{draw(words)}]: {raw}"], [(0, raw)]
    if kind == "fence":
        marks = draw(st.sampled_from(["```", "~~~", "````"]))
        inner = f"[x](nope_{draw(words)}.md)"
        return [f"{marks}md", inner, f"[r]: nope_{draw(words)}.md", marks], []
    items = draw(st.lists(st.one_of(silent_items(), broken_items()), min_size=1, max_size=3))
    line = " and ".join(text for text, _ in items)
    return [line], [(0, raw) for _, raw in items if raw is not None]


@st.composite
def link_docs(draw: st.DrawFn) -> tuple[str, list[tuple[int, str]]]:
    """Markdown text and the sorted (line number, raw target) pairs of its broken links."""
    units = draw(st.lists(line_units(), max_size=12))
    lines: list[str] = []
    broken: list[tuple[int, str]] = []
    for unit_lines, offsets in units:
        broken += [(len(lines) + 1 + offset, raw) for offset, raw in offsets]
        lines += unit_lines
    return "\n".join(lines) + "\n", sorted(broken)


def make_link_root(root: Path, text: str) -> Path:
    for relative in EXISTING_FILES:
        write(root, relative, "x\n")
    return write(root, "docs/a.md", text)


@settings(deadline=None)
@given(doc=link_docs())
def test_links_report_exactly_the_broken_links_with_line_numbers(
    doc: tuple[str, list[tuple[int, str]]],
) -> None:
    text, broken = doc
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        source = make_link_root(root, text)
        found = check_docs.check_links(text, source, root)
        assert found == check_docs.check_links(text, source, root)
        assert {f.code for f in found} <= {"LINK-BROKEN"}
        assert {f.path for f in found} <= {"docs/a.md"}
        assert sorted((f.line, raw) for f in found for raw in [_raw(f)]) == broken
        # The file layer sees the same problems, sorted. Two identical links on one line give
        # two equal findings here; `run` is what removes duplicates.
        assert check_docs.run_group("links", root) == sorted(found)
        assert check_docs.run(["links"], root) == sorted(set(found))


def _raw(finding: Finding) -> str:
    """The raw link target quoted in a LINK-BROKEN message."""
    prefix, suffix = "link '", "' does not resolve to a file"
    assert finding.message.startswith(prefix)
    assert finding.message.endswith(suffix)
    return finding.message[len(prefix) : -len(suffix)]


# ---------------------------------------------------------------------------
# Whole run: sorted, deterministic, duplicate-free, one finding per defect
# ---------------------------------------------------------------------------


@settings(deadline=None)
@given(
    drop_tm=subsets(list(check_docs.THREAT_IDS)),
    drop_at=subsets(list(check_docs.ACCEPTANCE_IDS)),
    drop_prd_headings=subsets(PRD_LABELS),
    drop_prd_ids=subsets(list(check_docs.PRD_IDS)),
    drop_contrib=subsets(CONTRIB_LABELS),
    doc=link_docs(),
)
def test_run_is_sorted_deterministic_and_counts_every_defect(
    drop_tm: frozenset[str],
    drop_at: frozenset[str],
    drop_prd_headings: frozenset[str],
    drop_prd_ids: frozenset[str],
    drop_contrib: frozenset[str],
    doc: tuple[str, list[tuple[int, str]]],
) -> None:
    link_text, broken = doc
    groups = ["prd", "threat-model", "acceptance", "contributing", "links"]
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        make_link_root(root, link_text)
        write(root, "docs/threat-model.md", threat_doc(drop_tm))
        write(root, "docs/acceptance-tests.md", acceptance_doc(drop_at))
        write(root, "docs/prd.md", prd_doc(drop_prd_headings, drop_prd_ids))
        write(root, "CONTRIBUTING.md", contributing_doc(drop_contrib))
        first = check_docs.run(groups, root)
        second = check_docs.run(list(reversed(groups)) + groups, root)
    assert first == second
    assert first == sorted(first)
    assert len(first) == len(set(first))
    removed = len(drop_tm) + len(drop_at) + len(drop_prd_headings) + len(drop_prd_ids)
    # Identical links on one line are equal findings, which `run` reports once.
    distinct_broken = len(set(broken))
    assert len(first) == removed + len(drop_contrib) + distinct_broken
    by_code: dict[str, int] = {}
    for finding in first:
        by_code[finding.code] = by_code.get(finding.code, 0) + 1
    assert by_code.get("TM-ID-MISSING", 0) == len(drop_tm)
    assert by_code.get("AT-ID-MISSING", 0) == len(drop_at)
    assert by_code.get("PRD-HEADING", 0) == len(drop_prd_headings)
    assert by_code.get("PRD-ID", 0) == len(drop_prd_ids)
    assert by_code.get("CONTRIB-SECTION", 0) == len(drop_contrib)
    assert by_code.get("LINK-BROKEN", 0) == distinct_broken
