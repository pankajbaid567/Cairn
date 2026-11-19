"""Docs checker: valid and invalid sample Markdown per group (Requirements 18.3 to 18.5, 19.4)."""

# The ADR group also checks ADR-0013 to ADR-0015 and the superseded status of ADR-0001
# (free-tier retarget task 12.1, Requirements 19.2 and 19.4).

from __future__ import annotations

import re
from pathlib import Path

import pytest

from scripts import check_docs
from scripts.check_docs import Finding

# ---------------------------------------------------------------------------
# Sample documents
# ---------------------------------------------------------------------------


def adr_text(
    *,
    status: str = "accepted",
    date: str = "2026-10-27",
    makers: str = "Pankaj",
    drop_key: str | None = None,
    drop_section: str | None = None,
    extra: str = "",
) -> str:
    front = {"status": status, "date": date, "decision-makers": makers}
    front_lines = [f"{k}: {v}" for k, v in front.items() if k != drop_key]
    sections = [
        "Context and Problem Statement",
        "Considered Options",
        "Decision Outcome",
        "Consequences",
        "Confirmation",
    ]
    body = [f"## {s}\n\nText.\n" for s in sections if s != drop_section]
    return "\n".join(["---", *front_lines, "---", "", "# Title", "", *body, extra])


ADR2_EXTRA = "DBOS lives in database `cairn_dbos`. Same-transaction enqueue is revisited in 3.4."
ADR4_EXTRA = "Roles `cairn_owner` and `cairn_app`. No foreign keys into restricted tables."
ADR13_EXTRA = (
    "Search uses websearch_to_tsquery and ts_rank_cd. The admin neondb_owner has BYPASSRLS. "
    "0001_baseline is edited once."
)
ADR14_EXTRA = "Database cairn_dbos, the outbox, a 512 MB limit, the freetier profile, /healthz."
ADR15_EXTRA = "Each neuron is budgeted. A different model family judges. bge-small-en-v1.5 embeds."
ADR_EXTRAS = {2: ADR2_EXTRA, 4: ADR4_EXTRA, 13: ADR13_EXTRA, 14: ADR14_EXTRA, 15: ADR15_EXTRA}
SUPERSEDED = "superseded by ADR-0013"


def threat_text(skip: tuple[str, ...] = (), repeat: str | None = None) -> str:
    rows = ["| Threat | Test id | Planned test | Phase |", "|---|---|---|---|"]
    for n in range(1, 17):
        ident = f"TM-{n:02d}"
        if ident not in skip:
            rows.append(f"| Threat {n} | {ident} | test_threat_{n} | {n % 7} |")
    if repeat:
        rows.append(f"| Again | **{repeat}** | test_again | 1 |")
    return "# Threat model\n\n" + "\n".join(rows) + "\n"


def acceptance_text(skip: tuple[str, ...] = (), no_test: str | None = None) -> str:
    ids = [f"AT-US{n}" for n in range(1, 10)] + [f"AT-EC{n}" for n in range(1, 6)]
    rows = ["| Id | Statement | Tests | Phase |", "|---|---|---|---|"]
    for ident in ids:
        if ident in skip:
            continue
        tests = "to be decided" if ident == no_test else f"`test_{ident.lower().replace('-', '_')}`"
        rows.append(f"| {ident} | Statement | {tests} | 1 |")
    return "# Acceptance tests\n\n" + "\n".join(rows) + "\n"


PRD_HEADINGS = [
    "Problem",
    "Personas and key moments",
    "Jobs to be done",
    "User stories",
    "MVP scope",
    "Non-goals",
    "Journeys and screens",
    "Metrics",
    "MVP exit criteria",
]


def prd_text(drop_heading: str | None = None, drop_id: str | None = None) -> str:
    ids = [f"US{n}" for n in range(1, 10)] + [f"EC{n}" for n in range(1, 6)]
    mentions = " ".join(i for i in ids if i != drop_id)
    parts = [f"## {h}\n\nSee {mentions}.\n" for h in PRD_HEADINGS if h != drop_heading]
    return "# PRD\n\n" + "\n".join(parts)


CONTRIB_HEADINGS = [
    "Quick start",
    "Definition of done",
    "Quality gates",
    "AI-assisted workflow",
    "ADR process",
    "Data rules",
    "User-owned steps",
]


def contributing_text(drop: str | None = None) -> str:
    return "# Contributing\n\n" + "\n".join(
        f"## {h}\n\nText.\n" for h in CONTRIB_HEADINGS if h != drop
    )


def write(root: Path, relative: str, text: str) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def codes(findings: list[Finding]) -> list[str]:
    return [f.code for f in findings]


