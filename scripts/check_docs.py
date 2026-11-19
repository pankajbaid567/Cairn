"""Check the Phase 0 documents (``make docs-check``).

Design §16 and Requirements 18.3 to 18.5 and 19.4.

Groups (``--only`` picks one or more; the default is all of them):

========== ==========================================================================
group      what it checks
========== ==========================================================================
prd        ``docs/prd.md`` has its required headings and mentions US1 to US9, EC1 to EC5
adr        ADR-0001 to ADR-0005 and ADR-0013 to ADR-0015, the MADR template and the
           index: front matter, the expected status of each ADR (``superseded by
           ADR-0013`` for ADR-0001, ``accepted`` for the rest), sections, and the
           keywords of ADR-0002, ADR-0004, ADR-0013, ADR-0014 and ADR-0015
threat-    ``docs/threat-model.md`` lists TM-01 to TM-16 once each, with a planned
model      test name and a phase
acceptance ``docs/acceptance-tests.md`` lists AT-US1 to AT-US9 and AT-EC1 to AT-EC5,
           each with at least one test id
contribu-  ``CONTRIBUTING.md`` has the setup, definition of done, quality gates, AI
ting       workflow, ADR process, data rules and user-owned steps sections
links      every relative link in ``docs/**/*.md`` and ``CONTRIBUTING.md`` resolves
           to a file
========== ==========================================================================

Every check is a pure function over a string (or a string plus paths for links)
that returns a list of ``Finding``. Only ``run_group`` touches the filesystem, and
a missing file is a ``DOC-MISSING`` finding, never a crash. Fenced code blocks are
ignored by every check, so examples in a document cannot satisfy or break a rule.

Exit status: 0 no findings, 1 findings, 2 bad usage (argparse) or root not a
directory. Standard library only, so it runs before ``uv sync``.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import defaultdict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from urllib.parse import unquote

REPO_ROOT = Path(__file__).resolve().parent.parent

GROUPS: tuple[str, ...] = ("prd", "adr", "threat-model", "acceptance", "contributing", "links")

# ---------------------------------------------------------------------------
# Expected content (design §16)
# ---------------------------------------------------------------------------

# The ADRs this checker owns. ADR-0006 to ADR-0012 are reserved for Phase 1 and the spike
# tracker (see docs/adr/README.md) and are not checked here.
ADR_NUMBERS: tuple[int, ...] = (1, 2, 3, 4, 5, 13, 14, 15)
ADR_ACCEPTED = "accepted"
# ADR number -> expected front matter status; numbers not listed must be ``accepted``.
ADR_EXPECTED_STATUS: dict[int, str] = {1: "superseded by ADR-0013"}
ADR_REQUIRED_KEYS: tuple[str, ...] = ("status", "date", "decision-makers")
ADR_REQUIRED_SECTIONS: tuple[str, ...] = (
    "Context and Problem Statement",
    "Considered Options",
    "Decision Outcome",
    "Consequences",
    "Confirmation",
)
# ADR number -> (label, pattern) keyword checks over the whole ADR text.
ADR_KEYWORDS: dict[int, tuple[tuple[str, str], ...]] = {
    2: (
        ("cairn_dbos", r"cairn_dbos"),
        ("same-transaction enqueue question", r"same[- ]transaction"),
        ("enqueue", r"enqueue"),
    ),
    4: (
        ("cairn_owner", r"cairn_owner"),
        ("cairn_app", r"cairn_app"),
        ("foreign-key rule", r"foreign[- ]keys?"),
    ),
    13: (
        ("websearch_to_tsquery", r"websearch_to_tsquery"),
        ("ts_rank_cd", r"ts_rank_cd"),
        ("BYPASSRLS", r"BYPASSRLS"),
        ("neondb_owner", r"neondb_owner"),
        ("0001_baseline", r"0001_baseline"),
    ),
    14: (
        ("cairn_dbos", r"cairn_dbos"),
        ("outbox", r"outbox"),
        ("the 512 MB limit", r"\b512\b"),
        ("freetier", r"freetier"),
        ("healthz", r"healthz"),
    ),
    15: (
        ("neuron", r"neuron"),
        ("different model family", r"different model famil(?:y|ies)"),
        ("bge-small-en-v1.5", r"bge-small-en-v1\.5"),
    ),
}

THREAT_IDS: tuple[str, ...] = tuple(f"TM-{n:02d}" for n in range(1, 17))
ACCEPTANCE_IDS: tuple[str, ...] = (
    *(f"AT-US{n}" for n in range(1, 10)),
    *(f"AT-EC{n}" for n in range(1, 6)),
)
TM_ID = re.compile(r"TM-\d+")
AT_ID = re.compile(r"AT-(?:US|EC)\d+")
TEST_ID = re.compile(r"\btest_[A-Za-z0-9_]+")
PHASE_CELL = re.compile(r"(?:phase\s*)?[0-6]", re.IGNORECASE)
TM_MIN_CELLS = 4  # threat, test id, planned test name, phase

# (label, heading pattern) pairs. A pattern is searched in the heading text.
PRD_HEADINGS: tuple[tuple[str, str], ...] = (
    ("Problem", r"problem"),
    ("Personas", r"personas?"),
    ("Jobs", r"\bjobs?\b"),
    ("User stories", r"user stories"),
    ("MVP scope", r"scope"),
    ("Non-goals", r"non-?\s?goals?"),
    ("Journeys", r"journeys?"),
    ("Metrics", r"metrics"),
    ("MVP exit criteria", r"exit criteria"),
)
PRD_IDS: tuple[str, ...] = (
    *(f"US{n}" for n in range(1, 10)),
    *(f"EC{n}" for n in range(1, 6)),
)
CONTRIBUTING_HEADINGS: tuple[tuple[str, str], ...] = (
    ("Setup or quick start", r"quick ?start|set-?up|getting started|prerequisites"),
    ("Definition of done", r"definition of done"),
    ("Quality gates", r"quality gates?"),
    ("AI-assisted workflow", r"\bai\b.*(workflow|assisted)|(workflow|assisted).*\bai\b"),
    ("ADR process", r"\badr\b.*process|decision records?"),
    ("Data rules", r"data rules?"),
    ("User-owned steps", r"user-owned steps?"),
)

# ---------------------------------------------------------------------------
# Findings
# ---------------------------------------------------------------------------


@dataclass(frozen=True, order=True)
class Finding:
    """One problem. ``path`` is relative to the repo root; ``line`` 0 means whole file."""

    path: str
    line: int
    code: str
    message: str

    def __str__(self) -> str:
        where = f"{self.path}:{self.line}" if self.line else self.path
        return f"{where}: {self.code} {self.message}"


# ---------------------------------------------------------------------------
# Markdown helpers (pure)
# ---------------------------------------------------------------------------

_FENCE = re.compile(r"^ {0,3}(?P<marks>`{3,}|~{3,})(?P<info>.*)$")
_HEADING = re.compile(r"^ {0,3}#{1,6}\s+(?P<text>.*?)\s*#*\s*$")
_SEPARATOR_CELL = re.compile(r":?-+:?")


def mask_code_fences(text: str) -> list[str]:
    """Lines of ``text`` with fenced code blocks (fences included) blanked out.

    The line count is unchanged, so index + 1 is the original line number.
    """
    out: list[str] = []
    fence: tuple[str, int] | None = None
    for line in text.splitlines():
        match = _FENCE.match(line)
        if fence is None:
            if match:
                fence = (match["marks"][0], len(match["marks"]))
                out.append("")
            else:
                out.append(line)
            continue
        closes = (
            match is not None
            and match["marks"][0] == fence[0]
            and len(match["marks"]) >= fence[1]
            and not match["info"].strip()
        )
        if closes:
            fence = None
        out.append("")
    return out


def normalize(text: str) -> str:
    """Case-folded text without asterisks, code ticks, a trailing colon or extra spaces."""
    cleaned = re.sub(r"[*`]", "", text).strip().rstrip(":").strip()
    return re.sub(r"\s+", " ", cleaned).casefold()


def parse_front_matter(text: str) -> tuple[dict[str, str] | None, int]:
    """Parse a leading ``---`` block of ``key: value`` lines.

    Returns ``(keys, line_count)``: ``keys`` is None when the block is absent or
    unterminated, and ``line_count`` is the number of lines it occupies (0 if absent).
    Indented or ``- item`` continuation lines are appended to the previous key.
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None, 0
    keys: dict[str, str] = {}
    current: str | None = None
    for index, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            return keys, index + 1
        entry = re.match(r"^([A-Za-z][\w-]*):\s*(.*)$", line)
        if entry:
            current = entry[1]
            keys[current] = entry[2].strip()
        elif current and (line[:1].isspace() or line.startswith("- ")) and line.strip():
            keys[current] = f"{keys[current]} {line.strip()}".strip()
    return None, 0


