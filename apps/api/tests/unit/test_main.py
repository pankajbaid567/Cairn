"""Unit tests for the API app: /healthz, /readyz, access log, spans, create_app.

No database is needed. Readiness uses injected probes; the unreachable-database cases use the
real probe against a port that nothing serves.
"""

from __future__ import annotations

import io
import json
import logging
import socket
import time
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind

import cairn_api.main as api_main
from cairn_api.main import build_app, create_app
from cairn_core.config import ApiSettings
from cairn_core.health import DbProbe, DbUnavailable, ProbeResult
from cairn_core.telemetry import JsonFormatter, build_tracer_provider

SECRET = "s3cr3t-pw-do-not-print"
EXTENSIONS = {"vector": "0.8.1", "btree_gist": "1.8"}
CAIRN_ENV_KEYS = (
    "CAIRN_ENV",
    "CAIRN_LOG_LEVEL",
    "CAIRN_DB_HOST",
    "CAIRN_DB_PORT",
    "CAIRN_DB_NAME",
    "CAIRN_DB_TIMEOUT_S",
    "CAIRN_APP_DB_USER",
    "CAIRN_APP_DB_PASSWORD",
    "CAIRN_PROXY_TOKEN",
    "OTEL_TRACES_EXPORTER",
    "OTEL_EXPORTER_OTLP_ENDPOINT",
)


def closed_port() -> int:
    with closing(socket.socket()) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def settings_for(port: int) -> ApiSettings:
    return ApiSettings.model_validate(
        {
            "CAIRN_DB_HOST": "127.0.0.1",
            "CAIRN_DB_PORT": port,
            "CAIRN_DB_TIMEOUT_S": 2.0,
            "CAIRN_APP_DB_PASSWORD": SECRET,
            "CAIRN_PROXY_TOKEN": None,  # explicit: a local .env may hold a token
        }
    )


def ready_probe(**overrides: object) -> DbProbe:
    values: dict[str, object] = {
        "server_version_num": 180001,
        "extensions": dict(EXTENSIONS),
        "role_name": "cairn_app",
        "role_is_superuser": False,
        "role_bypasses_rls": False,
        "schema_version": 1,
        "latency_ms": 1.0,
    }
    values.update(overrides)
    return DbProbe(**values)  # type: ignore[arg-type]


def app_with(result: ProbeResult | Exception, **kwargs: object) -> FastAPI:
    async def probe() -> ProbeResult:
        if isinstance(result, Exception):
            raise result
        return result

    return build_app(settings_for(closed_port()), probe=probe, expected_head=1, **kwargs)  # type: ignore[arg-type]


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(app_with(ready_probe())) as c:
        yield c


# --- /healthz --------------------------------------------------------------------------


def test_healthz_is_200(client: TestClient) -> None:
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_healthz_is_200_when_the_database_host_is_unreachable() -> None:
    # Real probe pointed at a closed port; /healthz must not even try it.
    app = build_app(settings_for(closed_port()), expected_head=1)
    with TestClient(app) as c:
        started = time.perf_counter()
        response = c.get("/healthz")
    assert response.status_code == 200
    assert time.perf_counter() - started < 1


def test_healthz_never_calls_the_probe() -> None:
    calls = 0

    async def probe() -> ProbeResult:
        nonlocal calls
        calls += 1
        return DbUnavailable("db_unreachable")

    with TestClient(build_app(settings_for(1), probe=probe, expected_head=1)) as c:
        assert c.get("/healthz").status_code == 200
    assert calls == 0


def test_app_starts_without_touching_the_database() -> None:
    # Startup (lifespan) must open no connection: building and starting the app with a dead
    # database host succeeds, and the first probe only happens on /readyz.
    with TestClient(build_app(settings_for(closed_port()), expected_head=1)):
        pass


# --- /readyz ---------------------------------------------------------------------------