def write_all_adrs(root: Path) -> None:
    write(root, "docs/adr/template.md", "# Template\n")
    write(root, "docs/adr/README.md", "# Index\n")
    for n in check_docs.ADR_NUMBERS:
        status = SUPERSEDED if n == 1 else "accepted"
        write(
            root,
            f"docs/adr/ADR-{n:04d}-sample.md",
            adr_text(status=status, extra=ADR_EXTRAS.get(n, "")),
        )


# ---------------------------------------------------------------------------
# ADR
# ---------------------------------------------------------------------------


def test_valid_adr_passes() -> None:
    assert check_docs.check_adr(adr_text(), "a.md", 3) == []


@pytest.mark.parametrize("key", ["status", "date", "decision-makers"])
def test_adr_missing_front_matter_key_is_named(key: str) -> None:
    found = check_docs.check_adr(adr_text(drop_key=key), "a.md", 3)
    assert codes(found) == ["ADR-KEY"]
    assert f"'{key}'" in found[0].message


@pytest.mark.parametrize(
    "section",
    [
        "Context and Problem Statement",
        "Considered Options",
        "Decision Outcome",
        "Consequences",
        "Confirmation",
    ],
)
def test_adr_missing_section_is_named(section: str) -> None:
    found = check_docs.check_adr(adr_text(drop_section=section), "a.md", 3)
    assert codes(found) == ["ADR-SECTION"]
    assert section in found[0].message


def test_adr_without_front_matter_block() -> None:
    found = check_docs.check_adr("# Title\n\n## Consequences\n", "a.md", 3)
    assert "ADR-FRONT-MATTER" in codes(found)


@pytest.mark.parametrize(
    ("kwargs", "fragment"),
    [
        ({"status": "proposed"}, "'accepted'"),
        ({"date": "27 Oct 2026"}, "YYYY-MM-DD"),
        ({"date": "2026-02-31"}, "YYYY-MM-DD"),
    ],
)
def test_adr_bad_front_matter_values(kwargs: dict[str, str], fragment: str) -> None:
    found = check_docs.check_adr(adr_text(**kwargs), "a.md", 3)
    assert codes(found) == ["ADR-KEY"]
    assert fragment in found[0].message


def test_adr_decision_makers_list_form_is_accepted() -> None:
    text = adr_text().replace("decision-makers: Pankaj", "decision-makers:\n  - Pankaj")
    assert check_docs.check_adr(text, "a.md", 3) == []


def test_adr_sections_inside_code_fences_do_not_count() -> None:
    text = adr_text(drop_section="Confirmation") + "\n```\n## Confirmation\n```\n"
    assert codes(check_docs.check_adr(text, "a.md", 3)) == ["ADR-SECTION"]


def test_adr2_and_adr4_keywords() -> None:
    assert check_docs.check_adr(adr_text(extra=ADR2_EXTRA), "a.md", 2) == []
    assert check_docs.check_adr(adr_text(extra=ADR4_EXTRA), "a.md", 4) == []
    missing2 = check_docs.check_adr(adr_text(), "a.md", 2)
    assert codes(missing2) == ["ADR-KEYWORD"] * 3
    missing4 = check_docs.check_adr(adr_text(extra="`cairn_app` only"), "a.md", 4)
    messages = " ".join(f.message for f in missing4)
    assert "cairn_owner" in messages
    assert "foreign-key rule" in messages
    assert "cairn_app" not in messages


def test_adr1_must_be_superseded_and_the_others_accepted() -> None:
    assert check_docs.check_adr(adr_text(status=SUPERSEDED), "a.md", 1) == []
    found = check_docs.check_adr(adr_text(), "a.md", 1)
    assert codes(found) == ["ADR-KEY"]
    assert f"'{SUPERSEDED}'" in found[0].message
    assert "found 'accepted'" in found[0].message
    for number in (2, 3, 4, 5, 13, 14, 15):
        extra = ADR_EXTRAS.get(number, "")
        assert check_docs.check_adr(adr_text(extra=extra), "a.md", number) == []
        found = check_docs.check_adr(adr_text(status=SUPERSEDED, extra=extra), "a.md", number)
        assert codes(found) == ["ADR-KEY"]
        assert "'accepted'" in found[0].message


def test_adr_without_a_number_must_be_accepted() -> None:
    assert check_docs.check_adr(adr_text(), "a.md") == []
    assert codes(check_docs.check_adr(adr_text(status=SUPERSEDED), "a.md")) == ["ADR-KEY"]


