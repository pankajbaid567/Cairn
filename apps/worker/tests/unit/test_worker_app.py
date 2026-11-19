"""Unit tests for cairn_worker.app: heartbeat record, schedule spec, DBOS config, logging, signals.

No database is used and DBOS is never launched. One test constructs the ``DBOS`` object
(which only reads configuration) to check how its logger is wired, then destroys it.
"""

from __future__ import annotations

import importlib
import io
import json
import logging
import signal
import threading
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest
from dbos import DBOS
from sqlalchemy.engine import make_url  # DBOS parses system_database_url with this

from cairn_core.config import CommonSettings, WorkerSettings, env_keys, load_settings
from cairn_core.telemetry import configure_logging
from cairn_worker import app
from cairn_worker.app import (
    SCHEDULE_NAME,
    dbos_config,
    dbos_url,
    describe_target,
    heartbeat,
    heartbeat_record,
    make_db_probe,
    prepare_dbos_logging,
    schedule_spec,
    wait_for_signal,
)

DBOS_PASSWORD = "dbos-pw-FAKE/with:odd@chars#1"
LOG_RECORD_ATTRIBUTES = set(logging.LogRecord("n", 0, "p", 0, "m", (), None).__dict__) | {
    "message",
    "asctime",
}


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for key in env_keys(CommonSettings) | env_keys(WorkerSettings):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.chdir(tmp_path)  # no .env here
    monkeypatch.setenv("CAIRN_DBOS_DB_PASSWORD", DBOS_PASSWORD)


def _settings(monkeypatch: pytest.MonkeyPatch, **env: str) -> WorkerSettings:
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return load_settings(WorkerSettings)


# --- heartbeat_record ----------------------------------------------------------------------


def test_heartbeat_record_for_a_utc_time() -> None:
    when = datetime(2026, 10, 27, 12, 0, 30, tzinfo=UTC)
    assert heartbeat_record(when, "phase-0") == {
        "schedule_name": "cairn-heartbeat",
        "scheduled_time": "2026-10-27T12:00:30+00:00",
        "schedule_context": "phase-0",
    }


def test_heartbeat_record_converts_other_zones_and_reads_naive_as_utc() -> None:
    plus_two = timezone(timedelta(hours=2))
    aware = datetime(2026, 10, 27, 14, 0, 0, tzinfo=plus_two)
    naive = datetime(2026, 10, 27, 12, 0, 0)
    assert heartbeat_record(aware, None)["scheduled_time"] == "2026-10-27T12:00:00+00:00"
    assert heartbeat_record(naive, None)["scheduled_time"] == "2026-10-27T12:00:00+00:00"
    assert heartbeat_record(naive, None)["schedule_context"] == "None"


def test_heartbeat_record_keys_are_safe_as_logging_extra() -> None:
    record = heartbeat_record(datetime(2026, 10, 27, tzinfo=UTC), "x")
    assert not set(record) & LOG_RECORD_ATTRIBUTES
    logging.getLogger("test").makeRecord("test", logging.INFO, "f", 1, "m", (), None, extra=record)


# --- schedule_spec -------------------------------------------------------------------------


def test_schedule_spec_uses_the_default_cron_and_the_heartbeat_workflow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = schedule_spec(_settings(monkeypatch))
    assert spec == {
        "schedule_name": SCHEDULE_NAME,
        "workflow_fn": heartbeat,
        "schedule": "*/30 * * * * *",
        "context": "phase-0",
        "automatic_backfill": False,
    }
    assert SCHEDULE_NAME == "cairn-heartbeat"


def test_schedule_spec_takes_the_cron_from_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(monkeypatch, CAIRN_HEARTBEAT_CRON="*/5 * * * * *")
    assert schedule_spec(settings)["schedule"] == "*/5 * * * * *"


@pytest.mark.parametrize(
    ("cron", "expected_next"),
    [
        ("*/30 * * * * *", "2026-10-27T12:00:30"),  # six fields: seconds first
        ("*/5 * * * * *", "2026-10-27T12:00:15"),
        ("5 * * * *", "2026-10-27T12:05:00"),  # five fields: minute first
    ],
)
def test_cron_strings_mean_what_the_design_says_in_dbos(cron: str, expected_next: str) -> None:
    # DBOS validates and evaluates with croniter(second_at_beginning=True); croniter ships no
    # type information, so it is imported dynamically here. Verify V2 records the same facts.
    croniter = importlib.import_module("croniter").croniter
    assert croniter.is_valid(cron, second_at_beginning=True)
    start = datetime(2026, 10, 27, 12, 0, 10)
    nxt = croniter(cron, start, second_at_beginning=True).get_next(datetime)
    assert nxt.isoformat() == expected_next


# --- dbos_config ---------------------------------------------------------------------------