def test_readyz_200_lists_the_extension_versions(client: TestClient) -> None:
    response = client.get("/readyz")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert body["extensions"] == {"btree_gist": "1.8", "vector": "0.8.1"}
    assert set(body["checks"]) == {"database", "postgres", "extensions", "role", "schema"}
    assert all(c["ok"] for c in body["checks"].values())
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize(
    ("result", "check", "code"),
    [
        (DbUnavailable("db_unreachable"), "database", "db_unreachable"),
        (DbUnavailable("db_timeout"), "database", "db_timeout"),
        (ready_probe(server_version_num=170004), "postgres", "postgres_major_mismatch"),
        (ready_probe(extensions={"vector": "0.8.1"}), "extensions", "extensions_missing"),
        (
            ready_probe(extensions={"vector": "0.7.4", "btree_gist": "1.8"}),
            "extensions",
            "extensions_outdated",
        ),
        (ready_probe(role_is_superuser=True), "role", "role_unsafe"),
        (ready_probe(role_bypasses_rls=True), "role", "role_unsafe"),
        (ready_probe(schema_version=None), "schema", "schema_behind"),
        (ready_probe(schema_version=2), "schema", "schema_ahead"),
    ],
)
def test_readyz_503_with_the_failing_code(result: ProbeResult, check: str, code: str) -> None:
    with TestClient(app_with(result)) as c:
        response = c.get("/readyz")
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "not_ready"
    assert body["checks"][check] == {**body["checks"][check], "ok": False, "error": code}


def test_readyz_names_the_missing_extensions() -> None:
    with TestClient(app_with(ready_probe(extensions={"vector": "0.8.1"}))) as c:
        body = c.get("/readyz").json()
    assert body["checks"]["extensions"]["detail"] == {"missing": "btree_gist"}
    assert body["extensions"] == {"vector": "0.8.1"}


def test_readyz_is_503_db_unreachable_within_three_seconds_with_a_dead_database() -> None:
    app = build_app(settings_for(closed_port()), expected_head=1)
    with TestClient(app) as c:
        started = time.perf_counter()
        response = c.get("/readyz")
        elapsed = time.perf_counter() - started
        assert response.status_code == 503
        assert response.json()["checks"]["database"] == {"ok": False, "error": "db_unreachable"}
        assert elapsed < 3
        # Liveness is unaffected while the database is down (Requirement 7.9).
        assert c.get("/healthz").status_code == 200
    assert SECRET not in response.text
    assert "127.0.0.1" not in response.text


def test_readyz_recovers_on_the_next_request_without_a_restart() -> None:
    results: list[ProbeResult] = [DbUnavailable("db_unreachable"), ready_probe()]

    async def probe() -> ProbeResult:
        return results.pop(0)

    with TestClient(build_app(settings_for(1), probe=probe, expected_head=1)) as c:
        assert c.get("/readyz").status_code == 503
        assert c.get("/readyz").status_code == 200


def test_a_crashing_probe_answers_503_probe_failed_without_exception_text() -> None:
    boom = RuntimeError(f"host=db.internal password={SECRET}")
    with TestClient(app_with(boom)) as c:
        response = c.get("/readyz")
    assert response.status_code == 503
    assert response.json()["checks"]["database"] == {"ok": False, "error": "probe_failed"}
    assert SECRET not in response.text
    assert "db.internal" not in response.text


def test_no_docs_or_schema_endpoints(client: TestClient) -> None:
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404


# --- access log -------------------------------------------------------------------------


def test_probe_endpoints_log_at_debug_and_other_paths_at_info(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG, logger="cairn.api.access")
    client.get("/healthz")
    client.get("/readyz")
    client.get("/nope?token=abc")
    records = [r for r in caplog.records if r.name == "cairn.api.access"]
    assert [(r.path, r.levelno, r.status) for r in records] == [  # type: ignore[attr-defined]
        ("/healthz", logging.DEBUG, 200),
        ("/readyz", logging.DEBUG, 200),
        ("/nope", logging.INFO, 404),
    ]
    first = records[0]
    assert first.method == "GET"  # type: ignore[attr-defined]
    assert isinstance(first.duration_ms, float)  # type: ignore[attr-defined]
    assert "token" not in caplog.text  # the query string is never logged


def test_probe_requests_are_silent_at_info(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="cairn.api.access")
    client.get("/healthz")
    client.get("/readyz")
    assert [r for r in caplog.records if r.name == "cairn.api.access"] == []


