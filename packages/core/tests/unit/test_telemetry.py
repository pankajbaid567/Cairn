"""Unit tests for cairn_core.telemetry. No network, no global tracer provider."""

from __future__ import annotations

import io
import json
import logging
import sys
from collections.abc import Iterator
from typing import Any

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from cairn_core import telemetry
from cairn_core.telemetry import (
    REDACTED,
    JsonFormatter,
    RedactionFilter,
    build_tracer_provider,
    configure_logging,
    configure_tracing,
    is_secret_key,
    redact_fields,
)


def make_record(msg: str = "hello", **extra: Any) -> logging.LogRecord:
    record = logging.LogRecord("cairn.test", logging.INFO, __file__, 1, msg, None, None)
    for key, value in extra.items():
        setattr(record, key, value)
    return record


def format_line(record: logging.LogRecord, service: str = "cairn-test") -> dict[str, Any]:
    line = JsonFormatter(service).format(record)
    assert "\n" not in line
    parsed = json.loads(line)
    assert isinstance(parsed, dict)
    return parsed


# --- JsonFormatter ---------------------------------------------------------------------


def test_core_fields_and_timestamp() -> None:
    out = format_line(make_record("started"))
    assert out["msg"] == "started"
    assert out["level"] == "INFO"
    assert out["logger"] == "cairn.test"
    assert out["service"] == "cairn-test"
    assert out["ts"].endswith("Z")
    assert "trace_id" not in out
    assert "span_id" not in out


def test_message_arguments_are_interpolated() -> None:
    record = logging.LogRecord("x", logging.WARNING, __file__, 1, "n=%d", (3,), None)
    assert format_line(record)["msg"] == "n=3"


def test_multiline_and_non_ascii_message_stays_one_line() -> None:
    out = format_line(make_record("line one\nline two é 日本"))
    assert out["msg"] == "line one\nline two é 日本"


def test_extra_fields_are_emitted_and_odd_values_are_stringified() -> None:
    out = format_line(make_record(route="/readyz", duration_ms=1.5, blob=b"x", bad=float("nan")))
    assert out["route"] == "/readyz"
    assert out["duration_ms"] == 1.5
    assert out["blob"] == "b'x'"
    assert out["bad"] == "nan"


def test_extra_that_collides_with_a_core_field_is_renamed() -> None:
    out = format_line(make_record("real", service="spoofed", ts="spoofed"))
    assert out["service"] == "cairn-test"
    assert out["extra_service"] == "spoofed"
    assert out["extra_ts"] == "spoofed"


def test_exception_info_adds_type_and_traceback() -> None:
    try:
        raise ValueError("boom")
    except ValueError:
        record = logging.LogRecord("x", logging.ERROR, __file__, 1, "failed", None, sys.exc_info())
    out = format_line(record)
    assert out["exc_type"] == "ValueError"
    assert "ValueError: boom" in out["exc"]


def test_trace_ids_come_from_the_current_span() -> None:
    provider = build_tracer_provider("cairn-test", "none")
    with provider.get_tracer("t").start_as_current_span("op") as span:
        ctx = span.get_span_context()
        out = format_line(make_record())
    assert out["trace_id"] == format(ctx.trace_id, "032x")
    assert out["span_id"] == format(ctx.span_id, "016x")
    assert len(out["trace_id"]) == 32
    assert len(out["span_id"]) == 16
    # Outside the span the ids disappear again.
    assert "trace_id" not in format_line(make_record())


def test_non_recording_span_context_adds_no_ids() -> None:
    with trace.use_span(trace.NonRecordingSpan(trace.INVALID_SPAN_CONTEXT)):
        assert "trace_id" not in format_line(make_record())


# --- redaction -------------------------------------------------------------------------


@pytest.mark.parametrize(
    "key",
    [
        "password",
        "PASSWORD",
        "db_password",
        "CAIRN_APP_DB_PASSWORD",
        "secret",
        "client-secret",
        "token",
        "refreshToken",
        "access_token",
        "authorization",
        "Authorization",
        "api_key",
        "api-key",
        "apiKey",
        "API KEY",
        "private_key",
        "dsn",
        "database_dsn",
        "pass",
        "key",
        "passwd",
        "credentials",
        "XPass",
        "APIKey",
        "myAPIKey",
        "DBPass",
    ],
)
def test_secret_keys_are_detected(key: str) -> None:
    assert is_secret_key(key)


@pytest.mark.parametrize(
    "key",
    [
        "route",
        "role_bypasses_rls",
        "passed",
        "bypass",
        "status",
        "trace_id",
        "user",
        "monkey",
        "passenger",
        "Passenger",
        "keyboard",
        "XPassenger",
        "RLSBypasses",
    ],
)
def test_ordinary_keys_are_not_secret(key: str) -> None:
    assert not is_secret_key(key)


def test_formatter_redacts_secret_extras_and_hides_values() -> None:
    record = make_record(
        "connecting",
        password="hunter2",
        api_key="sk-live-123",
        nested={"Authorization": "Bearer abc", "ok": 1},
        items=[{"token": "t0k"}],
        route="/x",
    )
    line = JsonFormatter("svc").format(record)
    out = json.loads(line)
    assert out["password"] == REDACTED
    assert out["api_key"] == REDACTED
    assert out["nested"] == {"Authorization": REDACTED, "ok": 1}
    assert out["items"] == [{"token": REDACTED}]
    assert out["route"] == "/x"
    for leaked in ("hunter2", "sk-live-123", "Bearer abc", "t0k"):
        assert leaked not in line


def test_redact_fields_does_not_modify_its_input() -> None:
    fields = {"password": "p", "a": {"token": "t"}}
    assert redact_fields(fields) == {"password": REDACTED, "a": {"token": REDACTED}}
    assert fields == {"password": "p", "a": {"token": "t"}}


