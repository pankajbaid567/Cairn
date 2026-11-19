"""The worker against a real database (Requirements 4.4, 8.1, 8.3, 8.4, 8.7, 17.7).

The database is the compose ``db`` service (``make up-db``). Every test gets its own throwaway
``cairn_test_<hex>`` database, bootstrapped like ``cairn_dbos`` (owned by and open to
``cairn_dbos`` only), so the live ``cairn_dbos`` database and a running ``worker`` container are
never touched. The fixtures skip when the db is down.

Two things run for real:

- DBOS is constructed, launched and given the ``cairn-heartbeat`` schedule in this process
  (``cairn_worker.app`` functions, a five-second cron), then destroyed;
- ``python -m cairn_worker`` is started as a subprocess, sent SIGTERM and timed, which is the
  same code path ``docker compose stop worker`` exercises. The timing of the container itself
  is recorded in ``docs/evidence/phase-0/task-6.3-notes.md``.
"""

from __future__ import annotations

import os
import selectors
import signal
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import psycopg
import pytest
from dbos import DBOS

from cairn_core.bootstrap import DBOS_ROLE, bootstrap_database, connect_like, dbos_spec
from cairn_core.config import WorkerSettings
from cairn_core.testing import TEST_DB_PREFIX, DbTestConfig, drop_database
from cairn_worker import app
from cairn_worker import healthcheck as worker_healthcheck

pytestmark = pytest.mark.integration

CRON = "*/5 * * * * *"
"""Six fields, seconds first: a tick every five seconds."""
INTERVAL_S = 5.0
STOP_BUDGET_S = 10.0
"""Requirement 8.7: the worker stops within 10 seconds of SIGTERM."""


def worker_environment(config: DbTestConfig, dbname: str) -> dict[str, str]:
    """The environment compose would give the worker, aimed at ``dbname`` on the test db."""
    return {
        "CAIRN_DB_HOST": config.target.host,
        "CAIRN_DB_PORT": str(config.target.port),
        "CAIRN_DBOS_DB_NAME": dbname,
        "CAIRN_DBOS_DB_PASSWORD": config.settings.dbos_db_password.get_secret_value(),
        "CAIRN_HEARTBEAT_CRON": CRON,
        "CAIRN_LOG_LEVEL": "INFO",
        "CAIRN_ENV": "ci",
    }


def worker_settings(config: DbTestConfig, dbname: str) -> WorkerSettings:
    return WorkerSettings.model_validate(worker_environment(config, dbname))


@pytest.fixture
def dbos_database(admin_conn: psycopg.Connection[Any], admin_config: DbTestConfig) -> Iterator[str]:
    """An empty database in the shape of ``cairn_dbos``; dropped afterwards."""
    name = f"{TEST_DB_PREFIX}dbos_{os.urandom(4).hex()}"
    try:
        bootstrap_database(admin_conn, name, spec=dbos_spec(name))
        yield name
    finally:
        drop_database(admin_conn, name)


def poll_successes(deadline_s: float) -> list[Any]:
    """SUCCESS runs of ``cairn-heartbeat`` seen through DBOS itself, polled until found."""
    deadline = time.monotonic() + deadline_s
    while True:
        runs = DBOS.list_workflows(schedule_name=app.SCHEDULE_NAME, status="SUCCESS")
        if runs or time.monotonic() >= deadline:
            return list(runs)
        time.sleep(0.5)