def test_new_adr_keywords() -> None:
    for number in (13, 14, 15):
        extra = ADR_EXTRAS[number]
        assert check_docs.check_adr(adr_text(extra=extra), "a.md", number) == []
    missing13 = check_docs.check_adr(adr_text(), "a.md", 13)
    assert codes(missing13) == ["ADR-KEYWORD"] * 5
    for word in (
        "websearch_to_tsquery",
        "ts_rank_cd",
        "BYPASSRLS",
        "neondb_owner",
        "0001_baseline",
    ):
        assert any(word in f.message for f in missing13)
    missing14 = check_docs.check_adr(adr_text(extra="cairn_dbos only"), "a.md", 14)
    assert codes(missing14) == ["ADR-KEYWORD"] * 4
    assert not any("cairn_dbos" in f.message for f in missing14)
    missing15 = check_docs.check_adr(adr_text(extra="A neuron count."), "a.md", 15)
    assert [f.message for f in missing15] == [
        "must mention different model family",
        "must mention bge-small-en-v1.5",
    ]


def test_adr_group_reports_missing_files(tmp_path: Path) -> None:
    found = check_docs.run_group("adr", tmp_path)
    assert codes(found) == ["DOC-MISSING"] * 10  # template, index, eight ADRs


def test_adr_group_reports_a_missing_new_adr(tmp_path: Path) -> None:
    write_all_adrs(tmp_path)
    (tmp_path / "docs/adr/ADR-0014-sample.md").unlink()
    found = check_docs.run_group("adr", tmp_path)
    assert [(f.path, f.code) for f in found] == [("docs/adr/ADR-0014-*.md", "DOC-MISSING")]


def test_adr_group_reports_a_missing_keyword_in_a_new_adr(tmp_path: Path) -> None:
    write_all_adrs(tmp_path)
    write(tmp_path, "docs/adr/ADR-0015-sample.md", adr_text(extra="A neuron count."))
    found = check_docs.run_group("adr", tmp_path)
    assert codes(found) == ["ADR-KEYWORD"] * 2
    assert {f.path for f in found} == {"docs/adr/ADR-0015-sample.md"}


def test_adr_group_does_not_check_the_reserved_numbers(tmp_path: Path) -> None:
    write_all_adrs(tmp_path)
    write(tmp_path, "docs/adr/ADR-0006-sample.md", "not an ADR at all")
    assert check_docs.run_group("adr", tmp_path) == []


def test_the_repository_adrs_pass_the_adr_group() -> None:
    assert check_docs.run_group("adr", check_docs.REPO_ROOT) == []


def test_adr_group_passes_on_complete_set(tmp_path: Path) -> None:
    write_all_adrs(tmp_path)
    assert check_docs.run_group("adr", tmp_path) == []


def test_adr_group_flags_duplicate_and_broken_adr(tmp_path: Path) -> None:
    write_all_adrs(tmp_path)
    write(tmp_path, "docs/adr/ADR-0003-other.md", adr_text())
    write(
        tmp_path,
        "docs/adr/ADR-0001-sample.md",
        adr_text(status=SUPERSEDED, drop_section="Consequences"),
    )
    found = check_docs.run_group("adr", tmp_path)
    assert sorted(codes(found)) == ["ADR-DUPLICATE", "ADR-SECTION"]
    assert any(f.path == "docs/adr/ADR-0001-sample.md" for f in found)


# ---------------------------------------------------------------------------
# Threat model
# ---------------------------------------------------------------------------


def test_valid_threat_model_passes() -> None:
    assert check_docs.check_threat_model(threat_text()) == []


def test_threat_model_missing_id() -> None:
    found = check_docs.check_threat_model(threat_text(skip=("TM-07",)))
    assert codes(found) == ["TM-ID-MISSING"]
    assert "TM-07" in found[0].message


@pytest.mark.parametrize("ident", ["TM-15", "TM-16"])
def test_threat_model_requires_the_free_tier_ids(ident: str) -> None:
    found = check_docs.check_threat_model(threat_text(skip=(ident,)))
    assert codes(found) == ["TM-ID-MISSING"]
    assert ident in found[0].message


def test_threat_ids_run_from_tm_01_to_tm_16() -> None:
    assert check_docs.THREAT_IDS[0] == "TM-01"
    assert check_docs.THREAT_IDS[-1] == "TM-16"
    assert len(check_docs.THREAT_IDS) == 16


def test_the_repository_threat_model_passes_the_threat_model_group() -> None:
    assert check_docs.run_group("threat-model", check_docs.REPO_ROOT) == []