def headings(lines: Sequence[str]) -> list[str]:
    """Normalized heading texts found in already-masked lines."""
    found: list[str] = []
    for line in lines:
        match = _HEADING.match(line)
        if match:
            found.append(normalize(match["text"]))
    return found


def table_rows(lines: Sequence[str]) -> list[tuple[int, list[str]]]:
    """``(line number, cells)`` for each Markdown table row, separator rows skipped.

    Cells are stripped of asterisks and code ticks.
    """
    rows: list[tuple[int, list[str]]] = []
    for number, line in enumerate(lines, start=1):
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        inner = stripped[1:-1] if stripped.endswith("|") else stripped[1:]
        cells = [re.sub(r"[*`]", "", cell).strip() for cell in re.split(r"(?<!\\)\|", inner)]
        if all(_SEPARATOR_CELL.fullmatch(cell) for cell in cells if cell) and any(cells):
            continue
        rows.append((number, cells))
    return rows


def _id_rows(
    lines: Sequence[str], pattern: re.Pattern[str]
) -> dict[str, list[tuple[int, list[str]]]]:
    """Table rows grouped by the ids (cells matching ``pattern`` exactly) they define."""
    found: dict[str, list[tuple[int, list[str]]]] = defaultdict(list)
    for number, cells in table_rows(lines):
        for ident in {cell for cell in cells if pattern.fullmatch(cell)}:
            found[ident].append((number, cells))
    return found