def test_heartbeat_schedule_succeeds_and_dbos_tables_are_owned_by_cairn_dbos(
    dbos_database: str,
    admin_conn: psycopg.Connection[Any],
    admin_config: DbTestConfig,
) -> None:
    settings = worker_settings(admin_config, dbos_database)
    assert settings.heartbeat_cron == CRON
    app.prepare_dbos_logging()
    DBOS(config=app.dbos_config(settings))
    try:
        DBOS.launch()
        DBOS.apply_schedules([app.schedule_spec(settings)])

        # At least one SUCCESS run within three intervals (Requirement 8.4).
        runs = poll_successes(3 * INTERVAL_S)
        assert runs, f"no SUCCESS run of {app.SCHEDULE_NAME} within {3 * INTERVAL_S:.0f} s"
        first = runs[0]
        assert first.name.endswith("heartbeat")
        assert first.status == "SUCCESS"
        assert first.workflow_id.startswith(f"sched-{app.SCHEDULE_NAME}-")

        # Calling it again replaces the schedule instead of failing or duplicating it.
        DBOS.apply_schedules([app.schedule_spec(settings)])
        names = [s["schedule_name"] for s in DBOS.list_schedules()]
        assert names == [app.SCHEDULE_NAME]
    finally:
        DBOS.destroy()

    # DBOS keeps its tables in schema ``dbos`` of this database, owned by cairn_dbos
    # (Requirements 4.4 and 8.3). Checked as the superuser after the worker is gone.
    with connect_like(admin_conn, dbos_database) as conn:
        schema_owner = conn.execute(
            "SELECT pg_get_userbyid(nspowner) FROM pg_namespace WHERE nspname = 'dbos'"
        ).fetchone()
        assert schema_owner == (DBOS_ROLE,)
        owners = conn.execute(
            "SELECT DISTINCT pg_get_userbyid(c.relowner) FROM pg_class c "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'dbos' AND c.relkind IN ('r', 'p')"
        ).fetchall()
        assert owners == [(DBOS_ROLE,)]
        tables = {
            str(r[0])
            for r in conn.execute(
                "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = 'dbos' AND c.relkind IN ('r', 'p')"
            ).fetchall()
        }
        assert {"workflow_status", "operation_outputs"} <= tables
        stored = conn.execute(
            "SELECT count(*) FROM dbos.workflow_status "
            "WHERE schedule_name = %s AND status = 'SUCCESS'",
            (app.SCHEDULE_NAME,),
        ).fetchone()
        assert stored is not None
        assert stored[0] >= 1


# The subprocess builds the app exactly as ``python -m cairn_worker`` does, except that the
# liveness file moves out of /tmp so the host is left alone (``run`` looks ``LivenessThread``
# up in the module at call time).
_RUNNER = (
    "import functools, pathlib, sys\n"
    "from cairn_worker import app\n"
    "app.LivenessThread = functools.partial(app.LivenessThread, path=pathlib.Path(sys.argv[1]))\n"
    "raise SystemExit(app.main())\n"
)


def read_until(proc: subprocess.Popen[bytes], needle: bytes, timeout_s: float) -> bytes:
    """Read the child's merged output until ``needle`` appears. Fails on timeout or exit."""
    assert proc.stdout is not None
    selector = selectors.DefaultSelector()
    selector.register(proc.stdout, selectors.EVENT_READ)
    seen = b""
    deadline = time.monotonic() + timeout_s
    try:
        while needle not in seen:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or (proc.poll() is not None and not selector.select(0)):
                raise AssertionError(f"{needle!r} not seen; output so far:\n{seen.decode()}")
            if selector.select(min(remaining, 0.5)):
                chunk = os.read(proc.stdout.fileno(), 65536)
                if not chunk:
                    raise AssertionError(f"worker exited early; output:\n{seen.decode()}")
                seen += chunk
    finally:
        selector.close()
    return seen


def test_worker_process_starts_stays_alive_and_stops_within_ten_seconds(
    dbos_database: str, admin_config: DbTestConfig, tmp_path: Path
) -> None:
    alive = tmp_path / "alive"
    env = {**os.environ, **worker_environment(admin_config, dbos_database)}
    password = admin_config.settings.dbos_db_password.get_secret_value()
    proc = subprocess.Popen(
        [sys.executable, "-c", _RUNNER, str(alive)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    output = b""
    try:
        output = read_until(proc, b"worker.started", timeout_s=60)

        # The liveness thread starts right after ``worker.started``; the healthcheck passes
        # while the file is fresh (Requirements 8.5 and 8.6).
        deadline = time.monotonic() + 15
        while not alive.exists() and time.monotonic() < deadline:
            time.sleep(0.2)
        assert alive.exists()
        assert worker_healthcheck.main(path=alive) == 0

        # A heartbeat tick is logged by the real workflow (Requirement 9.7).
        output += read_until(proc, b'"worker.heartbeat"', timeout_s=3 * INTERVAL_S)

        started = time.monotonic()
        proc.send_signal(signal.SIGTERM)
        status = proc.wait(timeout=STOP_BUDGET_S + 20)
        stop_s = time.monotonic() - started
        assert proc.stdout is not None
        output += proc.stdout.read()
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        if proc.stdout is not None:
            proc.stdout.close()

    text = output.decode(errors="replace")
    assert status == 0, text
    assert stop_s < STOP_BUDGET_S, f"stop took {stop_s:.2f} s\n{text}"
    assert "worker.stopped" in text
    assert password not in text