def test_free_tier_threat_rows_are_phase_0_and_name_tests_that_exist() -> None:
    text = (check_docs.REPO_ROOT / "docs" / "threat-model.md").read_text(encoding="utf-8")
    rows = {
        line.split("|")[1].strip(): line for line in text.splitlines() if line.startswith("| TM-")
    }
    sources = "\n".join(
        path.read_text(encoding="utf-8")
        for pattern in (
            "tests/**/test_*.py",
            "apps/*/tests/**/test_*.py",
            "packages/*/tests/**/test_*.py",
        )
        for path in check_docs.REPO_ROOT.glob(pattern)
    )
    for ident in ("TM-15", "TM-16"):
        cells = [cell.strip() for cell in rows[ident].strip().strip("|").split("|")]
        assert cells[-1] == "0"
        names = re.findall(r"`(test_[A-Za-z0-9_]+)`", cells[-2])
        assert names
        for name in names:
            assert f"def {name}(" in sources, f"{ident}: no test function {name}"


def test_threat_model_duplicate_id() -> None:
    found = check_docs.check_threat_model(threat_text(repeat="TM-03"))
    assert codes(found) == ["TM-ID-DUPLICATE"]
    assert "TM-03" in found[0].message


def test_threat_model_unknown_id() -> None:
    found = check_docs.check_threat_model(threat_text(repeat="TM-17"))
    assert codes(found) == ["TM-ID-UNKNOWN"]


def test_threat_model_prose_mentions_are_not_definitions() -> None:
    text = threat_text() + "\nSee TM-01 and TM-01 again, plus TM-99 in prose.\n"
    assert check_docs.check_threat_model(text) == []


def test_threat_model_incomplete_row() -> None:
    text = threat_text().replace("| Threat 2 | TM-02 | test_threat_2 | 2 |", "| Threat 2 | TM-02 |")
    found = check_docs.check_threat_model(text)
    assert codes(found) == ["TM-ROW-INCOMPLETE"]
    assert "TM-02" in found[0].message


def test_threat_model_row_without_phase() -> None:
    text = threat_text().replace(
        "| Threat 2 | TM-02 | test_threat_2 | 2 |", "| Threat 2 | TM-02 | t | soon |"
    )
    assert codes(check_docs.check_threat_model(text)) == ["TM-ROW-INCOMPLETE"]


def test_threat_ids_in_code_fences_do_not_count() -> None:
    fenced = "```\n" + threat_text() + "```\n"
    found = check_docs.check_threat_model(fenced)
    assert codes(found) == ["TM-ID-MISSING"] * 16


# ---------------------------------------------------------------------------
# Acceptance tests
# ---------------------------------------------------------------------------


def test_valid_acceptance_list_passes() -> None:
    assert check_docs.check_acceptance(acceptance_text()) == []


def test_acceptance_missing_ids() -> None:
    found = check_docs.check_acceptance(acceptance_text(skip=("AT-US9", "AT-EC1")))
    assert codes(found) == ["AT-ID-MISSING"] * 2
    assert {"AT-US9", "AT-EC1"} <= {m for f in found for m in f.message.split(":")[:1]}


def test_acceptance_row_without_test_id() -> None:
    found = check_docs.check_acceptance(acceptance_text(no_test="AT-EC3"))
    assert codes(found) == ["AT-TEST-ID"]
    assert "AT-EC3" in found[0].message


def test_acceptance_ids_are_not_prefix_matched() -> None:
    # AT-US1 must not satisfy AT-US10, and AT-US10 is unknown, not a duplicate of AT-US1.
    text = acceptance_text() + "| AT-US10 | x | test_x | 1 |\n"
    assert codes(check_docs.check_acceptance(text)) == ["AT-ID-UNKNOWN"]


# ---------------------------------------------------------------------------
# PRD and CONTRIBUTING
# ---------------------------------------------------------------------------


def test_valid_prd_passes() -> None:
    assert check_docs.check_prd(prd_text()) == []


@pytest.mark.parametrize("heading", PRD_HEADINGS)
def test_prd_missing_heading_is_named(heading: str) -> None:
    found = check_docs.check_prd(prd_text(drop_heading=heading))
    assert codes(found) == ["PRD-HEADING"]


def test_prd_missing_story_id() -> None:
    found = check_docs.check_prd(prd_text(drop_id="US7"))
    assert codes(found) == ["PRD-ID"]
    assert "US7" in found[0].message


def test_prd_id_must_be_a_whole_word() -> None:
    text = prd_text(drop_id="US1").replace("EC1", "EC1 US10")
    assert codes(check_docs.check_prd(text)) == ["PRD-ID"]


def test_valid_contributing_passes() -> None:
    assert check_docs.check_contributing(contributing_text()) == []


