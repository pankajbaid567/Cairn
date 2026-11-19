"""Phase 0 exit gate: source-document hash check, RAM evidence and checklist (Requirement 23)."""

import hashlib
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from scripts import phase0_exit
from scripts.phase0_exit import (
    FU_STEPS,
    GATE_LINES,
    RUN_TARGETS,
    SOURCE_DOCS,
    USER_STEPS,
    FreetierEvidence,
    RamEvidence,
    check_source_docs,
    find_freetier_evidence,
    find_ram_evidence,
    parse_sha_file,
    render_checklist,
    run_checks,
    verify_source_docs,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
BRAIN = b"brainstorm text"
PLAN = b"plan text"


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha_text(brain: bytes = BRAIN, plan: bytes = PLAN) -> str:
    return f"{_digest(brain)}  brainstorm.md\n{_digest(plan)}  development_plan.md\n"


def _actual(brain: bytes | None = BRAIN, plan: bytes | None = PLAN) -> dict[str, str | None]:
    return {
        "brainstorm.md": None if brain is None else _digest(brain),
        "development_plan.md": None if plan is None else _digest(plan),
    }


# --- hash check ---------------------------------------------------------------------------


def test_matching_documents_have_no_problems() -> None:
    assert verify_source_docs(_sha_text(), _actual()) == []


def test_changed_document_is_reported_by_name() -> None:
    problems = verify_source_docs(_sha_text(), _actual(plan=b"edited"))
    assert len(problems) == 1
    assert problems[0].startswith("development_plan.md changed")


def test_missing_hash_file_missing_entry_and_missing_document() -> None:
    assert "is missing" in verify_source_docs(None, _actual())[0]
    only_brain = f"{_digest(BRAIN)}  brainstorm.md\n"
    assert verify_source_docs(only_brain, _actual()) == [
        "development_plan.md has no entry in source-docs.sha256"
    ]
    assert verify_source_docs(_sha_text(), _actual(brain=None)) == [
        "brainstorm.md is missing from the repo root"
    ]


def test_parse_sha_file_accepts_binary_marker_and_flags_bad_lines() -> None:
    entries, problems = parse_sha_file(f"{_digest(BRAIN)} *a.md\n\nnot a hash line\n")
    assert entries == {"a.md": _digest(BRAIN)}
    assert problems == ["line 3 is not '<sha256>  <name>'"]
    _, duplicate = parse_sha_file(f"{_digest(BRAIN)}  a.md\n{_digest(PLAN)}  a.md\n")
    assert duplicate == ["a.md is listed twice"]


@given(brain=st.binary(max_size=200), plan=st.binary(max_size=200), edit=st.binary(min_size=1))
def test_any_change_to_a_document_is_detected(brain: bytes, plan: bytes, edit: bytes) -> None:
    """**Validates: Requirements 1.5**"""
    record = _sha_text(brain, plan)
    assert verify_source_docs(record, _actual(brain, plan)) == []
    changed = brain + edit
    assert verify_source_docs(record, _actual(changed, plan)) != []


def test_check_source_docs_reads_files(tmp_path: Path) -> None:
    (tmp_path / "brainstorm.md").write_bytes(BRAIN)
    (tmp_path / "development_plan.md").write_bytes(PLAN)
    sha_file = tmp_path / "source-docs.sha256"
    assert check_source_docs(tmp_path, sha_file)  # hash file missing
    sha_file.write_text(_sha_text(), encoding="utf-8")
    assert check_source_docs(tmp_path, sha_file) == []
    (tmp_path / "brainstorm.md").write_bytes(BRAIN + b"!")
    assert check_source_docs(tmp_path, sha_file)[0].startswith("brainstorm.md changed")


def test_recorded_hashes_match_the_real_documents() -> None:
    assert check_source_docs() == []


# --- RAM evidence -------------------------------------------------------------------------


PGVECTOR = "pgvector/pgvector:0.8.6-pg18@sha256:" + "a" * 64
EARLIER_IMAGE = "example/earlier-db:1.2.3-pg18@sha256:" + "b" * 64


def _images(db_reference: str | None = PGVECTOR) -> list[dict[str, object]]:
    images: list[dict[str, object]] = [
        {"container": "cairn-api-1", "reference": "cairn-api:local"},
        {"container": "cairn-web-1", "reference": "cairn-web:local"},
    ]
    if db_reference is not None:
        images.insert(1, {"container": "cairn-db-1", "reference": db_reference})
    return images


def _write_ram(
    directory: Path,
    date: str,
    result: str = "pass",
    markdown: bool = True,
    db_reference: str | None = PGVECTOR,
) -> None:
    payload = {
        "result": result,
        "verdict": f"{result.upper()}: total peak 0.22 GiB",
        "images": _images(db_reference),
    }
    (directory / f"core-ram-{date}.json").write_text(json.dumps(payload), encoding="utf-8")
    if markdown:
        (directory / f"core-ram-{date}.md").write_text("# report\n", encoding="utf-8")


def test_ram_evidence_uses_the_newest_report(tmp_path: Path) -> None:
    _write_ram(tmp_path, "2026-10-28", result="fail")
    _write_ram(tmp_path, "2026-10-30")
    evidence, problems = find_ram_evidence(tmp_path)
    assert problems == []
    assert evidence == RamEvidence(
        "core-ram-2026-10-30.md", "PASS: total peak 0.22 GiB", True, PGVECTOR
    )


def test_ram_evidence_problems(tmp_path: Path) -> None:
    assert "make ram-report" in find_ram_evidence(tmp_path)[1][0]
    _write_ram(tmp_path, "2026-10-30", result="fail", markdown=False)
    _, problems = find_ram_evidence(tmp_path)
    assert len(problems) == 2
    assert "core-ram-2026-10-30.md is missing" in problems[0]
    assert "does not record a pass" in problems[1]
    (tmp_path / "core-ram-2026-10-31.json").write_text("{", encoding="utf-8")
    assert "not readable JSON" in find_ram_evidence(tmp_path)[1][-1]


def test_ram_evidence_requires_the_pgvector_db_image(tmp_path: Path) -> None:
    """**Validates: Requirements 21.2**"""
    _write_ram(tmp_path, "2026-10-30", db_reference=EARLIER_IMAGE)
    evidence, problems = find_ram_evidence(tmp_path)
    assert evidence is not None
    assert evidence.passed
    assert len(problems) == 1
    assert "example/earlier-db:1.2.3-pg18" in problems[0]
    assert "pgvector/pgvector" in problems[0]
    assert "sha256" not in problems[0]
    _write_ram(tmp_path, "2026-10-31", db_reference=None)  # no db container in the report
    assert "names no db image" in find_ram_evidence(tmp_path)[1][0]
    _write_ram(tmp_path, "2026-11-01")  # the newest report decides, not the old one
    assert find_ram_evidence(tmp_path)[1] == []
    (tmp_path / "core-ram-2026-11-02.json").write_text(
        json.dumps({"result": "pass", "images": "nonsense"}), encoding="utf-8"
    )
    (tmp_path / "core-ram-2026-11-02.md").write_text("# report\n", encoding="utf-8")
    assert "names no db image" in find_ram_evidence(tmp_path)[1][0]


def test_committed_ram_evidence_passes() -> None:
    evidence, problems = find_ram_evidence()
    assert problems == []
    assert evidence is not None
    assert evidence.passed


# --- free-tier RAM evidence ---------------------------------------------------------------
def _write_freetier(directory: Path, date: str, result: str = "pass") -> None:
    payload = {"result": result, "verdict": f"{result.upper()}: every budget rule holds"}
    (directory / f"freetier-ram-{date}.json").write_text(json.dumps(payload), encoding="utf-8")


def test_freetier_evidence_uses_the_newest_report(tmp_path: Path) -> None:
    """**Validates: Requirements 21.1**"""
    _write_freetier(tmp_path, "2026-11-02", result="fail")
    _write_freetier(tmp_path, "2026-11-03")
    evidence, problems = find_freetier_evidence(tmp_path)
    assert problems == []
    assert evidence == FreetierEvidence(
        "freetier-ram-2026-11-03.json", "PASS: every budget rule holds", True
    )
    _write_freetier(tmp_path, "2026-11-04", result="fail")  # a newer failure is not hidden
    evidence, problems = find_freetier_evidence(tmp_path)
    assert evidence is not None
    assert not evidence.passed
    assert "freetier-ram-2026-11-04.json does not record a pass" in problems[0]


def test_freetier_evidence_problems(tmp_path: Path) -> None:
    """**Validates: Requirements 21.1**"""
    evidence, problems = find_freetier_evidence(tmp_path)
    assert evidence is None
    assert "make freetier-check" in problems[0]
    (tmp_path / "freetier-ram-2026-11-03.md").write_text("# report\n", encoding="utf-8")
    assert find_freetier_evidence(tmp_path)[0] is None  # the markdown alone is not evidence
    (tmp_path / "freetier-ram-2026-11-03.json").write_text("{", encoding="utf-8")
    assert "not readable JSON" in find_freetier_evidence(tmp_path)[1][0]
    (tmp_path / "freetier-ram-2026-11-03.json").write_text("[]", encoding="utf-8")
    evidence, problems = find_freetier_evidence(tmp_path)
    assert evidence is not None
    assert evidence.verdict == "no verdict recorded"
    assert "does not record a pass" in problems[0]


def test_committed_freetier_evidence_passes() -> None:
    evidence, problems = find_freetier_evidence()
    assert problems == []
    assert evidence is not None
    assert evidence.passed


# --- checklist ----------------------------------------------------------------------------

_RAM = RamEvidence("core-ram-2026-10-30.md", "PASS: total peak 0.22 GiB", True, PGVECTOR)


_FREETIER = FreetierEvidence("freetier-ram-2026-11-03.json", "PASS: every budget rule holds", True)


def _render(
    *,
    ran: bool,
    source_problems: tuple[str, ...] = (),
    freetier_problems: tuple[str, ...] = (),
) -> str:
    return render_checklist(
        source_problems=source_problems,
        ram=_RAM,
        ram_problems=(),
        freetier=None if freetier_problems else _FREETIER,
        freetier_problems=freetier_problems,
        ran=ran,
        gates="fmt-check lint",
    )


def test_checklist_lists_every_gate_line_with_its_command() -> None:
    text = _render(ran=True)
    for ident in ("EG1", "EG2", "EG3", "EG4", "EG5"):
        assert f"  {ident}: " in text
    for gate in GATE_LINES:
        for command in gate.local_commands:
            assert command in text
    assert "Passed in this run: gates, freetier-check, s1-smoke, s2-smoke" in text
    assert "core-ram-2026-10-30.md" in text
    assert "make ram-report" in text
    assert "db image: pgvector/pgvector:0.8.6-pg18" in text
    assert "docs/evidence/free-tier/freetier-ram-2026-11-03.json" in text
    eg5 = next(gate for gate in GATE_LINES if gate.ident == "EG5")
    assert eg5.local_commands == ("make freetier-check",)
    assert "proof [passed in this run]: make freetier-check" in text
    assert "freetier-check" in RUN_TARGETS


def test_checklist_marks_all_user_steps_as_not_automated() -> None:
    text = _render(ran=True)
    for ident, _, _ in USER_STEPS:
        assert f"  {ident}  NOT automated" in text
    assert [ident for ident, _, _ in USER_STEPS] == [f"U{n}" for n in range(1, 8)]
    for item in ("V9", "V10", "V11", "S2", "S5"):
        assert f"  {item}  " in text
    assert "U3 and U4" in text


def test_checklist_prints_fu1_to_fu9_after_u7_as_not_automated() -> None:
    """**Validates: Requirements 21.3**"""
    text = _render(ran=True)
    assert [ident for ident, _, _ in FU_STEPS] == [f"FU{n}" for n in range(1, 10)]
    for ident, _, when in FU_STEPS:
        assert f"  {ident}  NOT automated" in text
        assert when.startswith("plan 0.")
    # The development plan items 0.46 to 0.54, in order, one per step.
    plan_items = [when.split()[1].rstrip(",") for _, _, when in FU_STEPS]
    assert plan_items == [f"0.{n}" for n in range(46, 55)]
    positions = [text.index(f"  {ident}  NOT automated") for ident in ("U7", "FU1", "FU9")]
    assert positions == sorted(positions)
    assert text.index("  FU9  NOT automated") < text.index("Verify items and spikes still open")


def test_preview_does_not_claim_results() -> None:
    text = _render(ran=False)
    assert "PREVIEW" in text
    assert "NOT run" in text
    assert "Passed in this run" not in text
    assert "passed in this run" not in text


def test_failed_check_is_shown_and_sets_exit_status(tmp_path: Path) -> None:
    text = _render(ran=True, source_problems=("brainstorm.md changed: x",))
    assert "[FAIL] source documents" in text
    assert "brainstorm.md changed: x" in text

    (tmp_path / "brainstorm.md").write_bytes(BRAIN)
    (tmp_path / "development_plan.md").write_bytes(PLAN)
    (tmp_path / "source-docs.sha256").write_text(_sha_text(), encoding="utf-8")
    _write_ram(tmp_path, "2026-10-30")
    _write_freetier(tmp_path, "2026-11-03")
    status, ok_text = run_checks(
        ran=True, root=tmp_path, evidence_dir=tmp_path, freetier_dir=tmp_path
    )
    assert status == 0
    assert "[ ok ] source documents" in ok_text
    assert "[ ok ] free-tier RAM evidence" in ok_text
    (tmp_path / "development_plan.md").write_bytes(b"edited")
    status, bad_text = run_checks(
        ran=True, root=tmp_path, evidence_dir=tmp_path, freetier_dir=tmp_path
    )
    assert status == 1
    assert "[FAIL] source documents" in bad_text


def test_missing_or_failed_freetier_evidence_fails_the_exit_gate(tmp_path: Path) -> None:
    """**Validates: Requirements 21.1, 21.2**"""
    (tmp_path / "brainstorm.md").write_bytes(BRAIN)
    (tmp_path / "development_plan.md").write_bytes(PLAN)
    (tmp_path / "source-docs.sha256").write_text(_sha_text(), encoding="utf-8")
    _write_ram(tmp_path, "2026-10-30")
    empty = tmp_path / "free-tier"
    empty.mkdir()
    status, text = run_checks(ran=True, root=tmp_path, evidence_dir=tmp_path, freetier_dir=empty)
    assert status == 1
    assert "[FAIL] free-tier RAM evidence (make freetier-check)" in text
    assert "[ ok ] source documents" in text
    _write_freetier(empty, "2026-11-03", result="fail")
    assert run_checks(ran=True, root=tmp_path, evidence_dir=tmp_path, freetier_dir=empty)[0] == 1
    _write_freetier(empty, "2026-11-04")
    assert run_checks(ran=True, root=tmp_path, evidence_dir=tmp_path, freetier_dir=empty)[0] == 0
    # A core report from before the store swap fails the gate although it says pass.
    _write_ram(tmp_path, "2026-11-05", db_reference=EARLIER_IMAGE)
    status, text = run_checks(ran=True, root=tmp_path, evidence_dir=tmp_path, freetier_dir=empty)
    assert status == 1
    assert "[FAIL] RAM report evidence" in text
    assert "not pgvector/pgvector" in text


def test_main_returns_zero_in_preview_on_this_repo(capsys: pytest.CaptureFixture[str]) -> None:
    assert phase0_exit.main(["--preview"]) == 0
    assert "Phase 0 local exit gate" in capsys.readouterr().out


def test_documents_checked_are_the_two_source_documents() -> None:
    assert SOURCE_DOCS == ("brainstorm.md", "development_plan.md")


# --- make target --------------------------------------------------------------------------


@pytest.mark.skipif(shutil.which("make") is None, reason="make is not installed")
def test_make_dry_run_orders_gates_then_smokes_then_the_checklist() -> None:
    result = subprocess.run(
        ["make", "-n", "phase0-exit"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    out = result.stdout
    positions = [
        out.index(marker)
        for marker in ("==> make gates", "cairn-s1 generate", "cairn-s2 run", "phase0_exit.py")
    ]
    assert positions == sorted(positions)
    assert not re.search(r"(^|\s)git\s", out)
