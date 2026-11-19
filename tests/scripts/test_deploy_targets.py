"""The user-run Neon targets of mk/deploy.mk (free-tier retarget, task 9.3).

Requirements 13.5, 13.6 and 17.12: `make env-neon` writes `.env.neon` and then refuses to
overwrite it; `make remote-init` and `make remote-status` fail with a hint, before any docker
call, when `.env.neon` is missing or a `user-provided` key is empty.

Every test runs in a temporary copy of the make fragments with a stub `docker` first on PATH.
The stub only appends its arguments to a log file, so nothing here builds an image, opens a
connection or reads a real `.env.neon`; the repository's own `.env.neon` is never touched.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import pytest

REPO_ROOT: Final = Path(__file__).resolve().parents[2]
SECRET_KEYS: Final = ("CAIRN_OWNER_DB_PASSWORD", "CAIRN_APP_DB_PASSWORD", "CAIRN_DBOS_DB_PASSWORD")
USER_KEYS: Final = ("CAIRN_DB_HOST", "CAIRN_DB_ADMIN_PASSWORD")
FLAG: Final = "--allow-plaintext-password"

# Logs one line per call; `docker image inspect` fails when STUB_IMAGE_MISSING is set.
DOCKER_STUB: Final = """#!/bin/sh
printf '%s\\n' "$*" >> "$STUB_LOG"
if [ "$1 $2" = "image inspect" ] && [ -n "${STUB_IMAGE_MISSING:-}" ]; then
    exit 1