@pytest.mark.parametrize("heading", CONTRIB_HEADINGS)
def test_contributing_missing_section(heading: str) -> None:
    assert codes(check_docs.check_contributing(contributing_text(drop=heading))) == [
        "CONTRIB-SECTION"
    ]


# ---------------------------------------------------------------------------
# Links
# ---------------------------------------------------------------------------


def test_links_resolve_relative_to_the_document(tmp_path: Path) -> None:
    write(tmp_path, "docs/adr/other.md", "x")
    write(tmp_path, "docs/spikes/report.md", "x")
    source = write(
        tmp_path,
        "docs/adr/a.md",
        "[ok](other.md) [up](../spikes/report.md#frag) [ext](https://x.example/a) "
        "[anchor](#top) [mail](mailto:a@b.example) [sp](<other.md>)\n"
        "[ref]: other.md\n",
    )
    assert check_docs.check_links(source.read_text(), source, tmp_path) == []


def test_broken_links_are_reported_with_line_numbers(tmp_path: Path) -> None:
    write(tmp_path, "docs/exists.md", "x")
    text = '[a](exists.md)\n\n[b](missing.md) and ![img](gone.png "title")\n[r]: nowhere.md\n'
    source = write(tmp_path, "docs/a.md", text)
    found = check_docs.check_links(text, source, tmp_path)
    assert [(f.line, f.code) for f in found] == [(3, "LINK-BROKEN")] * 2 + [(4, "LINK-BROKEN")]
    assert found[0].path == "docs/a.md"
    assert "missing.md" in found[0].message


def test_directory_and_absolute_links_are_reported(tmp_path: Path) -> None:
    (tmp_path / "docs" / "adr").mkdir(parents=True)
    text = "[dir](adr) [abs](/etc/hosts)\n"
    source = write(tmp_path, "docs/a.md", text)
    assert codes(check_docs.check_links(text, source, tmp_path)) == ["LINK-BROKEN", "LINK-ABSOLUTE"]


def test_links_in_code_are_ignored(tmp_path: Path) -> None:
    text = "`[a](missing.md)`\n\n```md\n[b](missing.md)\n```\n"
    source = write(tmp_path, "docs/a.md", text)
    assert check_docs.check_links(text, source, tmp_path) == []


def test_links_group_covers_docs_and_contributing(tmp_path: Path) -> None:
    write(tmp_path, "docs/a.md", "[x](nope.md)")
    write(tmp_path, "CONTRIBUTING.md", "[y](docs/a.md) [z](docs/zzz.md)")
    found = check_docs.run_group("links", tmp_path)
    assert [(f.path, f.code) for f in found] == [
        ("CONTRIBUTING.md", "LINK-BROKEN"),
        ("docs/a.md", "LINK-BROKEN"),
    ]


def test_links_group_with_no_docs_is_clean(tmp_path: Path) -> None:
    assert check_docs.run_group("links", tmp_path) == []


# ---------------------------------------------------------------------------
# Files and command line
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("group", ["prd", "threat-model", "acceptance", "contributing"])
def test_missing_file_is_a_finding_not_a_crash(group: str, tmp_path: Path) -> None:
    found = check_docs.run_group(group, tmp_path)
    assert codes(found) == ["DOC-MISSING"]


def test_unreadable_file_is_a_finding(tmp_path: Path) -> None:
    path = tmp_path / "docs" / "prd.md"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"\xff\xfe\x00bad")
    assert codes(check_docs.run_group("prd", tmp_path)) == ["DOC-UNREADABLE"]


def test_main_exit_codes_and_only_groups(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write(tmp_path, "docs/prd.md", prd_text())
    root = str(tmp_path)
    assert check_docs.main(["--root", root, "--only", "prd"]) == 0
    assert "ok (prd)" in capsys.readouterr().out
    assert check_docs.main(["--root", root, "--only", "prd", "--only", "contributing"]) == 1
    captured = capsys.readouterr()
    assert "CONTRIBUTING.md: DOC-MISSING" in captured.out
    assert "1 problem(s)" in captured.err
    assert check_docs.main(["--root", str(tmp_path / "nope")]) == 2


def test_main_rejects_unknown_group(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as exc:
        check_docs.main(["--root", str(tmp_path), "--only", "bogus"])
    assert exc.value.code == 2


def test_default_runs_every_group_and_output_is_sorted(tmp_path: Path) -> None:
    findings = check_docs.run(check_docs.GROUPS, tmp_path)
    assert findings == sorted(findings)
    paths = {f.path for f in findings}
    assert {
        "docs/prd.md",
        "docs/threat-model.md",
        "docs/acceptance-tests.md",
        "CONTRIBUTING.md",
    } <= paths
