"""No reference to the old lexical-search extension or its vendor (free-tier retarget task 2.2).

Requirement 2.6, decision FT-01: outside a fixed history allowlist, the repository names neither
``pg_search`` nor ParadeDB, in any letter case. This module scans the code, test, infra and
build paths and the documents listed in ``SCAN_ROOTS``, which since task 12.3 include ``docs/``
and ``CONTRIBUTING.md``. ``brainstorm.md`` and ``development_plan.md`` are source documents that
keep history lines by design (Requirement 2.6), so they are outside the scan.

Three allowlists:

- ``HISTORY_ALLOWLIST`` and ``HISTORY_LINE_RULES``: the permanent history exceptions of
  Requirement 2.6 (ADR-0001, links to its file name, its ADR index row, ADR-0013's history,
  the 27 Oct 2026 S2 report and results, the evidence trees, ``var/``, the source-document
  lines kept as history). Since task 12.3 the documents among them are scanned, so the
  exceptions are enforced and not only listed.
- ``SELF_ALLOWLIST``: this module and the tests that assert absence; they must spell the names.
- ``TEMPORARY_ALLOWLIST``: files that still carry the old names until the task named next to
  each removes them. ``test_temporary_allowlist_entries_still_need_it`` fails as soon as an
  entry no longer matches, so each of those tasks has to delete its entries here.

Gitignored directories (virtual environments, caches, ``node_modules``, ``dist``, ``var``) are
skipped. Files are read as bytes, so binary and non-UTF-8 files are scanned too.
"""

from __future__ import annotations

import fnmatch
import os
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Final

REPO_ROOT: Final = Path(__file__).resolve().parents[2]

SCAN_ROOTS: Final = (
    "apps",
    "packages",
    "scripts",
    "tests",
    "infra",
    "mk",
    ".github",
    "docker-compose.yml",
    ".env.example",
    "docs",
    "CONTRIBUTING.md",
)

FORBIDDEN: Final = re.compile(rb"pg_search|paradedb", re.IGNORECASE)

# Directory names that .gitignore excludes; nothing in them is part of the repository.
SKIP_DIRS: Final = frozenset(
    {
        ".venv",
        "__pycache__",
        "node_modules",
        ".pnpm-store",
        "dist",
        "htmlcov",
        "var",
        ".mypy_cache",
        ".ruff_cache",
        ".pytest_cache",
        ".hypothesis",
    }
)

HISTORY_ALLOWLIST: Final = (
    "docs/adr/ADR-0001-*.md",
    "docs/adr/ADR-0013-*.md",  # only its history sections name the old store (design §15)
    "docs/spikes/S2/2026-10-27-report.md",
    "docs/spikes/S2/2026-10-27-results.json",
    "docs/evidence/phase-0/*",
    "docs/evidence/free-tier/*",
    "var/*",
)
"""Path patterns (fnmatch, ``*`` crosses ``/``) exempt as history (Requirement 2.6)."""

ADR_0001_FILE_STEM: Final = b"ADR-0001-postgres-paradedb-pgvector-store"

HISTORY_LINE_RULES: Final = (
    # Its row in the ADR index.
    ("docs/adr/README.md", re.compile(rb"^\|\s*\[ADR-0001\]")),
)
"""``(path, line pattern)``: matching lines of that file are exempt (Requirement 2.6)."""

SELF_ALLOWLIST: Final = (
    "tests/scripts/test_no_paradedb.py",  # this scan
    "tests/scripts/test_dev_env.py",  # asserts .env.example no longer names the vendor
    "tests/freetier/test_freetier_profile.py",  # asserts the extension is absent from ft-db
)

TEMPORARY_ALLOWLIST: Final[dict[str, str]] = {}
"""Path -> the task that removes the references. Empty since task 10.1."""


def iter_files(root: Path, entries: tuple[str, ...] = SCAN_ROOTS) -> Iterator[Path]:
    """Every file under ``entries`` (relative to ``root``), skipping gitignored directories."""
    for entry in entries:
        path = root / entry
        if path.is_file():
            yield path
            continue
        if not path.is_dir():
            continue
        for directory, subdirs, names in os.walk(path):
            subdirs[:] = sorted(
                d for d in subdirs if d not in SKIP_DIRS and not d.endswith(".egg-info")
            )
            for name in sorted(names):
                if name != ".DS_Store":
                    yield Path(directory) / name