def test_unhandled_error_is_logged_as_500_and_re_raised(
    caplog: pytest.LogCaptureFixture,
) -> None:
    app = app_with(ready_probe())

    @app.get("/boom")
    async def boom() -> None:
        raise RuntimeError("kaboom")

    caplog.set_level(logging.INFO, logger="cairn.api.access")
    with TestClient(app, raise_server_exceptions=False) as c:
        assert c.get("/boom").status_code == 500
    [record] = [r for r in caplog.records if r.name == "cairn.api.access"]
    assert record.status == 500  # type: ignore[attr-defined]
    assert record.path == "/boom"  # type: ignore[attr-defined]


# --- spans and trace ids in the log -------------------------------------------------------


def test_each_request_gets_a_server_span_and_the_log_line_carries_its_trace_id() -> None:
    exporter = InMemorySpanExporter()
    provider: TracerProvider = build_tracer_provider("cairn-api", "none")
    provider.add_span_processor(SimpleSpanProcessor(exporter))

    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter("cairn-api"))
    logger = logging.getLogger("cairn.api.access")
    old_level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    try:
        with TestClient(app_with(ready_probe(), tracer_provider=provider)) as c:
            c.get("/healthz")
            c.get("/readyz")
    finally:
        logger.removeHandler(handler)
        logger.setLevel(old_level)

    server_spans = [s for s in exporter.get_finished_spans() if s.kind == SpanKind.SERVER]
    assert len(server_spans) == 2
    lines = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert len(lines) == 2
    for line, span in zip(lines, server_spans, strict=True):
        assert span.context is not None
        assert line["trace_id"] == f"{span.context.trace_id:032x}"
        assert line["path"] in {"/healthz", "/readyz"}
        assert line["service"] == "cairn-api"
        assert line["msg"] == "http.request"


# --- create_app -------------------------------------------------------------------------


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for key in CAIRN_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.chdir(tmp_path)  # no .env here


def test_create_app_exits_2_naming_the_key_when_the_password_is_missing(
    clean_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as exit_info:
        create_app()
    assert exit_info.value.code == 2
    assert "CAIRN_APP_DB_PASSWORD" in capsys.readouterr().err


def test_create_app_exits_2_on_a_malformed_value_without_printing_it(
    clean_env: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("CAIRN_APP_DB_PASSWORD", SECRET)
    monkeypatch.setenv("CAIRN_DB_PORT", "not-a-port")
    with pytest.raises(SystemExit) as exit_info:
        create_app()
    assert exit_info.value.code == 2
    err = capsys.readouterr().err
    assert "CAIRN_DB_PORT" in err
    assert SECRET not in err
    assert "not-a-port" not in err


def test_create_app_wires_logging_tracing_and_psycopg_instrumentation(
    clean_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CAIRN_APP_DB_PASSWORD", SECRET)
    monkeypatch.setenv("CAIRN_DB_PORT", str(closed_port()))
    provider = build_tracer_provider("cairn-api", "none")
    calls: list[tuple[str, tuple[object, ...]]] = []

    class StubInstrumentor:
        def instrument(self, **kwargs: object) -> None:
            calls.append(("psycopg", (kwargs["tracer_provider"],)))

    monkeypatch.setattr(
        api_main,
        "configure_logging",
        lambda service, level: calls.append(("logging", (service, level))),
    )

    def fake_configure_tracing(
        service: str, exporter: str, endpoint: str | None, *, environment: str
    ) -> TracerProvider:
        calls.append(("tracing", (service, exporter, endpoint, environment)))
        return provider

    monkeypatch.setattr(api_main, "configure_tracing", fake_configure_tracing)
    monkeypatch.setattr(api_main, "PsycopgInstrumentor", StubInstrumentor)

    app = create_app()

    assert calls == [
        ("logging", ("cairn-api", "INFO")),
        ("tracing", ("cairn-api", "none", None, "local")),
        ("psycopg", (provider,)),
    ]
    with TestClient(app) as c:
        assert c.get("/healthz").status_code == 200
