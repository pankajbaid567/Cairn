"""Phase 0 exit-gate checks and checklist (``make phase0-exit``; design §10, §18; Req. 23).

``make phase0-exit`` runs ``gates``, ``s1-smoke`` and ``s2-smoke`` from ``mk/exit.mk`` and then
calls this script, which does the parts that are not a make target:

1. Checks that ``brainstorm.md`` and ``development_plan.md`` still hash to the values in
   ``docs/evidence/phase-0/source-docs.sha256`` (Requirement 1.5). The script only reads; the
   two documents and the hash file are never written.
2. Checks that a RAM report from ``make ram-report`` is on disk, passed (Requirement 20) and
   names the ``pgvector/pgvector`` image for the ``db`` service (free-tier Requirement 21.2).
3. Checks that the newest ``docs/evidence/free-tier/freetier-ram-<date>.json`` from
   ``make freetier-check`` is on disk and records a pass (free-tier Requirement 21.1).
4. Prints the exit-gate checklist: EG1 to EG5 with the command that proves each line, the
   user-owned steps U1 to U7 and FU1 to FU9 (never automated), and the Verify items and spikes
   still open.

``--preview`` runs the same checks and printout without claiming that ``gates`` and the smoke
runs passed; ``make phase0-exit-preview`` uses it, so the checklist can be read on a machine that
is busy or has no Docker. No git command is run. Standard library only.

Exit codes: 0 when every check passes, 1 when any fails.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
EVIDENCE_DIR = REPO_ROOT / "docs" / "evidence" / "phase-0"
FREETIER_EVIDENCE_DIR = REPO_ROOT / "docs" / "evidence" / "free-tier"
SOURCE_DOCS_HASH_FILE = EVIDENCE_DIR / "source-docs.sha256"
SOURCE_DOCS = ("brainstorm.md", "development_plan.md")
RAM_REPORT_PATTERN = re.compile(r"^core-ram-(\d{4}-\d{2}-\d{2})\.json$")
FREETIER_RAM_PATTERN = re.compile(r"^freetier-ram-(\d{4}-\d{2}-\d{2})\.json$")
DB_IMAGE_PREFIX = "pgvector/pgvector"
# freetier-check is the last gate of `gates` (mk/gates.mk), so it passed when `gates` did.
RUN_TARGETS = ("gates", "freetier-check", "s1-smoke", "s2-smoke")

_SHA_LINE = re.compile(r"^(?P<digest>[0-9a-fA-F]{64}) [ *](?P<name>\S.*)$")


# --- Source documents (Requirement 1.5) ----------------------------------------------------


def sha256_of(path: Path) -> str | None:
    """Hex digest of the file, or ``None`` when it cannot be read."""
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def parse_sha_file(text: str) -> tuple[dict[str, str], list[str]]:
    """Parse ``sha256sum`` output into ``{name: digest}`` and a list of problems.

    Blank lines are skipped. A line that is not ``<64 hex digits>  <name>`` is a problem, and so
    is a name that appears twice.
    """
    entries: dict[str, str] = {}
    problems: list[str] = []
    for number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        match = _SHA_LINE.match(line)
        if match is None:
            problems.append(f"line {number} is not '<sha256>  <name>'")
            continue
        name = match["name"]
        if name in entries:
            problems.append(f"{name} is listed twice")
            continue
        entries[name] = match["digest"].lower()
    return entries, problems


def verify_source_docs(
    sha_text: str | None,
    actual: Mapping[str, str | None],
    required: Sequence[str] = SOURCE_DOCS,
) -> list[str]:
    """Problems found comparing the hash file's text with the documents' actual digests.

    ``sha_text`` is ``None`` when the hash file is missing; ``actual`` maps each document name
    to its digest, or ``None`` when the document is missing. An empty result means every
    required document is listed and unchanged.
    """
    if sha_text is None:
        return ["docs/evidence/phase-0/source-docs.sha256 is missing"]
    entries, problems = parse_sha_file(sha_text)
    for name in required:
        expected = entries.get(name)
        digest = actual.get(name)
        if expected is None:
            problems.append(f"{name} has no entry in source-docs.sha256")
        elif digest is None:
            problems.append(f"{name} is missing from the repo root")
        elif digest != expected:
            problems.append(
                f"{name} changed: sha256 is {digest[:12]}..., the record has {expected[:12]}..."
            )
    return problems


def check_source_docs(root: Path = REPO_ROOT, hash_file: Path = SOURCE_DOCS_HASH_FILE) -> list[str]:
    """Read the hash file and the two documents under ``root`` and compare them."""
    try:
        sha_text: str | None = hash_file.read_text(encoding="utf-8")
    except OSError:
        sha_text = None
    actual = {name: sha256_of(root / name) for name in SOURCE_DOCS}
    return verify_source_docs(sha_text, actual)


# --- RAM report evidence (Requirement 20) ---------------------------------------------------


@dataclass(frozen=True)
class RamEvidence:
    """The newest ``core-ram-<date>`` report on disk and its verdict."""

    name: str
    verdict: str
    passed: bool
    db_image: str | None = None


def db_image_reference(data: object) -> str | None:
    """The image reference of the ``db`` container in a RAM report's ``images`` list.

    The compose project names it ``<project>-db-<n>`` (for example ``cairn-db-1``).
    """
    images = data.get("images") if isinstance(data, dict) else None
    if not isinstance(images, list):
        return None
    for image in images:
        if not isinstance(image, dict):
            continue
        container, reference = image.get("container"), image.get("reference")
        if (
            isinstance(container, str)
            and isinstance(reference, str)
            and re.search(r"(?:^|[-_])db(?:[-_]\d+)?$", container)
        ):
            return reference
    return None


def find_ram_evidence(evidence_dir: Path = EVIDENCE_DIR) -> tuple[RamEvidence | None, list[str]]:
    """The newest RAM report (by the date in its name) and the problems with the evidence.

    Both ``core-ram-<date>.json`` and ``.md`` must exist, the JSON must say ``pass``, and the
    ``db`` service must have run the ``pgvector/pgvector`` image (a report from before the
    store swap names the earlier image and has to be made again with ``make ram-report``).
    """
    dated = sorted(
        (m[1], path)
        for path in evidence_dir.glob("core-ram-*.json")
        if (m := RAM_REPORT_PATTERN.match(path.name))
    )
    if not dated:
        return None, ["no core-ram-<date>.json in docs/evidence/phase-0 (run make ram-report)"]
    date, json_path = dated[-1]
    markdown = f"core-ram-{date}.md"
    problems: list[str] = []
    if not (evidence_dir / markdown).is_file():
        problems.append(f"{markdown} is missing next to {json_path.name}")
    try:
        data = json.loads(json_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None, [*problems, f"{json_path.name} is not readable JSON"]
    result = data.get("result") if isinstance(data, dict) else None
    verdict = data.get("verdict") if isinstance(data, dict) else None
    verdict_text = verdict if isinstance(verdict, str) else "no verdict recorded"
    passed = result == "pass"
    if not passed:
        problems.append(f"{json_path.name} does not record a pass ({verdict_text})")
    db_image = db_image_reference(data)
    if db_image is None:
        problems.append(f"{json_path.name} names no db image (run make ram-report)")
    elif not db_image.startswith(DB_IMAGE_PREFIX):
        problems.append(
            f"{json_path.name} db image is {db_image.split('@')[0]}, not {DB_IMAGE_PREFIX} "
            "(run make ram-report)"
        )
    return RamEvidence(markdown, verdict_text, passed, db_image), problems


# --- Free-tier RAM evidence (free-tier Requirement 21.1) -------------------------------------
@dataclass(frozen=True)
class FreetierEvidence:
    """The newest ``freetier-ram-<date>`` report on disk and its verdict."""

    name: str
    verdict: str
    passed: bool


def find_freetier_evidence(
    evidence_dir: Path = FREETIER_EVIDENCE_DIR,
) -> tuple[FreetierEvidence | None, list[str]]:
    """The newest ``freetier-ram-<date>.json`` (by the date in its name) and its problems.

    The file is written by ``make freetier-ram`` inside ``make freetier-check``, also when a
    budget is missed, so its ``result`` must be ``pass``.
    """
    dated = sorted(
        (m[1], path)
        for path in evidence_dir.glob("freetier-ram-*.json")
        if (m := FREETIER_RAM_PATTERN.match(path.name))
    )
    if not dated:
        return None, [
            "no freetier-ram-<date>.json in docs/evidence/free-tier (run make freetier-check)"
        ]
    _, json_path = dated[-1]
    try:
        data = json.loads(json_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None, [f"{json_path.name} is not readable JSON"]
    result = data.get("result") if isinstance(data, dict) else None
    verdict = data.get("verdict") if isinstance(data, dict) else None
    verdict_text = verdict if isinstance(verdict, str) else "no verdict recorded"
    if result != "pass":
        return FreetierEvidence(json_path.name, verdict_text, False), [
            f"{json_path.name} does not record a pass ({verdict_text})"
        ]
    return FreetierEvidence(json_path.name, verdict_text, True), []


# --- Checklist ------------------------------------------------------------------------------


@dataclass(frozen=True)
class GateLine:
    """One line of the plan's Phase 0 exit gate."""

    ident: str
    text: str
    local_commands: tuple[str, ...]
    open_steps: str