def _line_exempt(relative: str, line: bytes) -> bool:
    return any(path == relative and rule.search(line) for path, rule in HISTORY_LINE_RULES)


def offending_lines(relative: str, data: bytes) -> list[int]:
    """1-based numbers of lines that name the old extension or vendor outside the history rules.

    Links to ADR-0001's file name are not references (Requirement 2.6), so the file stem is
    removed before the search.
    """
    found: list[int] = []
    for number, line in enumerate(data.splitlines(), start=1):
        if _line_exempt(relative, line):
            continue
        if FORBIDDEN.search(line.replace(ADR_0001_FILE_STEM, b"")):
            found.append(number)
    return found


def allowlisted(relative: str) -> bool:
    if relative in SELF_ALLOWLIST or relative in TEMPORARY_ALLOWLIST:
        return True
    return any(fnmatch.fnmatchcase(relative, pattern) for pattern in HISTORY_ALLOWLIST)


def scan(root: Path, entries: tuple[str, ...] = SCAN_ROOTS) -> dict[str, list[int]]:
    """``relative path -> offending line numbers`` for every non-allowlisted file."""
    hits: dict[str, list[int]] = {}
    for path in iter_files(root, entries):
        relative = path.relative_to(root).as_posix()
        if allowlisted(relative):
            continue
        lines = offending_lines(relative, path.read_bytes())
        if lines:
            hits[relative] = lines
    return hits


# --- ADR-0013 keeps the old store in its history sections only ---------------------------------

ADR_0013_GLOB: Final = "docs/adr/ADR-0013-*.md"
_HEADING: Final = re.compile(rb"^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$")


def lines_outside_history(data: bytes) -> list[int]:
    """1-based numbers of lines that name the old store outside a section titled "History".

    A section is a heading and everything below it until the next heading of the same or a
    higher level, so a "History" subsection covers its own sub-subsections.
    """
    stack: list[tuple[int, bytes]] = []
    found: list[int] = []
    for number, line in enumerate(data.splitlines(), start=1):
        heading = _HEADING.match(line)
        if heading:
            level = len(heading[1])
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, heading[2].lower()))
        if any(b"history" in title for _, title in stack):
            continue
        if FORBIDDEN.search(line.replace(ADR_0001_FILE_STEM, b"")):
            found.append(number)
    return found


def test_adr_0013_names_the_old_store_only_in_its_history_section() -> None:
    files = sorted(REPO_ROOT.glob(ADR_0013_GLOB))
    assert len(files) == 1, f"expected one ADR-0013 file, found {len(files)}"
    data = files[0].read_bytes()
    assert lines_outside_history(data) == []
    # The exception is in use: the history section really does name the old store.
    assert FORBIDDEN.search(data.replace(ADR_0001_FILE_STEM, b""))


def test_history_section_rule_on_samples() -> None:
    sample = (
        b"# Title\n"
        b"Mentions ParadeDB here.\n"  # 2: outside
        b"## More Information\n"
        b"Links stay clean.\n"
        b"### History\n"
        b"The pg_search extension was retired.\n"  # exempt
        b"#### Details\n"
        b"paradedb again.\n"  # exempt: inside History
        b"## Next section\n"
        b"pg_search is back.\n"  # 10: outside
    )
    assert lines_outside_history(sample) == [2, 10]
    link = b"See [ADR-0001](ADR-0001-postgres-paradedb-pgvector-store.md).\n"
    assert lines_outside_history(link) == []


# --- the scan --------------------------------------------------------------------------------


def test_no_reference_outside_the_allowlists() -> None:
    hits = scan(REPO_ROOT)
    assert hits == {}, "pg_search or ParadeDB named in: " + ", ".join(
        f"{path} (lines {', '.join(map(str, lines))})" for path, lines in sorted(hits.items())
    )


def test_every_scan_root_exists() -> None:
    assert [entry for entry in SCAN_ROOTS if not (REPO_ROOT / entry).exists()] == []