def _check_id_table(
    lines: Sequence[str],
    path: str,
    expected: Sequence[str],
    pattern: re.Pattern[str],
    prefix: str,
    what: str,
) -> tuple[list[Finding], dict[str, list[tuple[int, list[str]]]]]:
    """Missing, duplicated and unknown ids. Returns the findings and the row index."""
    findings: list[Finding] = []
    rows = _id_rows(lines, pattern)
    for ident in expected:
        count = len(rows.get(ident, []))
        if count == 0:
            findings.append(Finding(path, 0, f"{prefix}-ID-MISSING", f"{ident}: no {what} row"))
        elif count > 1:
            where = ", ".join(str(n) for n, _ in rows[ident])
            message = f"{ident} appears in {count} rows (lines {where}); expected exactly one"
            findings.append(Finding(path, rows[ident][1][0], f"{prefix}-ID-DUPLICATE", message))
    for ident in sorted(set(rows) - set(expected)):
        findings.append(
            Finding(
                path, rows[ident][0][0], f"{prefix}-ID-UNKNOWN", f"{ident} is not an expected id"
            )
        )
    return findings, rows


# ---------------------------------------------------------------------------
# Checks (pure functions over text)
# ---------------------------------------------------------------------------


def expected_adr_status(number: int | None) -> str:
    """The front matter status ADR ``number`` must carry (``accepted`` unless listed)."""
    return ADR_EXPECTED_STATUS.get(number or 0, ADR_ACCEPTED)


def check_adr(text: str, path: str, number: int | None = None) -> list[Finding]:
    """Front matter, expected status, required sections and the keywords of one ADR."""
    findings: list[Finding] = []
    keys, consumed = parse_front_matter(text)
    if keys is None:
        findings.append(
            Finding(path, 1, "ADR-FRONT-MATTER", "missing or unterminated front matter block")
        )
        keys, consumed = {}, 0
    else:
        for key in ADR_REQUIRED_KEYS:
            if not keys.get(key):
                findings.append(
                    Finding(path, 1, "ADR-KEY", f"front matter key '{key}' is missing or empty")
                )
        status = keys.get("status", "")
        expected = expected_adr_status(number)
        if status and status.casefold() != expected.casefold():
            message = f"front matter key 'status' must be '{expected}', found '{status}'"
            findings.append(Finding(path, 1, "ADR-KEY", message))
        when = keys.get("date", "")
        if when and not _is_iso_date(when):
            message = f"front matter key 'date' is not YYYY-MM-DD: '{when}'"
            findings.append(Finding(path, 1, "ADR-KEY", message))
    body = mask_code_fences(text)
    present = set(headings(body[consumed:]))
    for section in ADR_REQUIRED_SECTIONS:
        if normalize(section) not in present:
            findings.append(Finding(path, 0, "ADR-SECTION", f"missing section '{section}'"))
    for label, pattern in ADR_KEYWORDS.get(number or 0, ()):
        if not re.search(pattern, text, re.IGNORECASE):
            findings.append(Finding(path, 0, "ADR-KEYWORD", f"must mention {label}"))
    return findings


