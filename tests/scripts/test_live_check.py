"""``make workers-ai-live-check``: user-run, in no gate and not in CI (Requirements 16.10, 16.11).

The target spends the user's Workers AI quota with the user's token (FU8). These tests read the
make fragments and the workflow, run ``make -n`` (nothing executes), and run the live test
module once with the switch off. None of them calls Cloudflare.

**Validates: Requirements 16.10, 16.11**
"""

import os
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
MK_DIR = REPO_ROOT / "mk"
PROVIDERS_MK = MK_DIR / "providers.mk"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
LIVE_TESTS = REPO_ROOT / "packages" / "retrieval" / "tests" / "live"
TARGET = "workers-ai-live-check"
SWITCH = "CAIRN_LIVE_CHECK=1"
MK_IMAGE_VARIABLE = re.compile(r"^[A-Z][A-Z0-9_]*_IMAGE\s*[?:]*=", re.MULTILINE)


def _make_dry_run(*targets: str) -> str:
    done = subprocess.run(
        ["make", "-n", "--no-print-directory", *targets],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    return done.stdout


def test_target_runs_the_live_marker_with_the_switch_set() -> None:
    text = PROVIDERS_MK.read_text(encoding="utf-8")
    assert f".PHONY: {TARGET}" in text
    recipe = _make_dry_run(TARGET)
    assert SWITCH in recipe
    assert "pytest -m live packages/retrieval/tests/live" in recipe


def test_help_text_says_user_run() -> None:
    line = next(
        ln for ln in PROVIDERS_MK.read_text(encoding="utf-8").splitlines() if ln.startswith(TARGET)
    )
    assert "## User-run" in line


def test_only_providers_mk_sets_the_live_switch() -> None:
    makefiles = [REPO_ROOT / "Makefile", *sorted(MK_DIR.glob("*.mk"))]
    # providers.mk also names the variable in its header comment, so look at recipe lines only.
    setting = [
        p.name
        for p in makefiles
        for line in p.read_text(encoding="utf-8").splitlines()
        if line.startswith("\t") and SWITCH in line
    ]
    assert setting == ["providers.mk"]


def test_the_target_is_in_no_gate_list_and_no_gate_run() -> None:
    gates_mk = (MK_DIR / "gates.mk").read_text(encoding="utf-8")
    gate_lists = re.findall(r"^GATES_\w+\s*:?=.*$", gates_mk, re.MULTILINE)
    assert gate_lists
    assert all(TARGET not in line for line in gate_lists)
    plan = _make_dry_run("gates")
    assert TARGET not in plan
    assert "CAIRN_LIVE_CHECK" not in plan
    assert "-m live" not in plan.replace("not live", "")


def test_unit_and_integration_targets_exclude_the_live_marker() -> None:
    assert "not live" in _make_dry_run("test-unit")
    assert "not live" in _make_dry_run("test-integration")


def test_the_ci_workflow_neither_runs_the_target_nor_sets_the_switch() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    assert TARGET not in text
    assert "CAIRN_LIVE_CHECK" not in text
    assert "CLOUDFLARE_API_TOKEN" not in text


def test_providers_mk_defines_no_variable_the_license_scanner_reads_as_an_image() -> None:
    assert MK_IMAGE_VARIABLE.search(PROVIDERS_MK.read_text(encoding="utf-8")) is None


def _run_live_module(env: dict[str, str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-c",
            str(REPO_ROOT / "pyproject.toml"),
            "--rootdir",
            str(REPO_ROOT),
            "-p",
            "no:cacheprovider",
            "-rs",
            str(LIVE_TESTS),
        ],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def _clean_env() -> dict[str, str]:
    drop = ("CAIRN_LIVE_CHECK", "CLOUDFLARE_ACCOUNT_ID", "CLOUDFLARE_API_TOKEN")
    return {k: v for k, v in os.environ.items() if k not in drop}


def test_live_tests_skip_with_a_how_to_message_when_the_switch_is_off(tmp_path: Path) -> None:
    done = _run_live_module(_clean_env(), tmp_path)
    assert done.returncode == 0, done.stdout[-2000:]
    assert "3 skipped" in done.stdout
    assert "make workers-ai-live-check" in done.stdout
    assert "CLOUDFLARE_API_TOKEN" in done.stdout


def test_live_tests_skip_when_the_switch_is_on_but_the_token_is_missing(tmp_path: Path) -> None:
    # tmp_path has no .env and the environment has no token, so no request can be made.
    done = _run_live_module({**_clean_env(), "CAIRN_LIVE_CHECK": "1"}, tmp_path)
    assert done.returncode == 0, done.stdout[-2000:]
    assert "3 skipped" in done.stdout
    assert "make workers-ai-live-check" in done.stdout