GATE_LINES = (
    GateLine(
        "EG1",
        "CI green on main, and ADR-0001 to ADR-0005 merged",
        ("make gates", "make docs-check"),
        "U4: commit, create the repo, push, confirm CI is green on main, merge the ADRs. "
        "Locally, make gates runs the same targets as .github/workflows/ci.yml",
    ),
    GateLine(
        "EG2",
        "Scenario YAML v1 passes the validator, and it rejects all three broken fixtures",
        ("make scenario-validate", "make test-unit"),
        "none",
    ),
    GateLine(
        "EG3",
        "/readyz healthy in compose core, and the role test passes",
        ("make smoke", "make matrix"),
        "none",
    ),
    GateLine(
        "EG4",
        "S1 and S2 harnesses committed, with S2 results due 30 Oct",
        ("make s1-smoke", "make s2-smoke"),
        "U4 for committing the harnesses; S2 results: see the open spikes below",
    ),
    GateLine(
        "EG5",
        "Free-tier emulation: smoke, managed-mode matrix and the combined-process RAM budget",
        ("make freetier-check",),
        "FU1 to FU6 for the hosted deploy (not part of the gate); locally, make gates ends "
        "with freetier-check",
    ),
)

USER_STEPS = (
    ("U1", "Docker Desktop memory at least 4 GB (Verify V10)", "before make up"),
    ("U2", "git init -b main, then uv run pre-commit install", "after the skeleton exists"),
    (
        "U3",
        "0.10: demo GitHub org, private repos and teams on its plan, record in "
        "docs/evidence/phase-0/github-org.md (Verify V11)",
        "OPEN, by 29 Oct",
    ),
    (
        "U4",
        "commit, create the GitHub repo, push, CI green on main, merge ADR-0001 to ADR-0005",
        "OPEN, end of Phase 0",
    ),
    ("U5", "tick plan items and add progress-log rows in development_plan.md", "each working day"),
    (
        "U6",
        "choose the repo license (recommendation: Apache-2.0) before the first push",
        "before U4",
    ),
    ("U7", "optional: Node 24 LTS on the host for pnpm dev and WEB_RUNNER=host", "any time"),
)