def _is_iso_date(value: str) -> bool:
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def check_threat_model(text: str, path: str = "docs/threat-model.md") -> list[Finding]:
    """TM-01 to TM-16 each in exactly one table row with a planned test and a phase."""
    lines = mask_code_fences(text)
    findings, rows = _check_id_table(lines, path, THREAT_IDS, TM_ID, "TM", "threat")
    for ident in THREAT_IDS:
        if len(rows.get(ident, [])) != 1:
            continue
        number, cells = rows[ident][0]
        filled = [cell for cell in cells if cell]
        if len(filled) < TM_MIN_CELLS:
            message = (
                f"{ident}: row needs threat, test id, planned test name and phase "
                f"({len(filled)} of {TM_MIN_CELLS} cells filled)"
            )
            findings.append(Finding(path, number, "TM-ROW-INCOMPLETE", message))
        elif not any(PHASE_CELL.fullmatch(cell) for cell in filled):
            message = f"{ident}: no phase cell (a number 0 to 6, optionally 'Phase N')"
            findings.append(Finding(path, number, "TM-ROW-INCOMPLETE", message))
    return findings


def check_acceptance(text: str, path: str = "docs/acceptance-tests.md") -> list[Finding]:
    """AT-US1 to AT-US9 and AT-EC1 to AT-EC5 once each, every row naming a test id."""
    lines = mask_code_fences(text)
    findings, rows = _check_id_table(lines, path, ACCEPTANCE_IDS, AT_ID, "AT", "acceptance")
    for ident in ACCEPTANCE_IDS:
        if len(rows.get(ident, [])) != 1:
            continue
        number, cells = rows[ident][0]
        if not TEST_ID.search(" ".join(cells)):
            findings.append(
                Finding(path, number, "AT-TEST-ID", f"{ident}: row names no test id (test_<name>)")
            )
    return findings


def check_prd(text: str, path: str = "docs/prd.md") -> list[Finding]:
    """Required headings, and every story and exit criterion id mentioned."""
    lines = mask_code_fences(text)
    found = headings(lines)
    findings = [
        Finding(path, 0, "PRD-HEADING", f"missing heading for '{label}'")
        for label, pattern in PRD_HEADINGS
        if not any(re.search(pattern, heading) for heading in found)
    ]
    body = "\n".join(lines)
    findings.extend(
        Finding(path, 0, "PRD-ID", f"{ident} is not mentioned")
        for ident in PRD_IDS
        if not re.search(rf"\b{ident}\b", body)
    )
    return findings


def check_contributing(text: str, path: str = "CONTRIBUTING.md") -> list[Finding]:
    """Required sections of the contributing guide."""
    found = headings(mask_code_fences(text))
    return [
        Finding(path, 0, "CONTRIB-SECTION", f"missing section '{label}'")
        for label, pattern in CONTRIBUTING_HEADINGS
        if not any(re.search(pattern, heading) for heading in found)
    ]


_INLINE_LINK = re.compile(
    r"""!?\[[^\]]*\]\(\s*(?P<target><[^>]*>|[^)\s]*)(?:\s+(?:"[^"]*"|'[^']*'))?\s*\)"""
)
_REFERENCE_LINK = re.compile(r"^ {0,3}\[[^\]]+\]:\s*(?P<target><[^>]*>|\S+)")
_CODE_SPAN = re.compile(r"`[^`\n]*`")
_SCHEME = re.compile(r"^(?:[A-Za-z][A-Za-z0-9+.-]*:|//)")


def link_targets(text: str) -> list[tuple[int, str]]:
    """``(line number, raw target)`` for each inline and reference-style link outside code."""
    found: list[tuple[int, str]] = []
    for number, line in enumerate(mask_code_fences(text), start=1):
        scan = _CODE_SPAN.sub(lambda m: " " * len(m[0]), line)
        targets = [m["target"] for m in _INLINE_LINK.finditer(scan)]
        reference = _REFERENCE_LINK.match(scan)
        if reference:
            targets.append(reference["target"])
        found.extend((number, target.strip("<>").strip()) for target in targets)
    return found


def check_links(text: str, source: Path, root: Path) -> list[Finding]:
    """Relative links in ``text`` (the content of ``source``) that do not resolve to a file."""
    try:
        shown = source.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        shown = source.as_posix()
    findings: list[Finding] = []
    for number, raw in link_targets(text):
        if not raw or raw.startswith("#") or _SCHEME.match(raw):
            continue  # same-page anchors and external links are out of scope
        target = unquote(re.split(r"[#?]", raw, maxsplit=1)[0])
        if not target:
            continue
        if target.startswith("/"):
            findings.append(
                Finding(shown, number, "LINK-ABSOLUTE", f"link '{raw}' is not relative")
            )
        elif not (source.parent / target).is_file():
            findings.append(
                Finding(shown, number, "LINK-BROKEN", f"link '{raw}' does not resolve to a file")
            )
    return findings