def test_scan_covers_the_documents() -> None:
    assert {"docs", "CONTRIBUTING.md"} <= set(SCAN_ROOTS)
    scanned = {path.relative_to(REPO_ROOT).as_posix() for path in iter_files(REPO_ROOT)}
    assert {"CONTRIBUTING.md", "docs/prd.md", "docs/deploy.md", "docs/threat-model.md"} <= scanned
    assert any(name.startswith("docs/adr/ADR-0014-") for name in scanned)
    assert any(name.startswith("docs/adr/ADR-0015-") for name in scanned)


def test_documents_outside_the_history_allowlist_are_checked(tmp_path: Path) -> None:
    def put(relative: str, text: str) -> None:
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)

    put("CONTRIBUTING.md", "U6: ParadeDB is AGPL\n")
    put("docs/prd.md", "The stack is PostgreSQL 18 with ParadeDB.\n")
    put("docs/adr/ADR-0014-x.md", "clean\n")
    put("docs/adr/ADR-0001-postgres-paradedb-pgvector-store.md", "paradedb\n")
    put("docs/adr/ADR-0013-x.md", "pg_search history\n")
    put("docs/spikes/S2/2026-10-27-report.md", "BM25 via pg_search\n")
    put("docs/spikes/S2/2026-11-02-report.md", "FTS only\n")
    put("docs/evidence/free-tier/notes.md", "paradedb\n")
    hits = scan(tmp_path, ("docs", "CONTRIBUTING.md"))
    assert hits == {"CONTRIBUTING.md": [1], "docs/prd.md": [1]}


def test_temporary_allowlist_entries_still_need_it() -> None:
    """A task that removes the last reference from a file must also drop its entry here."""
    stale = [
        f"{path} (task {task})"
        for path, task in sorted(TEMPORARY_ALLOWLIST.items())
        if not (REPO_ROOT / path).is_file()
        or not offending_lines(path, (REPO_ROOT / path).read_bytes())
    ]
    assert stale == [], "drop these TEMPORARY_ALLOWLIST entries: " + ", ".join(stale)


def test_temporary_allowlist_names_only_tasks_3_1_and_10_1() -> None:
    assert set(TEMPORARY_ALLOWLIST.values()) <= {"3.1", "10.1"}


# --- the scanner itself ----------------------------------------------------------------------


def test_matching_is_case_insensitive_and_reports_line_numbers() -> None:
    data = b"ok\nimage: ParadeDB/paradedb:1\nCREATE EXTENSION PG_SEARCH;\nvector\n"
    assert offending_lines("apps/x.py", data) == [2, 3]
    assert offending_lines("apps/x.py", b"vector btree_gist pg_trgm search\n") == []


def test_links_to_the_adr_0001_file_name_are_not_references() -> None:
    link = b"See [ADR-0001](../adr/ADR-0001-postgres-paradedb-pgvector-store.md).\n"
    assert offending_lines("docs/x.md", link) == []
    assert offending_lines("docs/x.md", link + b"ParadeDB is gone\n") == [2]


def test_only_the_adr_index_row_of_adr_0001_is_exempt() -> None:
    data = b"| [ADR-0001](ADR-0001-x.md) | with ParadeDB | superseded |\n| ADR-0006 | ParadeDB |\n"
    assert offending_lines("docs/adr/README.md", data) == [2]
    assert offending_lines("docs/other.md", data) == [1, 2]


def test_scan_skips_ignored_directories_and_honors_the_allowlists(tmp_path: Path) -> None:
    def put(relative: str, text: str) -> None:
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)

    put("apps/a.py", "x = 'pg_search'\n")
    put("apps/.venv/lib/b.py", "paradedb\n")
    put("apps/web/node_modules/c.js", "paradedb\n")
    put("packages/p/__pycache__/d.pyc", "paradedb\n")
    put("docs/evidence/phase-0/e.md", "ParadeDB\n")
    put("tests/scripts/test_dev_env.py", "paradedb\n")  # SELF_ALLOWLIST
    put("docker-compose.yml", "image: pgvector/pgvector:0.8.6-pg18\n")
    hits = scan(tmp_path, ("apps", "packages", "scripts", "tests", "docs", "docker-compose.yml"))
    assert hits == {"apps/a.py": [1]}
