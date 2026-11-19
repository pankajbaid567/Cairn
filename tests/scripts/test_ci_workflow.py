"""CI workflow and `make gates`: static checks on ci.yml and mk/gates.mk (Req. 10.1 to 10.6).

The workflow itself is linted by `make actionlint`; these tests pin the properties that
actionlint does not check: SHA-pinned actions, a read-only token, no secrets, and that every
run step goes through a Makefile target.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"

FULL_SHA = re.compile(r"^[0-9a-f]{40}$")
USES_LINE = re.compile(r"^\s*(?:-\s+)?uses:\s*(?P<ref>\S+)(?P<rest>.*)$")
MAKE_TARGET_RULE = re.compile(r"^([a-zA-Z0-9][a-zA-Z0-9_.-]*):(?!=)", re.MULTILINE)

EXPECTED_JOBS = {"python", "web", "lint-config", "security", "integration"}


@pytest.fixture(scope="module")
def workflow() -> dict[str, Any]:
    loaded = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def _triggers(workflow: dict[str, Any]) -> dict[str, Any]:
    # YAML 1.1 reads the bare key `on` as the boolean True.
    as_mapping: dict[Any, Any] = workflow
    raw = as_mapping.get("on", as_mapping.get(True))
    assert isinstance(raw, dict)
    return raw


def _steps(workflow: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    return [(job, step) for job, spec in workflow["jobs"].items() for step in spec["steps"]]


def _make_targets() -> set[str]:
    texts = [(REPO_ROOT / "Makefile").read_text(encoding="utf-8")]
    texts += [p.read_text(encoding="utf-8") for p in sorted((REPO_ROOT / "mk").glob("*.mk"))]
    return {m for text in texts for m in MAKE_TARGET_RULE.findall(text)}


def test_triggers_are_push_pull_request_nightly_schedule_and_manual(
    workflow: dict[str, Any],
) -> None:
    triggers = _triggers(workflow)
    assert {"push", "pull_request", "schedule", "workflow_dispatch"} <= set(triggers)
    assert "pull_request_target" not in triggers
    assert triggers["schedule"] == [{"cron": "0 3 * * *"}]


def test_token_is_read_only_everywhere(workflow: dict[str, Any]) -> None:
    assert workflow["permissions"] == {"contents": "read"}
    for name, spec in workflow["jobs"].items():
        assert "permissions" not in spec, f"job {name} must not widen the token"


def test_runs_cancel_superseded_runs_and_every_job_has_a_timeout(workflow: dict[str, Any]) -> None:
    assert workflow["concurrency"]["cancel-in-progress"] is True
    assert "github.ref" in workflow["concurrency"]["group"]
    assert set(workflow["jobs"]) == EXPECTED_JOBS
    for name, spec in workflow["jobs"].items():
        assert spec["runs-on"] == "ubuntu-24.04", name
        assert isinstance(spec["timeout-minutes"], int), name


def test_every_action_is_pinned_by_full_commit_sha_with_a_version_comment() -> None:
    found = 0
    for line in WORKFLOW.read_text(encoding="utf-8").splitlines():
        match = USES_LINE.match(line)
        if match is None:
            continue
        found += 1
        action, _, ref = match["ref"].partition("@")
        assert "/" in action, line
        assert not action.startswith("./"), line
        assert FULL_SHA.match(ref), f"not pinned by full commit SHA: {line.strip()}"
        assert re.search(r"#\s*v\d+\.\d+\.\d+\s*$", match["rest"]), (
            f"missing version comment: {line.strip()}"
        )
    assert found >= 5


def test_every_run_step_calls_a_make_target(workflow: dict[str, Any]) -> None:
    targets = _make_targets()
    runs = [(job, step["run"]) for job, step in _steps(workflow) if "run" in step]
    assert runs
    for job, command in runs:
        assert "\n" not in command.strip(), f"{job}: one make call per step: {command!r}"
        # `make <target>... [VAR=value]... [> file]`: the redirect is for the log artifact only.
        words = command.split(">")[0].split()
        assert words[0] == "make", f"{job}: {command!r}"
        named = [w for w in words[1:] if "=" not in w]
        assert named, f"{job}: no target in {command!r}"
        for target in named:
            assert target in targets, f"{job}: unknown make target {target!r}"


def test_workflow_references_no_secrets() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "secrets." not in text
    assert "GITHUB_TOKEN" not in text


def test_host_toolchain_for_web_and_the_gates_that_need_it(workflow: dict[str, Any]) -> None:
    assert workflow["env"]["WEB_RUNNER"] == "host"
    runs = {
        job: [s["run"] for s in spec["steps"] if "run" in s]
        for job, spec in workflow["jobs"].items()
    }
    assert runs["web"] == ["make web-check"]
    for job in ("python", "web", "security"):
        uses = [s["uses"].split("@")[0] for s in workflow["jobs"][job]["steps"] if "uses" in s]
        assert "pnpm/action-setup" in uses, job
        assert "actions/setup-node" in uses, job


def test_gate_coverage_and_order_in_ci(workflow: dict[str, Any]) -> None:
    runs = {
        job: [s["run"] for s in spec["steps"] if "run" in s]
        for job, spec in workflow["jobs"].items()
    }
    assert runs["python"] == [
        "make fmt-check",
        "make lint",
        "make types",
        "make test-unit",
        "make scenario-validate",
        "make licenses",
        "make docs-check",
    ]
    assert runs["lint-config"] == ["make compose-check", "make actionlint"]
    assert runs["security"] == ["make audit", "make secrets", "make secrets-history"]
    integration = workflow["jobs"]["integration"]
    assert integration["needs"] == "python"
    assert runs["integration"][:9] == [
        "make env",
        "make up-db",
        "make test-integration",
        "make matrix",
        "make migrations-check",
        "make eval-smoke",
        "make smoke",
        "make down",
        "make freetier-check",
    ]
    failure_steps = [s for s in integration["steps"] if s.get("if") == "failure()"]
    assert any(s.get("uses", "").startswith("actions/upload-artifact@") for s in failure_steps)


def test_freetier_gate_runs_after_the_core_stack_is_down_and_logs_are_kept(
    workflow: dict[str, Any],
) -> None:
    """**Validates: Requirements 11.7, 11.8**"""
    integration = workflow["jobs"]["integration"]
    runs = [s["run"] for s in integration["steps"] if "run" in s]
    # The core and freetier profiles never share the Docker VM: down sits between them.
    assert runs.index("make smoke") < runs.index("make down") < runs.index("make freetier-check")
    on_failure = [s for s in integration["steps"] if s.get("if") == "failure()"]
    collected = [s["run"] for s in on_failure if "run" in s]
    assert any(c.startswith("make freetier-logs") and "freetier-logs.txt" in c for c in collected)
    upload = next(s for s in on_failure if s.get("uses", "").startswith("actions/upload-artifact@"))
    assert "freetier-logs.txt" in upload["with"]["path"]
    assert "compose-logs.txt" in upload["with"]["path"]
    # The log step runs after the gate it reports on.
    names = [s.get("name") for s in integration["steps"]]
    assert names.index("Free-tier gate") < names.index("Collect free-tier logs")
    assert "freetier-logs" in _make_targets()


def test_live_workers_ai_check_is_in_no_gate_and_no_workflow() -> None:
    """**Validates: Requirements 16.11**

    The target lives in mk/providers.mk (task 8.13) and is user-run; this assertion keeps it out
    of the gate lists and CI. tests/scripts/test_live_check.py covers the rest.
    """
    assert "workers-ai-live-check" not in WORKFLOW.read_text(encoding="utf-8")
    gates_mk = (REPO_ROOT / "mk" / "gates.mk").read_text(encoding="utf-8")
    gate_lists = re.findall(r"^GATES_\w+\s*:?=.*$", gates_mk, re.MULTILINE)
    assert gate_lists
    assert all("workers-ai-live-check" not in line for line in gate_lists)


def test_security_job_scans_full_history_with_the_same_gitleaks_image(
    workflow: dict[str, Any],
) -> None:
    checkout = next(
        s
        for s in workflow["jobs"]["security"]["steps"]
        if s.get("uses", "").startswith("actions/checkout@")
    )
    assert checkout["with"]["fetch-depth"] == 0
    security_mk = (REPO_ROOT / "mk" / "security.mk").read_text(encoding="utf-8")
    recipe = security_mk.split("secrets-history:", 1)[1].split("\nlicenses:", 1)[0]
    assert " git /repo" in recipe
    assert "$(GITLEAKS_IMAGE)" in recipe


def test_toolchain_versions_match_the_repo_pins(workflow: dict[str, Any]) -> None:
    steps = [step for _, step in _steps(workflow) if "uses" in step]

    def with_value(action: str, key: str) -> set[str]:
        return {str(s["with"][key]) for s in steps if s["uses"].startswith(action + "@")}

    package = json.loads((REPO_ROOT / "apps" / "web" / "package.json").read_text(encoding="utf-8"))
    assert with_value("pnpm/action-setup", "version") == {
        package["packageManager"].removeprefix("pnpm@")
    }

    web_mk = (REPO_ROOT / "mk" / "web.mk").read_text(encoding="utf-8")
    node_image = re.search(r"^NODE_IMAGE \?= node:(\d+\.\d+\.\d+)-", web_mk, re.MULTILINE)
    assert node_image is not None
    assert with_value("actions/setup-node", "node-version") == {node_image[1]}

    pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    required = re.search(r'required-version\s*=\s*"==([\d.]+)"', pyproject)
    assert required is not None
    assert with_value("astral-sh/setup-uv", "version") == {required[1]}


@pytest.mark.skipif(shutil.which("make") is None, reason="make is not installed")
def test_make_gates_dry_run_lists_every_gate_in_order() -> None:
    result = subprocess.run(
        ["make", "-n", "gates"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    order = re.findall(r"^==> make (\S+)$", result.stdout, re.MULTILINE)
    assert order == [
        "fmt-check",
        "lint",
        "types",
        "test-unit",
        "web-check",
        "scenario-validate",
        "compose-check",
        "actionlint",
        "licenses",
        "docs-check",
        "audit",
        "secrets",
        "up-db",
        "test-integration",
        "matrix",
        "migrations-check",
        "eval-smoke",
        "smoke",
        "down",
        "freetier-check",
        # `make -n` still runs the recursive $(MAKE) lines, so the seven steps inside
        # freetier-check are listed too.
        "freetier-up",
        "freetier-seed",
        "freetier-phase1",
        "freetier-smoke",
        "matrix-managed",
        "freetier-ram",
        "freetier-down",
    ]
    assert order.index("down") < order.index("freetier-check")
    assert order.index("compose-check") < order.index("up-db")
    assert order.index("licenses") < order.index("docs-check")
    assert "phase0-exit" not in result.stdout