fi
exit 0
"""


@dataclass(frozen=True)
class Sandbox:
    root: Path
    log: Path
    env: dict[str, str]

    @property
    def env_neon(self) -> Path:
        return self.root / ".env.neon"

    def make(self, *args: str, missing_image: bool = False) -> subprocess.CompletedProcess[str]:
        env = dict(self.env)
        if missing_image:
            env["STUB_IMAGE_MISSING"] = "1"
        return subprocess.run(
            ["make", "-C", str(self.root), *args, f"DEV_ENV_PYTHON={sys.executable}"],
            env=env,
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )

    def docker_calls(self) -> list[str]:
        return self.log.read_text(encoding="utf-8").splitlines() if self.log.exists() else []


@pytest.fixture
def sandbox(tmp_path: Path) -> Sandbox:
    root = tmp_path / "repo"
    (root / "scripts").mkdir(parents=True)
    shutil.copytree(REPO_ROOT / "mk", root / "mk")
    shutil.copy(REPO_ROOT / "Makefile", root / "Makefile")
    shutil.copy(REPO_ROOT / "scripts" / "dev_env.py", root / "scripts" / "dev_env.py")
    shutil.copy(REPO_ROOT / ".env.neon.example", root / ".env.neon.example")
    stub_dir = tmp_path / "bin"
    stub_dir.mkdir()
    stub = stub_dir / "docker"
    stub.write_text(DOCKER_STUB, encoding="utf-8")
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
    log = tmp_path / "docker.log"
    env = {
        **os.environ,
        "PATH": f"{stub_dir}{os.pathsep}{os.environ['PATH']}",
        "STUB_LOG": str(log),
    }
    env.pop("STUB_IMAGE_MISSING", None)
    return Sandbox(root, log, env)


def _fill(path: Path, value: str = "filled-by-test") -> None:
    """Give every empty user-provided key in `path` a (fake) value."""
    lines = []
    for line in path.read_text(encoding="utf-8").splitlines():
        key, _, current = line.partition("=")
        lines.append(f"{key}={value}" if key in USER_KEYS and not current else line)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _values(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        key, sep, value = line.partition("=")
        if sep and not line.startswith("#"):
            out[key] = value
    return out


# --- env-neon -------------------------------------------------------------------------------


def test_env_neon_writes_the_file_then_refuses_to_overwrite(sandbox: Sandbox) -> None:
    first = sandbox.make("env-neon")
    assert first.returncode == 0, first.stderr
    assert stat.S_IMODE(sandbox.env_neon.stat().st_mode) == 0o600
    values = _values(sandbox.env_neon)
    assert all(len(values[key]) >= 24 for key in SECRET_KEYS)
    assert all(values[key] == "" for key in USER_KEYS)
    assert values["CAIRN_DB_SSLMODE"] == "verify-full"
    before = sandbox.env_neon.read_bytes()

    second = sandbox.make("env-neon")
    assert second.returncode != 0
    assert "already exists" in second.stderr
    assert sandbox.env_neon.read_bytes() == before
    assert sandbox.docker_calls() == []


def test_env_neon_refuses_an_existing_file_before_running_python(sandbox: Sandbox) -> None:
    sandbox.env_neon.write_text("CAIRN_DB_HOST=keep-me\n", encoding="utf-8")
    result = sandbox.make("env-neon", "DEV_ENV_PYTHON=/nonexistent/python")
    assert result.returncode != 0
    assert "already exists" in result.stderr
    assert sandbox.env_neon.read_text(encoding="utf-8") == "CAIRN_DB_HOST=keep-me\n"


# --- remote-init and remote-status: refusals ------------------------------------------------


@pytest.mark.parametrize("target", ["remote-init", "remote-status"])
def test_missing_env_file_fails_with_a_hint_and_no_docker_call(
    sandbox: Sandbox, target: str
) -> None:
    result = sandbox.make(target)
    assert result.returncode != 0
    assert ".env.neon not found" in result.stderr
    assert "make env-neon" in result.stderr
    assert sandbox.docker_calls() == []


@pytest.mark.parametrize("target", ["remote-init", "remote-status"])
def test_empty_user_provided_keys_fail_naming_the_keys_and_no_docker_call(
    sandbox: Sandbox, target: str
) -> None:
    assert sandbox.make("env-neon").returncode == 0
    secrets = [_values(sandbox.env_neon)[key] for key in SECRET_KEYS]

    result = sandbox.make(target)
    assert result.returncode != 0
    for key in USER_KEYS:
        assert key in result.stderr
    assert "No connection was opened" in result.stderr
    assert sandbox.docker_calls() == []
    assert all(secret not in result.stdout + result.stderr for secret in secrets)


@pytest.mark.parametrize("empty", ["", '""', "''", "  "])
def test_a_quoted_or_blank_value_counts_as_empty(sandbox: Sandbox, empty: str) -> None:
    assert sandbox.make("env-neon").returncode == 0
    _fill(sandbox.env_neon)
    text = sandbox.env_neon.read_text(encoding="utf-8")
    sandbox.env_neon.write_text(
        text.replace("CAIRN_DB_HOST=filled-by-test", f"CAIRN_DB_HOST={empty}"), encoding="utf-8"
    )
    result = sandbox.make("remote-init")
    assert result.returncode != 0
    assert "CAIRN_DB_HOST" in result.stderr
    assert "CAIRN_DB_ADMIN_PASSWORD" not in result.stderr
    assert sandbox.docker_calls() == []


# --- remote-init and remote-status: the docker command --------------------------------------


def test_remote_init_runs_cairn_db_init_with_the_plaintext_flag(sandbox: Sandbox) -> None:
    assert sandbox.make("env-neon").returncode == 0
    _fill(sandbox.env_neon)
    result = sandbox.make("remote-init")
    assert result.returncode == 0, result.stderr
    runs = [call for call in sandbox.docker_calls() if call.startswith("run ")]
    assert runs == [
        f"run --rm --env-file {sandbox.env_neon} cairn-python:local cairn-db init {FLAG}"
    ]
    assert [c for c in sandbox.docker_calls() if c.startswith("build ")] == []


def test_remote_status_runs_cairn_db_status_without_the_flag(sandbox: Sandbox) -> None:
    assert sandbox.make("env-neon").returncode == 0
    _fill(sandbox.env_neon)
    result = sandbox.make("remote-status")
    assert result.returncode == 0, result.stderr
    runs = [call for call in sandbox.docker_calls() if call.startswith("run ")]
    assert runs == [f"run --rm --env-file {sandbox.env_neon} cairn-python:local cairn-db status"]


def test_a_missing_image_is_built_before_the_run(sandbox: Sandbox) -> None:
    assert sandbox.make("env-neon").returncode == 0
    _fill(sandbox.env_neon)
    result = sandbox.make("remote-init", missing_image=True)
    assert result.returncode == 0, result.stderr
    calls = sandbox.docker_calls()
    kinds = [call.split()[0] for call in calls]
    assert kinds == ["image", "build", "run"]
    assert "--build-arg PACKAGE=cairn-core" in calls[1]
    assert "-t cairn-python:local" in calls[1]


def test_dry_run_of_remote_init_shows_the_flag_and_calls_nothing(sandbox: Sandbox) -> None:
    result = sandbox.make("-n", "remote-init")
    assert result.returncode == 0, result.stderr
    run_lines = [line for line in result.stdout.splitlines() if line.startswith("docker run")]
    assert len(run_lines) == 1
    assert f"cairn-db init {FLAG}" in run_lines[0]
    assert "--env-file" in run_lines[0]
    assert sandbox.docker_calls() == []
    assert not sandbox.env_neon.exists()


def test_dry_run_of_remote_status_has_no_plaintext_flag(sandbox: Sandbox) -> None:
    result = sandbox.make("-n", "remote-status")
    assert result.returncode == 0, result.stderr
    assert FLAG not in result.stdout
    assert "cairn-db status" in result.stdout


# --- the repository itself ------------------------------------------------------------------


def test_help_marks_the_neon_targets_as_user_run() -> None:
    result = subprocess.run(
        ["make", "-C", str(REPO_ROOT), "help"],
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    lines = {line.split()[0]: line for line in result.stdout.splitlines() if line.strip()}
    for target in ("env-neon", "remote-init", "remote-status"):
        assert "user-run" in lines[target]
    assert "neon-env-check" not in lines
    assert "neon-image" not in lines


def test_no_gate_target_runs_a_neon_target() -> None:
    result = subprocess.run(
        ["make", "-C", str(REPO_ROOT), "-n", "gates"],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert "env-neon" not in result.stdout
    assert "remote-init" not in result.stdout
    assert "remote-status" not in result.stdout
