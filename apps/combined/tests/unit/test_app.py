"""Unit tests for the combined process: the jobs runner, the app wiring, settings and ``main``.

No database and no DBOS: the runner gets fake ``launch`` and ``destroy`` functions, readiness
gets injected probes, and uvicorn is replaced where ``main`` is called.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace import TracerProvider

import cairn_combined.app as combined
from cairn_api.main import build_app
from cairn_combined.app import (
    JobsRunner,
    backoff_schedule,
    create_app,
    main,
    runner_lifespan,
    sqlstate_of,
)
from cairn_core.config import CombinedSettings, env_keys
from cairn_core.health import DbProbe, JobsStatus, ProbeResult
from cairn_core.telemetry import build_tracer_provider

SECRET = "combined-pw-do-not-print"
EXTENSIONS = {"vector": "0.8.1", "btree_gist": "1.8"}


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for key in env_keys(CombinedSettings):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.chdir(tmp_path)  # no .env here


def settings_with(**overrides: object) -> CombinedSettings:
    values: dict[str, object] = {
        "CAIRN_APP_DB_PASSWORD": SECRET,
        "CAIRN_DBOS_DB_PASSWORD": SECRET + "-dbos",
        **overrides,
    }
    return CombinedSettings.model_validate(values)


def ready_probe() -> DbProbe:
    return DbProbe(
        server_version_num=180001,
        extensions=dict(EXTENSIONS),
        role_name="cairn_app",
        role_is_superuser=False,
        role_bypasses_rls=False,
        schema_version=1,
        latency_ms=1.0,
    )


async def ready() -> ProbeResult:
    return ready_probe()


class Recorder:
    """Fake launch and destroy: fails the first ``failures`` launches, records every call."""

    def __init__(self, failures: int = 0, error: Exception | None = None) -> None:
        self.failures = failures
        self.error = error or RuntimeError("boom")
        self.calls: list[str] = []
        self.waits: list[float] = []

    def launch(self, _settings: CombinedSettings) -> None:
        self.calls.append("launch")
        if self.calls.count("launch") <= self.failures:
            raise self.error

    def destroy(self) -> None:
        self.calls.append("destroy")

    def wait(self, seconds: float) -> bool:
        self.waits.append(seconds)
        return False


def runner_for(rec: Recorder, **kwargs: Any) -> JobsRunner:
    return JobsRunner(
        settings_with(), launch=rec.launch, destroy=rec.destroy, wait=rec.wait, **kwargs
    )


# --- backoff_schedule ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("attempt", "cap", "expected"),
    [
        (1, 60, 1),
        (2, 60, 2),
        (3, 60, 4),
        (6, 60, 32),
        (7, 60, 60),
        (50, 60, 60),
        (5, 3, 3),
        (1, 0.5, 0.5),
    ],
)
def test_backoff_doubles_from_one_second_up_to_the_cap(
    attempt: int, cap: float, expected: float
) -> None:
    assert backoff_schedule(attempt, cap) == expected


def test_backoff_rejects_attempt_zero() -> None:
    with pytest.raises(ValueError, match="attempt"):
        backoff_schedule(0, 60)


# --- JobsRunner ---------------------------------------------------------------------------


def test_runner_launches_on_the_first_try() -> None:
    rec = Recorder()
    runner = runner_for(rec)
    assert runner.status() == JobsStatus(launched=False, attempts=0)
    runner.run()
    assert rec.calls == ["launch"]
    assert rec.waits == []
    assert runner.status() == JobsStatus(launched=True, attempts=1)


def test_runner_retries_with_backoff_and_destroys_after_each_failure() -> None:
    rec = Recorder(failures=3)
    runner = runner_for(rec)
    runner.run()
    assert rec.calls == ["launch", "destroy", "launch", "destroy", "launch", "destroy", "launch"]
    assert rec.waits == [1.0, 2.0, 4.0]
    assert runner.status() == JobsStatus(launched=True, attempts=4)


def test_runner_caps_the_backoff_at_the_setting() -> None:
    rec = Recorder(failures=5)
    runner = JobsRunner(
        settings_with(CAIRN_JOBS_RETRY_MAX_S="3"),
        launch=rec.launch,
        destroy=rec.destroy,
        wait=rec.wait,
    )
    runner.run()
    assert rec.waits == [1.0, 2.0, 3.0, 3.0, 3.0]


def test_max_backoff_argument_overrides_the_setting() -> None:
    rec = Recorder(failures=3)
    runner_for(rec, max_backoff_s=2.0).run()
    assert rec.waits == [1.0, 2.0, 2.0]


def test_runner_stops_when_the_wait_reports_stop_and_launches_no_more() -> None:
    rec = Recorder(failures=100)
    stops_after = 2
    waited: list[float] = []

    def wait(seconds: float) -> bool:
        waited.append(seconds)
        return len(waited) >= stops_after

    runner = JobsRunner(settings_with(), launch=rec.launch, destroy=rec.destroy, wait=wait)
    runner.run()
    assert rec.calls == ["launch", "destroy", "launch", "destroy"]
    assert waited == [1.0, 2.0]
    assert runner.status() == JobsStatus(launched=False, attempts=2)
    runner.stop()  # not launched: no extra destroy
    assert rec.calls.count("destroy") == 2


def test_stop_while_really_waiting_ends_the_thread_without_another_launch() -> None:
    launched_once = threading.Event()
    calls: list[str] = []

    def launch(_settings: CombinedSettings) -> None:
        calls.append("launch")
        launched_once.set()
        raise RuntimeError("down")

    runner = JobsRunner(
        settings_with(), launch=launch, destroy=lambda: calls.append("destroy")
    )  # the real Event.wait, backoff 1 s
    runner.start()
    assert launched_once.wait(5)
    runner.stop(timeout=5)
    assert not runner.is_alive()
    assert calls == ["launch", "destroy"]
    assert runner.status().launched is False


def test_stop_after_launch_destroys_once_and_is_idempotent() -> None:
    rec = Recorder()
    runner = runner_for(rec)
    runner.start()
    runner.join(5)
    assert runner.status().launched is True
    runner.stop()
    runner.stop()
    assert rec.calls == ["launch", "destroy"]
    assert runner.status().launched is False


def test_stop_before_start_prevents_any_launch() -> None:
    rec = Recorder()
    runner = runner_for(rec)
    runner.stop()
    runner.run()
    assert rec.calls == []


def test_a_failing_destroy_does_not_end_the_retry_loop() -> None:
    rec = Recorder(failures=1)

    def bad_destroy() -> None:
        rec.calls.append("destroy")
        raise RuntimeError("teardown failed")

    runner = JobsRunner(settings_with(), launch=rec.launch, destroy=bad_destroy, wait=rec.wait)
    runner.run()
    assert rec.calls == ["launch", "destroy", "launch"]
    assert runner.status().launched is True


class FakeSqlStateError(Exception):
    sqlstate = "28P01"


def test_launch_failure_logs_class_and_sqlstate_but_not_the_message(
    caplog: pytest.LogCaptureFixture,
) -> None:
    rec = Recorder(failures=1, error=FakeSqlStateError(f"password {SECRET} rejected by 10.0.0.9"))
    with caplog.at_level(logging.INFO, logger="cairn_combined.app"):
        runner_for(rec).run()
    [failed] = [r for r in caplog.records if r.getMessage() == "jobs.launch_failed"]
    assert failed.__dict__["error_class"] == "FakeSqlStateError"
    assert failed.__dict__["sqlstate"] == "28P01"
    assert failed.__dict__["attempt"] == 1
    assert failed.__dict__["retry_in_s"] == 1.0
    assert failed.exc_info is None
    text = " ".join(str(r.__dict__) + r.getMessage() for r in caplog.records)
    assert SECRET not in text
    assert "10.0.0.9" not in text
    assert any(r.getMessage() == "jobs.launched" for r in caplog.records)


def test_sqlstate_of_reads_the_cause_chain() -> None:
    inner = FakeSqlStateError("x")
    outer = RuntimeError("wrapped")
    outer.__cause__ = inner
    assert sqlstate_of(outer) == "28P01"
    assert sqlstate_of(RuntimeError("plain")) is None


# --- app wiring: /healthz and /readyz -----------------------------------------------------


@pytest.fixture
def blocked_launch() -> Iterator[tuple[JobsRunner, threading.Event, threading.Event]]:
    """A runner whose launch blocks until the test releases it."""
    entered, release = threading.Event(), threading.Event()

    def launch(_settings: CombinedSettings) -> None:
        entered.set()
        assert release.wait(10)

    runner = JobsRunner(settings_with(), launch=launch, destroy=lambda: None)
    yield runner, entered, release
    release.set()
    runner.stop(timeout=5)


def combined_app(runner: JobsRunner) -> FastAPI:
    return build_app(
        settings_with(),
        probe=ready,
        expected_head=1,
        jobs=runner.status,
        lifespan=runner_lifespan(runner),
    )


def test_healthz_is_served_while_the_launch_blocks_and_readyz_follows_the_runner(
    blocked_launch: tuple[JobsRunner, threading.Event, threading.Event],
) -> None:
    runner, entered, release = blocked_launch
    with TestClient(combined_app(runner)) as client:
        assert entered.wait(5), "the lifespan did not start the runner"
        assert client.get("/healthz").status_code == 200

        response = client.get("/readyz")
        assert response.status_code == 503
        body = response.json()
        assert body["status"] == "not_ready"
        assert body["checks"]["jobs"] == {
            "ok": False,
            "error": "jobs_not_launched",
            "detail": {"attempts": 1},
        }
        assert body["checks"]["database"]["ok"] is True

        release.set()
        runner.join(5)
        response = client.get("/readyz")
        assert response.status_code == 200
        assert response.json()["checks"]["jobs"] == {"ok": True, "detail": {"attempts": 1}}


def test_readyz_without_a_jobs_probe_has_no_jobs_check() -> None:
    app = build_app(settings_with(), probe=ready, expected_head=1)
    with TestClient(app) as client:
        response = client.get("/readyz")
    assert response.status_code == 200
    assert "jobs" not in response.json()["checks"]


def test_lifespan_stops_the_runner_and_destroys_dbos_at_shutdown() -> None:
    rec = Recorder()
    runner = JobsRunner(settings_with(), launch=rec.launch, destroy=rec.destroy)
    with TestClient(combined_app(runner)) as client:
        runner.join(5)
        assert client.get("/readyz").status_code == 200
    assert rec.calls == ["launch", "destroy"]
    assert not runner.is_alive()


# --- settings -----------------------------------------------------------------------------


def test_defaults() -> None:
    settings = settings_with()
    assert settings.port == 10000
    assert settings.jobs_retry_max_s == 60.0
    assert settings.heartbeat_cron == "0 */5 * * * *"


def test_both_runtime_passwords_are_required(
    clean_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main()
    assert exit_info.value.code == 2
    err = capsys.readouterr().err
    assert "CAIRN_APP_DB_PASSWORD" in err
    assert "CAIRN_DBOS_DB_PASSWORD" in err


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("PORT", "70000"),
        ("PORT", "http"),
        ("CAIRN_JOBS_RETRY_MAX_S", "0"),
        ("CAIRN_JOBS_RETRY_MAX_S", "601"),
        ("CAIRN_HEARTBEAT_CRON", "* * *"),
        ("CAIRN_DB_SSLMODE", "sometimes"),
    ],
)
def test_invalid_settings_exit_2_naming_the_key_before_uvicorn_runs(
    clean_env: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    key: str,
    value: str,
) -> None:
    monkeypatch.setenv("CAIRN_APP_DB_PASSWORD", SECRET)
    monkeypatch.setenv("CAIRN_DBOS_DB_PASSWORD", SECRET)
    monkeypatch.setenv(key, value)
    monkeypatch.setattr(
        combined.uvicorn, "run", lambda *a, **kw: pytest.fail("uvicorn.run must not be reached")
    )
    with pytest.raises(SystemExit) as exit_info:
        main()
    assert exit_info.value.code == 2
    err = capsys.readouterr().err
    assert key in err
    assert SECRET not in err
    assert value not in err or value == "0"  # "0" also appears in the error code text


# --- main and create_app ------------------------------------------------------------------


def test_main_passes_port_and_the_uvicorn_options(
    clean_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CAIRN_APP_DB_PASSWORD", SECRET)
    monkeypatch.setenv("CAIRN_DBOS_DB_PASSWORD", SECRET)
    monkeypatch.setenv("PORT", "12345")
    calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    monkeypatch.setattr(combined.uvicorn, "run", lambda *a, **kw: calls.append((a, kw)))
    monkeypatch.setattr(combined, "_terminate", lambda code: None)
    main()
    [(args, kwargs)] = calls
    assert args == (create_app,)
    assert kwargs == {
        "factory": True,
        "host": "0.0.0.0",  # noqa: S104
        "port": 12345,
        "log_config": None,
        "access_log": False,
        "server_header": False,
        "timeout_graceful_shutdown": 20,
    }


def test_main_defaults_to_port_10000(clean_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CAIRN_APP_DB_PASSWORD", SECRET)
    monkeypatch.setenv("CAIRN_DBOS_DB_PASSWORD", SECRET)
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(combined.uvicorn, "run", lambda *a, **kw: calls.append(kw))
    monkeypatch.setattr(combined, "_terminate", lambda code: None)
    main()
    assert calls[0]["port"] == 10000


def test_create_app_wires_logging_tracing_psycopg_and_the_runner(
    clean_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CAIRN_APP_DB_PASSWORD", SECRET)
    monkeypatch.setenv("CAIRN_DBOS_DB_PASSWORD", SECRET)
    provider = build_tracer_provider("cairn-combined", "none")
    calls: list[tuple[str, tuple[object, ...]]] = []

    class StubInstrumentor:
        def instrument(self, **kwargs: object) -> None:
            calls.append(("psycopg", (kwargs["tracer_provider"],)))

    def fake_configure_tracing(
        service: str, exporter: str, endpoint: str | None, *, environment: str
    ) -> TracerProvider:
        calls.append(("tracing", (service, exporter, endpoint, environment)))
        return provider

    monkeypatch.setattr(
        combined, "configure_logging", lambda s, level: calls.append(("logging", (s, level)))
    )
    monkeypatch.setattr(combined, "configure_tracing", fake_configure_tracing)
    monkeypatch.setattr(combined, "PsycopgInstrumentor", StubInstrumentor)
    started: list[bool] = []
    monkeypatch.setattr(combined.JobsRunner, "start", lambda self: started.append(True))
    stopped: list[bool] = []
    monkeypatch.setattr(
        combined.JobsRunner, "stop", lambda self, timeout=20.0: stopped.append(True)
    )

    app = create_app()

    assert calls == [
        ("logging", ("cairn-combined", "INFO")),
        ("tracing", ("cairn-combined", "none", None, "local")),
        ("psycopg", (provider,)),
    ]
    with TestClient(app) as client:
        assert client.get("/healthz").status_code == 200
        assert started == [True]
    assert stopped == [True]


def test_main_leaves_without_interpreter_finalization_after_a_clean_shutdown(
    clean_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On 0.1 CPU, finalizing the interpreter outlasts the stop grace period (plan 4.20), so
    ``main`` flushes and exits directly once uvicorn has returned."""
    monkeypatch.setenv("CAIRN_APP_DB_PASSWORD", SECRET)
    monkeypatch.setenv("CAIRN_DBOS_DB_PASSWORD", SECRET)
    order: list[str] = []
    monkeypatch.setattr(combined.uvicorn, "run", lambda *a, **kw: order.append("served"))
    monkeypatch.setattr(combined, "_terminate", lambda code: order.append(f"exit{code}"))
    main()
    assert order == ["served", "exit0"]


def test_terminate_flushes_logging_then_calls_os_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[object] = []
    monkeypatch.setattr(combined.logging, "shutdown", lambda: seen.append("logging"))
    monkeypatch.setattr(combined.os, "_exit", lambda code: seen.append(("exit", code)))
    combined._terminate(0)
    assert seen == ["logging", ("exit", 0)]
