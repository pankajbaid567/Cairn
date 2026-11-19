"""Unit tests for the liveness thread and the healthcheck (Requirements 8.5 and 8.6)."""

from __future__ import annotations

import logging
import os
import socket
import threading
import time
from pathlib import Path

import psycopg
import pytest

from cairn_core.config import CommonSettings, WorkerSettings, load_settings
from cairn_worker.app import LivenessThread, make_db_probe
from cairn_worker.healthcheck import MAX_AGE_S, is_fresh, main, read_stamp

NOW = 1_800_000_000.0


# --- is_fresh: a pure function of (now, stamp, max age) ---------------------------------------


@pytest.mark.parametrize(
    ("age_s", "expected"),
    [
        (0.0, True),
        (9.9, True),
        (29.999, True),
        (30.0, False),  # "younger than 30 seconds" is strict
        (30.001, False),
        (3600.0, False),
        (-5.0, True),  # a stamp slightly in the future (clock skew)
    ],
)
def test_is_fresh_at_fixed_ages(age_s: float, expected: bool) -> None:
    assert MAX_AGE_S == 30.0
    assert is_fresh(NOW, NOW - age_s) is expected


def test_is_fresh_is_false_for_a_missing_file() -> None:
    assert is_fresh(NOW, None) is False


def test_is_fresh_honours_an_explicit_threshold() -> None:
    assert is_fresh(NOW, NOW - 4.0, 5.0)
    assert not is_fresh(NOW, NOW - 6.0, 5.0)


# --- healthcheck.main ------------------------------------------------------------------------


def _file_aged(path: Path, age_s: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()
    os.utime(path, (NOW - age_s, NOW - age_s))


def test_main_fails_when_the_file_is_missing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(path=tmp_path / "alive", clock=lambda: NOW) == 1
    assert "missing" in capsys.readouterr().err


@pytest.mark.parametrize(("age_s", "status"), [(1.0, 0), (29.0, 0), (31.0, 1), (600.0, 1)])
def test_main_follows_the_file_age(tmp_path: Path, age_s: float, status: int) -> None:
    path = tmp_path / "alive"
    _file_aged(path, age_s)
    assert main(path=path, clock=lambda: NOW) == status


def test_main_reports_a_stale_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "alive"
    _file_aged(path, 120.0)
    assert main(path=path, clock=lambda: NOW) == 1
    assert "stale" in capsys.readouterr().err


def test_read_stamp_returns_none_for_a_missing_path(tmp_path: Path) -> None:
    assert read_stamp(tmp_path / "nope") is None


# --- LivenessThread.tick ---------------------------------------------------------------------


def test_tick_creates_the_directory_and_refreshes_the_file(tmp_path: Path) -> None:
    path = tmp_path / "run" / "alive"
    thread = LivenessThread(lambda: None, path)
    assert thread.tick() is True
    assert path.exists()

    old = time.time() - 100.0
    os.utime(path, (old, old))
    assert thread.tick() is True
    assert path.stat().st_mtime > old + 50.0
    assert is_fresh(time.time(), read_stamp(path))


def test_tick_leaves_the_file_alone_when_the_probe_fails(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "alive"

    def failing_probe() -> None:
        raise RuntimeError("database says no: password=hunter2")

    thread = LivenessThread(failing_probe, path)
    with caplog.at_level(logging.WARNING, logger="cairn_worker.app"):
        assert thread.tick() is False
    assert not path.exists()

    _file_aged(path, 100.0)
    stamp = path.stat().st_mtime
    assert thread.tick() is False
    assert path.stat().st_mtime == stamp

    record = caplog.records[0]
    assert record.getMessage() == "worker.liveness_probe_failed"
    assert record.error_class == "RuntimeError"  # type: ignore[attr-defined]
    assert "hunter2" not in caplog.text  # only the class and SQLSTATE are logged


def test_tick_returns_false_when_the_path_cannot_be_written(tmp_path: Path) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("not a directory")
    assert LivenessThread(lambda: None, blocker / "alive").tick() is False


# --- LivenessThread as a thread --------------------------------------------------------------


def test_thread_refreshes_the_file_repeatedly_and_stops(tmp_path: Path) -> None:
    path = tmp_path / "alive"
    ticks = threading.Semaphore(0)

    def probe() -> None:
        ticks.release()

    thread = LivenessThread(probe, path, every_s=0.01)
    thread.start()
    try:
        for _ in range(3):
            assert ticks.acquire(timeout=5), "liveness thread stopped ticking"
        assert main(path=path) == 0  # fresh by the real clock
    finally:
        thread.stop()
    assert not thread.is_alive()


def test_thread_with_a_failing_probe_never_creates_the_file(tmp_path: Path) -> None:
    path = tmp_path / "alive"
    attempts = threading.Semaphore(0)

    def probe() -> None:
        attempts.release()
        raise OSError("down")

    thread = LivenessThread(probe, path, every_s=0.01)
    thread.start()
    try:
        for _ in range(3):
            assert attempts.acquire(timeout=5)
    finally:
        thread.stop()
    assert not path.exists()
    assert main(path=path) == 1


def test_stop_before_start_is_harmless(tmp_path: Path) -> None:
    LivenessThread(lambda: None, tmp_path / "alive").stop()


# --- make_db_probe ---------------------------------------------------------------------------


def test_db_probe_raises_without_leaking_the_password_when_nothing_listens(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    secret = "probe-pw-FAKE-0123456789"
    for field in (*CommonSettings.model_fields.values(), *WorkerSettings.model_fields.values()):
        if field.alias is not None:
            monkeypatch.delenv(field.alias, raising=False)
    monkeypatch.chdir(tmp_path)
    with socket.socket() as sock:  # reserve a port, then close it so connections are refused
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    monkeypatch.setenv("CAIRN_DB_HOST", "127.0.0.1")
    monkeypatch.setenv("CAIRN_DB_PORT", str(port))
    monkeypatch.setenv("CAIRN_DBOS_DB_PASSWORD", secret)
    probe = make_db_probe(load_settings(WorkerSettings))

    with pytest.raises(psycopg.OperationalError) as excinfo:
        probe()
    assert secret not in str(excinfo.value)

    path = tmp_path / "alive"
    assert LivenessThread(probe, path).tick() is False
    assert not path.exists()