# ---------------------------------------------------------------------------
# Filesystem layer
# ---------------------------------------------------------------------------


def _read(root: Path, relative: str) -> tuple[str | None, list[Finding]]:
    """File text, or None plus a DOC-MISSING or DOC-UNREADABLE finding."""
    path = root / relative
    if not path.is_file():
        return None, [Finding(relative, 0, "DOC-MISSING", "file does not exist")]
    try:
        return path.read_text(encoding="utf-8"), []
    except (OSError, UnicodeDecodeError) as exc:
        reason = exc.strerror if isinstance(exc, OSError) and exc.strerror else type(exc).__name__
        return None, [Finding(relative, 0, "DOC-UNREADABLE", f"cannot read file: {reason}")]


def _single_file_group(
    root: Path, relative: str, check: Callable[[str, str], list[Finding]]
) -> list[Finding]:
    text, problems = _read(root, relative)
    return problems if text is None else check(text, relative)


def _adr_group(root: Path) -> list[Finding]:
    findings: list[Finding] = []
    adr_dir = root / "docs" / "adr"
    for name in ("template.md", "README.md"):
        relative = f"docs/adr/{name}"
        if not (root / relative).is_file():
            findings.append(Finding(relative, 0, "DOC-MISSING", "file does not exist"))
    for number in ADR_NUMBERS:
        stem = f"ADR-{number:04d}"
        matches = sorted(adr_dir.glob(f"{stem}-*.md")) if adr_dir.is_dir() else []
        if not matches:
            findings.append(
                Finding(f"docs/adr/{stem}-*.md", 0, "DOC-MISSING", f"no file for {stem}")
            )
            continue
        for extra in matches[1:]:
            shown = extra.relative_to(root).as_posix()
            findings.append(Finding(shown, 0, "ADR-DUPLICATE", f"more than one file for {stem}"))
        relative = matches[0].relative_to(root).as_posix()
        text, problems = _read(root, relative)
        findings.extend(problems if text is None else check_adr(text, relative, number))
    return findings


def _links_group(root: Path) -> list[Finding]:
    files = sorted((root / "docs").rglob("*.md")) if (root / "docs").is_dir() else []
    contributing = root / "CONTRIBUTING.md"
    if contributing.is_file():
        files.append(contributing)
    findings: list[Finding] = []
    for path in files:
        relative = path.relative_to(root).as_posix()
        text, problems = _read(root, relative)
        findings.extend(problems if text is None else check_links(text, path, root))
    return sorted(findings)


def run_group(group: str, root: Path) -> list[Finding]:
    """Run one group against the files under ``root``."""
    if group == "prd":
        return _single_file_group(root, "docs/prd.md", check_prd)
    if group == "adr":
        return _adr_group(root)
    if group == "threat-model":
        return _single_file_group(root, "docs/threat-model.md", check_threat_model)
    if group == "acceptance":
        return _single_file_group(root, "docs/acceptance-tests.md", check_acceptance)
    if group == "contributing":
        return _single_file_group(root, "CONTRIBUTING.md", check_contributing)
    if group == "links":
        return _links_group(root)
    raise ValueError(f"unknown group: {group}")


def run(groups: Iterable[str], root: Path) -> list[Finding]:
    """Findings of the given groups, sorted and without duplicates."""
    return sorted({finding for group in groups for finding in run_group(group, root)})


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0] if __doc__ else None)
    parser.add_argument(
        "--only",
        action="append",
        choices=GROUPS,
        metavar="GROUP",
        help=f"run only this group; repeatable. Groups: {', '.join(GROUPS)}",
    )
    parser.add_argument(
        "--root", type=Path, default=REPO_ROOT, help="repo root (default: this repo)"
    )
    args = parser.parse_args(argv)

    if not args.root.is_dir():
        print(f"error: {args.root} is not a directory", file=sys.stderr)
        return 2
    groups: list[str] = list(dict.fromkeys(args.only or GROUPS))
    findings = run(groups, args.root)
    for finding in findings:
        print(finding)
    label = ", ".join(groups)
    if findings:
        print(f"docs-check: {len(findings)} problem(s) in groups: {label}", file=sys.stderr)
        return 1
    print(f"docs-check: ok ({label})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
