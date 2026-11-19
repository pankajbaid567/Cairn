"""Spike host guard (free-tier retarget FT-28, Requirement 3.8): ``is_local_host``,
``host_allowed`` and ``cairn-s2 run``. ``cairn-s1 load`` is covered in ``test_s1.py``.
No database is needed: a connection attempt fails the test."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import cairn_core.testing
from cairn_eval.spikes.host_guard import host_allowed, is_local_host
from cairn_eval.spikes.s2_rls_explain.cli import main as s2_main

REMOTE = "ep-quiet-forest-123456.ap-southeast-1.aws.neon.tech"


@pytest.mark.parametrize(
    "host",
    ["127.0.0.1", "127.255.0.9", "::1", "[::1]", "localhost", "LocalHost", "db", "ft-db", "DB"],
)
def test_loopback_addresses_localhost_and_compose_services_are_local(host: str) -> None:
    assert is_local_host(host)
    assert host_allowed(host, allow_remote=False)


@pytest.mark.parametrize(
    "host",
    [
        REMOTE,
        "10.0.0.5",
        "192.168.1.20",
        "128.0.0.1",
        "::2",
        "[2001:db8::1]",
        "",
        " localhost",
        "localhost.",
        "db.example.com",
        "ft-db2",
        "127.0.0.1,ep-x.neon.tech",
        "/var/run/postgresql",
    ],
)
def test_every_other_host_is_refused_unless_allow_remote(host: str) -> None:
    assert not is_local_host(host)
    assert not host_allowed(host, allow_remote=False)
    assert host_allowed(host, allow_remote=True)


class ConnectRecorder:
    """Stands in for ``cairn_core.testing.connect_admin``: records the call and stops the
    command there, as an unreachable database would (exit 2)."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, *_args: Any, **_kwargs: Any) -> Any:
        self.calls += 1
        raise cairn_core.testing.DatabaseUnavailable("stopped by the test before connecting")


@pytest.fixture
def remote_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[ConnectRecorder, Path]:
    """Settings that point the spike commands at a remote host, without a ``.env``."""
    monkeypatch.chdir(tmp_path)
    for key, value in {
        "CAIRN_TEST_ADMIN_HOST": REMOTE,
        "CAIRN_TEST_ADMIN_PORT": "5432",
        "POSTGRES_PASSWORD": "test-only-superuser",
        "CAIRN_OWNER_DB_PASSWORD": "test-only-owner",
        "CAIRN_APP_DB_PASSWORD": "test-only-app",
        "CAIRN_DBOS_DB_PASSWORD": "test-only-dbos",
    }.items():
        monkeypatch.setenv(key, value)
    recorder = ConnectRecorder()
    monkeypatch.setattr(cairn_core.testing, "connect_admin", recorder)
    return recorder, tmp_path


def test_s2_run_refuses_a_remote_host_with_status_2_before_connecting(
    remote_env: tuple[ConnectRecorder, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    recorder, tmp_path = remote_env
    assert s2_main(["run", "--out", str(tmp_path / "out")]) == 2
    assert recorder.calls == 0
    err = capsys.readouterr().err
    assert "refusing database host" in err
    assert REMOTE in err
    assert "--allow-remote" in err
    assert "test-only" not in err
    assert not (tmp_path / "out").exists()


def test_s2_run_with_allow_remote_goes_on_to_connect(
    remote_env: tuple[ConnectRecorder, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    recorder, tmp_path = remote_env
    assert s2_main(["run", "--out", str(tmp_path / "out"), "--allow-remote"]) == 2
    assert recorder.calls == 1
    assert "refusing database host" not in capsys.readouterr().err


def test_s2_run_accepts_the_compose_service_name(
    remote_env: tuple[ConnectRecorder, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder, tmp_path = remote_env
    monkeypatch.setenv("CAIRN_TEST_ADMIN_HOST", "db")
    assert s2_main(["run", "--out", str(tmp_path / "out")]) == 2
    assert recorder.calls == 1
