"""The combined process against a real database (Requirements 8.1, 8.2, 8.3, 8.5, 8.6, 8.7).

The database is the compose ``db`` service (``make up-db``). Each test gets its own throwaway
``cairn_test_dbos_<hex>`` database, bootstrapped like ``cairn_dbos``, so the live ``cairn_dbos``
database and any running ``worker`` or ``ft-app`` container are never touched. The API side
reads the live ``cairn`` database as ``cairn_app`` (read-only probe), as the ``api`` service does.

``python -m cairn_combined`` runs as a subprocess on a free port. That is the code path
``docker compose stop ft-app`` exercises, minus Docker.
"""

from __future__ import annotations

import json
import os
import selectors
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path
from typing import Any

import psycopg
import pytest

from cairn_core.bootstrap import bootstrap_database, dbos_spec
from cairn_core.testing import TEST_DB_PREFIX, DbTestConfig, drop_database

pytestmark = pytest.mark.integration

CRON = "*/5 * * * * *"
STOP_BUDGET_S = 20.0
"""Requirement 8.7: exit within 20 seconds of SIGTERM."""


@pytest.fixture
def dbos_database(admin_conn: psycopg.Connection[Any]) -> Iterator[str]:
    name = f"{TEST_DB_PREFIX}dbos_{os.urandom(4).hex()}"
    try:
        bootstrap_database(admin_conn, name, spec=dbos_spec(name))
        yield name
    finally:
        drop_database(admin_conn, name)


def free_port() -> int:
    with closing(socket.socket()) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def environment(
    config: DbTestConfig, dbname: str, port: int, *, dbos_password: str
) -> dict[str, str]:
    """What compose gives ``ft-app``, aimed at ``dbname`` on the test db."""
    settings = config.settings
    return {
        **os.environ,
        "PORT": str(port),
        "CAIRN_ENV": "ci",
        "CAIRN_LOG_LEVEL": "INFO",
        "CAIRN_DB_HOST": config.target.host,
        "CAIRN_DB_PORT": str(config.target.port),
        "CAIRN_DBOS_DB_NAME": dbname,
        "CAIRN_DBOS_DB_PASSWORD": dbos_password,
        "CAIRN_APP_DB_PASSWORD": settings.app_db_password.get_secret_value(),
        "CAIRN_HEARTBEAT_CRON": CRON,
        "CAIRN_JOBS_RETRY_MAX_S": "2",
        "OTEL_TRACES_EXPORTER": "none",
    }


def get(port: int, path: str) -> tuple[int, dict[str, Any]]:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def wait_for(predicate: Any, timeout_s: float, what: str) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            if predicate():
                return
        except (urllib.error.URLError, ConnectionError, OSError):
            pass
        time.sleep(0.25)
    raise AssertionError(f"timed out waiting for {what}")


def read_available(proc: subprocess.Popen[bytes]) -> bytes:
    assert proc.stdout is not None
    selector = selectors.DefaultSelector()
    selector.register(proc.stdout, selectors.EVENT_READ)
    chunks = b""
    try:
        while selector.select(0):
            chunk = os.read(proc.stdout.fileno(), 65536)
            if not chunk:
                break
            chunks += chunk
    finally:
        selector.close()
    return chunks


def start(env: dict[str, str], cwd: Path) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        [sys.executable, "-m", "cairn_combined"],
        env=env,
        cwd=cwd,  # no .env here
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )


def stop(proc: subprocess.Popen[bytes]) -> tuple[int, float, str]:
    started = time.monotonic()
    proc.send_signal(signal.SIGTERM)
    status = proc.wait(timeout=STOP_BUDGET_S + 20)
    elapsed = time.monotonic() - started
    assert proc.stdout is not None
    output = proc.stdout.read().decode(errors="replace")
    return status, elapsed, output


def test_healthz_first_then_jobs_launch_heartbeat_runs_and_sigterm_stops_within_20_s(
    dbos_database: str, admin_config: DbTestConfig, tmp_path: Path
) -> None:
    port = free_port()
    password = admin_config.settings.dbos_db_password.get_secret_value()
    proc = start(environment(admin_config, dbos_database, port, dbos_password=password), tmp_path)
    seen = b""
    try:
        wait_for(lambda: get(port, "/healthz")[0] == 200, 30, "/healthz")
        wait_for(lambda: get(port, "/readyz")[1]["checks"]["jobs"]["ok"], 60, "the jobs check")
        body = get(port, "/readyz")[1]
        assert body["checks"]["jobs"]["ok"] is True
        assert body["checks"]["database"]["ok"] is True
        # a heartbeat tick from the real workflow (Requirement 8.5)
        deadline = time.monotonic() + 20
        while b'"worker.heartbeat"' not in seen and time.monotonic() < deadline:
            seen += read_available(proc)
            time.sleep(0.25)
        assert b'"worker.heartbeat"' in seen
        code, elapsed, rest = stop(proc)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        if proc.stdout is not None:
            proc.stdout.close()
    text = (seen.decode(errors="replace")) + rest
    # uvicorn re-raises SIGTERM after its shutdown, so the status is 0 or -SIGTERM.
    assert code in (0, -signal.SIGTERM), text
    assert elapsed < STOP_BUDGET_S, f"stop took {elapsed:.2f} s\n{text}"
    assert "jobs.launched" in text
    assert "jobs.stopped" in text
    assert password not in text


def test_a_failing_launch_keeps_healthz_up_reports_jobs_not_launched_and_still_stops(
    dbos_database: str, admin_config: DbTestConfig, tmp_path: Path
) -> None:
    port = free_port()
    env = environment(admin_config, dbos_database, port, dbos_password="wrong-password-for-test")
    proc = start(env, tmp_path)
    seen = b""
    try:
        wait_for(lambda: get(port, "/healthz")[0] == 200, 30, "/healthz")

        def failing_twice() -> bool:
            _, body = get(port, "/readyz")
            return int(body["checks"]["jobs"]["detail"]["attempts"]) >= 2

        wait_for(failing_twice, 30, "two failed launch attempts")
        status, body = get(port, "/readyz")
        assert status == 503
        assert body["checks"]["jobs"]["error"] == "jobs_not_launched"
        assert get(port, "/healthz")[0] == 200
        seen = read_available(proc)
        code, elapsed, rest = stop(proc)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        if proc.stdout is not None:
            proc.stdout.close()
    text = seen.decode(errors="replace") + rest
    assert code in (0, -signal.SIGTERM), text
    assert elapsed < STOP_BUDGET_S, f"stop took {elapsed:.2f} s\n{text}"
    assert '"jobs.launch_failed"' in text
    assert "wrong-password-for-test" not in text