def test_redaction_filter_edits_extras_only_and_keeps_the_record() -> None:
    record = make_record("m", secret="s3", route="/r")
    assert RedactionFilter().filter(record) is True
    assert record.__dict__["secret"] == REDACTED
    assert record.__dict__["route"] == "/r"
    assert record.getMessage() == "m"


# --- configure_logging -----------------------------------------------------------------


@pytest.fixture
def clean_root_logger() -> Iterator[logging.Logger]:
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    yield root
    root.handlers[:] = handlers
    root.setLevel(level)


def test_configure_logging_writes_json_lines_and_is_idempotent(
    clean_root_logger: logging.Logger,
) -> None:
    first, second = io.StringIO(), io.StringIO()
    configure_logging("cairn-test", "debug", stream=first)
    configure_logging("cairn-test", "INFO", stream=second)

    logging.getLogger("cairn.demo").info("once", extra={"password": "pw", "k": 1})
    logging.getLogger("cairn.demo").debug("filtered out by level")

    assert first.getvalue() == ""
    lines = second.getvalue().splitlines()
    assert len(lines) == 1
    out = json.loads(lines[0])
    assert (out["msg"], out["service"], out["k"]) == ("once", "cairn-test", 1)
    assert out["password"] == REDACTED
    assert sum(getattr(h, "_cairn_json_handler", False) for h in clean_root_logger.handlers) == 1


def test_configure_logging_rejects_unknown_level(clean_root_logger: logging.Logger) -> None:
    before = list(clean_root_logger.handlers)
    with pytest.raises(ValueError, match="log level"):
        configure_logging("cairn-test", "LOUD")
    assert clean_root_logger.handlers == before


# --- tracing ---------------------------------------------------------------------------


class RecordingOtlpExporter(InMemorySpanExporter):
    """Stands in for OTLPSpanExporter: remembers its endpoint and sends nothing."""

    instances: list[RecordingOtlpExporter] = []  # noqa: RUF012

    def __init__(self, endpoint: str | None = None) -> None:
        super().__init__()
        self.endpoint = endpoint
        RecordingOtlpExporter.instances.append(self)


@pytest.fixture
def fake_otlp(monkeypatch: pytest.MonkeyPatch) -> type[RecordingOtlpExporter]:
    RecordingOtlpExporter.instances = []
    monkeypatch.setattr(telemetry, "OTLPSpanExporter", RecordingOtlpExporter)
    return RecordingOtlpExporter


def emit_span(provider: TracerProvider, name: str = "op") -> None:
    with provider.get_tracer("t").start_as_current_span(name):
        pass


def test_none_installs_a_provider_that_exports_nothing(
    fake_otlp: type[RecordingOtlpExporter],
) -> None:
    provider = build_tracer_provider("cairn-api", "none", "http://collector:4318", environment="ci")
    memory = InMemorySpanExporter()
    # A processor added by the test sees spans; the provider added none of its own.
    provider.add_span_processor(SimpleSpanProcessor(memory))
    emit_span(provider)
    spans = memory.get_finished_spans()
    assert [s.name for s in spans] == ["op"]
    assert spans[0].context is not None
    assert spans[0].context.is_valid
    assert fake_otlp.instances == []
    attrs = provider.resource.attributes
    assert attrs["service.name"] == "cairn-api"
    assert attrs["deployment.environment"] == "ci"
    assert "service.version" in attrs


def test_otlp_exports_to_the_collector_traces_url(fake_otlp: type[RecordingOtlpExporter]) -> None:
    provider = build_tracer_provider("cairn-api", "otlp", "http://otel-collector:4318/")
    emit_span(provider)
    provider.force_flush()
    provider.shutdown()
    [exporter] = fake_otlp.instances
    assert exporter.endpoint == "http://otel-collector:4318/v1/traces"
    assert [s.name for s in exporter.get_finished_spans()] == ["op"]


def test_otlp_endpoint_already_a_traces_url_is_kept(
    fake_otlp: type[RecordingOtlpExporter],
) -> None:
    provider = build_tracer_provider("s", "otlp", "http://h:4318/v1/traces")
    provider.shutdown()
    assert fake_otlp.instances[0].endpoint == "http://h:4318/v1/traces"


def test_otlp_without_endpoint_uses_the_exporter_default(
    fake_otlp: type[RecordingOtlpExporter],
) -> None:
    build_tracer_provider("s", "otlp").shutdown()
    assert fake_otlp.instances[0].endpoint is None


def test_real_otlp_exporter_is_built_without_sending_anything() -> None:
    provider = build_tracer_provider("s", "otlp", "http://127.0.0.1:9")
    provider.shutdown()  # no spans were created, so nothing is exported


def test_console_writes_finished_spans_to_the_stream() -> None:
    out = io.StringIO()
    provider = build_tracer_provider("cairn-worker", "console", console_stream=out)
    emit_span(provider, "worker.heartbeat")
    parsed = json.loads(out.getvalue())
    assert parsed["name"] == "worker.heartbeat"
    assert parsed["resource"]["attributes"]["service.name"] == "cairn-worker"


def test_unknown_exporter_is_rejected() -> None:
    with pytest.raises(ValueError, match="trace exporter"):
        build_tracer_provider("s", "zipkin")  # type: ignore[arg-type]


def test_configure_tracing_installs_the_provider_globally(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    installed: list[Any] = []
    monkeypatch.setattr(trace, "set_tracer_provider", installed.append)
    provider = configure_tracing("cairn-api", "none")
    assert installed == [provider]
    assert isinstance(provider, TracerProvider)