# Free-tier user steps (design §19; development plan items 0.46 to 0.54). Never automated.
FU_STEPS = (
    (
        "FU1",
        "Neon account and a PostgreSQL 18 project; make env-neon; endpoint host and "
        "neondb_owner password into .env.neon",
        "plan 0.46",
    ),
    ("FU2", "make remote-init and make remote-status from the laptop", "plan 0.47"),
    (
        "FU3",
        "Cloudflare account, account id, and an API token scoped to Workers AI only",
        "plan 0.48",
    ),
    (
        "FU4",
        "Render Blueprint from render.yaml and its environment entries (after U4)",
        "plan 0.49",
    ),
    ("FU5", "first Render deploy; confirm /healthz", "plan 0.50"),
    (
        "FU6",
        "Pages project, API_ORIGIN, make web-dist, pinned wrangler deploy, "
        "CAIRN_PROXY_TOKEN Pages secret",
        "plan 0.51",
    ),
    ("FU7", "docker volume rm cairn_pgdata once satisfied with the new store", "plan 0.52"),
    ("FU8", "optional: Workers AI token in .env, then make workers-ai-live-check", "plan 0.53"),
    ("FU9", "tick plan items and add progress-log rows", "plan 0.54, each working day"),
)
OPEN_ITEMS = (
    (
        "V9",
        "action SHAs are recorded in docs/evidence/phase-0/verify/V9.md; how the workflow runs "
        "on a GitHub runner is unverified until U4",
    ),
    ("V10", "Docker Desktop VM memory allocation on the laptop (U1)"),
    ("V11", "private repos and teams on the demo org's plan (U3)"),
    (
        "S2",
        "the real run and ADR-0006 (plan 1.1) are due 30 Oct; s2-smoke only proves the harness",
    ),
    ("S5", "GitHub permission events spike, Phase 3 on 7 Nov (ADR-0010); not started"),
)