def test_dbos_config_matches_the_design(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(monkeypatch, CAIRN_DB_HOST="db", CAIRN_LOG_LEVEL="WARNING")
    cfg = dbos_config(settings)
    assert cfg["name"] == "cairn-worker"
    assert cfg["sys_db_pool_size"] == 5
    assert cfg["run_migrations"] is True
    assert cfg["enable_otlp"] is False
    assert cfg["log_level"] == "WARNING"
    assert cfg["system_database_url"] == dbos_url(settings)


def test_dbos_url_targets_cairn_dbos_as_cairn_dbos_with_a_quoted_password(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(monkeypatch, CAIRN_DB_HOST="db", CAIRN_DB_PORT="5433")
    url = dbos_url(settings)
    assert url == (
        "postgresql://cairn_dbos:dbos-pw-FAKE%2Fwith%3Aodd%40chars%231@db:5433/cairn_dbos"
        "?sslmode=prefer"
    )
    # The content database and the app role never appear.
    assert "/cairn?" not in url
    assert "cairn_app" not in url


def test_dbos_url_follows_the_database_name_setting_and_brackets_ipv6(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(monkeypatch, CAIRN_DB_HOST="::1", CAIRN_DBOS_DB_NAME="cairn_dbos_test")
    assert dbos_url(settings).endswith("@[::1]:5432/cairn_dbos_test?sslmode=prefer")


def test_dbos_url_carries_the_tls_settings_as_query_parameters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(
        monkeypatch,
        CAIRN_DB_HOST="ep-fictional-123.ap-southeast-1.aws.example",
        CAIRN_DB_SSLMODE="verify-full",
        CAIRN_DB_SSLROOTCERT="/etc/ssl/certs/ca-certificates.crt",
    )
    url = make_url(dbos_url(settings))  # how DBOS (SQLAlchemy) reads it
    assert url.username == "cairn_dbos"
    assert url.password == DBOS_PASSWORD
    assert (url.host, url.port, url.database) == (
        "ep-fictional-123.ap-southeast-1.aws.example",
        5432,
        "cairn_dbos",
    )
    assert dict(url.query) == {
        "sslmode": "verify-full",
        "sslrootcert": "/etc/ssl/certs/ca-certificates.crt",
    }


def test_dbos_url_without_a_root_certificate_has_only_sslmode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    url = make_url(dbos_url(_settings(monkeypatch, CAIRN_DB_SSLMODE="require")))
    assert dict(url.query) == {"sslmode": "require"}


@pytest.mark.parametrize(
    ("tls", "expected"),
    [
        ({}, {"sslmode": "prefer"}),
        (
            {"CAIRN_DB_SSLMODE": "verify-full", "CAIRN_DB_SSLROOTCERT": "/certs/root.crt"},
            {"sslmode": "verify-full", "sslrootcert": "/certs/root.crt"},
        ),
    ],
)
def test_liveness_probe_passes_the_tls_settings(
    monkeypatch: pytest.MonkeyPatch, tls: dict[str, str], expected: dict[str, str]
) -> None:
    captured: list[dict[str, object]] = []

    def fake_connect(**kwargs: object) -> None:
        captured.append(kwargs)
        raise app.psycopg.OperationalError("stopped by the test")

    monkeypatch.setattr(app.psycopg, "connect", fake_connect)
    probe = make_db_probe(_settings(monkeypatch, **tls))
    with pytest.raises(app.psycopg.OperationalError):
        probe()
    (kwargs,) = captured
    assert {k: kwargs[k] for k in ("sslmode", "sslrootcert") if k in kwargs} == expected
    assert (kwargs["user"], kwargs["dbname"]) == ("cairn_dbos", "cairn_dbos")


def test_describe_target_has_no_password(monkeypatch: pytest.MonkeyPatch) -> None:
    text = describe_target(_settings(monkeypatch, CAIRN_DB_HOST="db"))
    assert text == "cairn_dbos@db:5432/cairn_dbos"
    assert DBOS_PASSWORD not in text


# --- logging -------------------------------------------------------------------------------


@pytest.fixture
def restore_loggers() -> Iterator[None]:
    root, dbos_logger = logging.getLogger(), logging.getLogger("dbos")
    saved = (list(root.handlers), root.level, list(dbos_logger.handlers), dbos_logger.propagate)
    yield
    root.handlers[:] = saved[0]
    root.setLevel(saved[1])
    dbos_logger.handlers[:] = saved[2]
    dbos_logger.propagate = saved[3]


@pytest.mark.usefixtures("restore_loggers")
def test_dbos_records_reach_the_root_json_handler(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify V2: without ``prepare_dbos_logging`` DBOS cuts the ``dbos`` logger off from root."""
    settings = _settings(monkeypatch, CAIRN_DB_HOST="db")
    stream = io.StringIO()
    configure_logging("cairn-worker", "INFO", stream=stream)

    prepare_dbos_logging()
    DBOS(config=dbos_config(settings))  # reads configuration only; nothing connects
    try:
        dbos_logger = logging.getLogger("dbos")
        assert dbos_logger.propagate is True
        assert not [h for h in dbos_logger.handlers if isinstance(h, logging.StreamHandler)]
        dbos_logger.info("hello from dbos")
    finally:
        DBOS.destroy()

    lines = [json.loads(line) for line in stream.getvalue().splitlines()]
    mine = [line for line in lines if line["logger"] == "dbos"]
    assert {"hello from dbos"} <= {line["msg"] for line in mine}
    assert all(line["service"] == "cairn-worker" for line in mine)
    assert any(line["msg"].startswith("Initializing DBOS") for line in mine)
    assert DBOS_PASSWORD not in stream.getvalue()


# --- wait_for_signal -----------------------------------------------------------------------


def test_wait_for_signal_returns_the_signal_and_restores_handlers() -> None:
    before = signal.getsignal(signal.SIGUSR1)
    timer = threading.Timer(0.05, signal.raise_signal, args=(signal.SIGUSR1,))
    timer.start()
    try:
        assert wait_for_signal((signal.SIGUSR1, signal.SIGUSR2)) == signal.SIGUSR1
    finally:
        timer.join()
    assert signal.getsignal(signal.SIGUSR1) == before


def test_module_exposes_the_documented_public_names() -> None:
    for name in ("main", "run", "dbos_config", "schedule_spec", "heartbeat", "LivenessThread"):
        assert hasattr(app, name)