def render_checklist(
    *,
    source_problems: Sequence[str],
    ram: RamEvidence | None,
    ram_problems: Sequence[str],
    freetier: FreetierEvidence | None,
    freetier_problems: Sequence[str],
    ran: bool,
    gates: str = "",
) -> str:
    """The exit-gate printout.

    ``ran`` is true when ``gates``, ``s1-smoke`` and ``s2-smoke`` passed in this invocation of
    ``make phase0-exit``, false for ``--preview``.
    """
    lines: list[str] = ["Phase 0 local exit gate", "======================="]
    if ran:
        lines.append(f"Passed in this run: {', '.join(RUN_TARGETS)}")
        if gates:
            lines.append(f"  gates covered: {gates}")
    else:
        lines.append(
            f"PREVIEW: {', '.join(RUN_TARGETS)} were NOT run by this command. "
            "The lines below name the proof; they are not results."
        )
    lines += ["", "Checks made by this command"]
    if source_problems:
        lines.append("  [FAIL] source documents (Requirement 1.5)")
        lines += [f"         {problem}" for problem in source_problems]
    else:
        lines.append(
            "  [ ok ] source documents: brainstorm.md and development_plan.md match "
            "docs/evidence/phase-0/source-docs.sha256"
        )
    if ram_problems:
        lines.append("  [FAIL] RAM report evidence (make ram-report)")
        lines += [f"         {problem}" for problem in ram_problems]
    elif ram is not None:
        lines.append(f"  [ ok ] RAM report evidence: docs/evidence/phase-0/{ram.name}")
        lines.append(f"         {ram.verdict}")
        lines.append(f"         db image: {(ram.db_image or '').split('@')[0]}")
        lines.append("         proof: make ram-report (not part of gates; about 2.5 minutes)")
    if freetier_problems:
        lines.append("  [FAIL] free-tier RAM evidence (make freetier-check)")
        lines += [f"         {problem}" for problem in freetier_problems]
    elif freetier is not None:
        lines.append(f"  [ ok ] free-tier RAM evidence: docs/evidence/free-tier/{freetier.name}")
        lines.append(f"         {freetier.verdict}")
        lines.append("         proof: make freetier-check (the last gate of make gates)")
    lines += ["", "Exit-gate lines"]
    status = "passed in this run" if ran else "preview, not run"
    for gate in GATE_LINES:
        lines.append(f"  {gate.ident}: {gate.text}")
        lines.append(f"      proof [{status}]: {'; '.join(gate.local_commands)}")
        lines.append(f"      still open: {gate.open_steps}")
    lines += ["", "User-owned steps (NOT automated; this command runs no git command)"]
    lines += [
        f"  {ident}  NOT automated  {text} [{when}]"
        for ident, text, when in (*USER_STEPS, *FU_STEPS)
    ]
    lines += ["", "Verify items and spikes still open"]
    lines += [f"  {ident}  {text}" for ident, text in OPEN_ITEMS]
    lines += [
        "",
        "Remaining for the user after this command: U3 and U4; FU1 to FU6 (Neon, Cloudflare, "
        "Render, Pages, first deploy, needed by the Phase 1 demo and by Phase 6 at the latest) "
        "and FU7; optional FU8; plus U5 and FU9 for ticking the plan.",
    ]
    return "\n".join(lines)


def run_checks(
    *,
    ran: bool,
    gates: str = "",
    root: Path = REPO_ROOT,
    evidence_dir: Path = EVIDENCE_DIR,
    freetier_dir: Path = FREETIER_EVIDENCE_DIR,
) -> tuple[int, str]:
    """Run the three checks; return the exit status and the printout."""
    source_problems = check_source_docs(root, evidence_dir / "source-docs.sha256")
    ram, ram_problems = find_ram_evidence(evidence_dir)
    freetier, freetier_problems = find_freetier_evidence(freetier_dir)
    text = render_checklist(
        source_problems=source_problems,
        ram=ram,
        ram_problems=ram_problems,
        freetier=freetier,
        freetier_problems=freetier_problems,
        ran=ran,
        gates=gates,
    )
    failed = source_problems or ram_problems or freetier_problems
    return (1 if failed else 0), text


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Phase 0 exit-gate checks and checklist.")
    parser.add_argument(
        "--preview",
        action="store_true",
        help="print the checklist without claiming that gates and the smoke runs passed",
    )
    parser.add_argument("--gates", default="", help="the gate targets that make gates ran")
    args = parser.parse_args(argv)
    status, text = run_checks(ran=not args.preview, gates=args.gates)
    print(text)
    if status:
        print("\nphase0-exit: FAILED (see [FAIL] above)", file=sys.stderr)
    return status


if __name__ == "__main__":
    sys.exit(main())
