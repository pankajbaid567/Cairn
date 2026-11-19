"""Structured JSON logging and OpenTelemetry tracing (design §9, requirements 9.1 to 9.5).

Importing this module changes nothing global. ``JsonFormatter``, ``RedactionFilter``,
``redact_fields`` and ``build_tracer_provider`` are side-effect free and can be tested in
isolation. Only ``configure_logging`` and ``configure_tracing`` touch process-wide state.

Typical use in a service entry point::

    settings = load_settings(ApiSettings)
    configure_logging("cairn-api", settings.log_level)
    configure_tracing("cairn-api", settings.otel_traces_exporter, str(settings.otel_endpoint))
"""

from __future__ import annotations

import json
import logging
import math
import re
import sys
from collections.abc import Mapping
from datetime import UTC, datetime
from importlib import metadata
from typing import Any, Final, Literal, TextIO

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    ConsoleSpanExporter,
    SimpleSpanProcessor,
    SpanExporter,
)
from opentelemetry.sdk.trace.sampling import ALWAYS_ON, ParentBased

__all__ = [
    "REDACTED",
    "JsonFormatter",
    "RedactionFilter",
    "TraceExporter",
    "build_tracer_provider",
    "configure_logging",
    "configure_tracing",
    "is_secret_key",
    "redact_fields",
]

TraceExporter = Literal["none", "otlp", "console"]

REDACTED: Final = "[REDACTED]"

# Compound key names that mean a credential. Matched as substrings of the key after
# lower-casing and removing every non-alphanumeric character, so "API_KEY", "api-key",
# "apiKey" and "Api Key" all become "apikey".
_SECRET_SUBSTRINGS: Final = (
    "password",
    "passwd",
    "passphrase",
    "pwd",
    "secret",
    "token",
    "authorization",
    "credential",
    "cookie",
    "dsn",
    "apikey",
    "accesskey",
    "privatekey",
    "signingkey",
    "encryptionkey",
    "authkey",
)
# Words that are secret-like only as a whole word, because as substrings they hit
# innocent names ("bypass", "passed", "monkey").
_SECRET_WORDS: Final = frozenset({"pass", "key"})
# Word boundaries: any non-alphanumeric run, a lower/digit-to-upper step ("apiKey"), and the
# end of an uppercase run before a capitalized word ("APIKey" -> "API", "Key"; "XPass" ->
# "X", "Pass").
_WORD_SPLIT_RE: Final = re.compile(r"[^A-Za-z0-9]+|(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
_NON_ALNUM_RE: Final = re.compile(r"[^a-z0-9]")

# Attributes every LogRecord has, plus the ones Formatter adds. Anything else on a
# record came from ``extra=`` and is emitted as a field.
_RECORD_ATTRS: Final = frozenset(logging.makeLogRecord({}).__dict__) | {
    "message",
    "asctime",
    "taskName",
}
_CORE_FIELDS: Final = ("ts", "level", "logger", "msg", "service", "trace_id", "span_id")
_MAX_DEPTH: Final = 8
_TRUNCATED: Final = "[TRUNCATED]"
_HANDLER_MARK: Final = "_cairn_json_handler"


def is_secret_key(key: object) -> bool:
    """True when ``key`` names a credential, in any case or separator style.

    Matches password, passwd, pwd, secret, token, authorization, credential, cookie, dsn,
    api/access/private/signing/encryption key, the whole words "pass" and "key", and any
    key that contains one of those names (``db_password``, ``refresh_token``).
    """
    text = str(key)
    squashed = _NON_ALNUM_RE.sub("", text.lower())
    if any(s in squashed for s in _SECRET_SUBSTRINGS):
        return True
    words = {w.lower() for w in _WORD_SPLIT_RE.split(text) if w}
    return bool(words & _SECRET_WORDS)


def redact_fields(fields: Mapping[str, Any]) -> dict[str, Any]:
    """Return a copy of ``fields`` with the value of every secret-like key replaced.

    Nested mappings are redacted recursively. The input is never modified.
    """
    return {k: _redact_value(k, v, 0) for k, v in fields.items()}


def _redact_value(key: object, value: Any, depth: int) -> Any:
    if is_secret_key(key):
        return REDACTED
    if isinstance(value, Mapping | list | tuple | set | frozenset):
        if depth >= _MAX_DEPTH:
            # Not scanned, so not passed through either: a secret key below the limit
            # must not survive into a handler that formats records differently.
            return _TRUNCATED
        if isinstance(value, Mapping):
            return {str(k): _redact_value(k, v, depth + 1) for k, v in value.items()}
        return [_redact_value(key, v, depth + 1) for v in value]
    return value


def _to_jsonable(value: Any, depth: int = 0) -> Any:
    """Convert ``value`` to something ``json.dumps`` accepts without ``default``."""
    if value is None or isinstance(value, bool | int | str):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if depth >= _MAX_DEPTH:
        return _TRUNCATED
    if isinstance(value, Mapping):
        return {str(k): _to_jsonable(v, depth + 1) for k, v in value.items()}
    if isinstance(value, list | tuple | set | frozenset):
        return [_to_jsonable(v, depth + 1) for v in value]
    return str(value)


class RedactionFilter(logging.Filter):
    """Replaces the value of every secret-like ``extra`` field on a record.

    ``JsonFormatter`` redacts on its own, so this filter is a second line of defence for
    handlers that format records differently. It edits the record in place and never
    drops a record.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        for key in [k for k in record.__dict__ if k not in _RECORD_ATTRS]:
            record.__dict__[key] = _redact_value(key, record.__dict__[key], 0)
        return True


class JsonFormatter(logging.Formatter):
    """One JSON object per line.

    Fields: ``ts`` (RFC 3339, UTC, milliseconds), ``level``, ``logger``, ``msg``, ``service``;
    ``trace_id`` (32 hex) and ``span_id`` (16 hex) while a valid span is current; then any
    ``extra`` fields with secret-like keys replaced by ``"[REDACTED]"``. A record with
    exception info adds ``exc_type`` and ``exc``. An ``extra`` key that collides with a
    core field is kept under ``extra_<key>``.
    """

    def __init__(self, service: str) -> None:
        super().__init__()
        self.service = service

    def format(self, record: logging.LogRecord) -> str:
        ts = datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds")
        out: dict[str, Any] = {
            "ts": ts.replace("+00:00", "Z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "service": self.service,
        }
        ctx = trace.get_current_span().get_span_context()
        if ctx.is_valid:
            out["trace_id"] = format(ctx.trace_id, "032x")
            out["span_id"] = format(ctx.span_id, "016x")

        extras = {k: v for k, v in record.__dict__.items() if k not in _RECORD_ATTRS}
        for key, value in redact_fields(extras).items():
            out[f"extra_{key}" if key in _CORE_FIELDS or key.startswith("exc") else key] = value

        if record.exc_info and record.exc_info[0] is not None:
            out["exc_type"] = record.exc_info[0].__name__
            out["exc"] = self.formatException(record.exc_info)
        elif record.exc_text:
            out["exc"] = record.exc_text
        if record.stack_info:
            out["stack"] = self.formatStack(record.stack_info)

        # ensure_ascii keeps the line pure ASCII; json escapes newlines inside strings.
        return json.dumps(_to_jsonable(out), ensure_ascii=True, allow_nan=False)


def configure_logging(
    service: str, level: str = "INFO", *, stream: TextIO | None = None
) -> logging.Handler:
    """Send all stdlib logging to ``stream`` (default stdout) as JSON lines.

    Idempotent: a handler installed by an earlier call is replaced, so calling it twice
    never duplicates output. Returns the installed handler.
    """
    numeric = logging.getLevelNamesMapping().get(level.upper())
    if numeric is None:
        raise ValueError(f"unknown log level: {level!r}")
    handler = logging.StreamHandler(stream if stream is not None else sys.stdout)
    handler.setFormatter(JsonFormatter(service))
    handler.addFilter(RedactionFilter())
    setattr(handler, _HANDLER_MARK, True)

    root = logging.getLogger()
    for old in [h for h in root.handlers if getattr(h, _HANDLER_MARK, False)]:
        root.removeHandler(old)
    root.addHandler(handler)
    root.setLevel(numeric)
    return handler


def _service_version() -> str:
    try:
        return metadata.version("cairn-core")
    except metadata.PackageNotFoundError:
        return "unknown"


def _otlp_traces_endpoint(endpoint: str) -> str:
    """OTLP/HTTP signal URL for a base endpoint such as ``http://otel-collector:4318``.

    An explicit ``endpoint=`` argument is used verbatim by the exporter, unlike the
    ``OTEL_EXPORTER_OTLP_ENDPOINT`` variable, so the signal path is added here.
    """
    base = endpoint.rstrip("/")
    return base if base.endswith("/v1/traces") else f"{base}/v1/traces"


def build_tracer_provider(
    service: str,
    exporter: TraceExporter,
    endpoint: str | None = None,
    *,
    environment: str = "local",
    console_stream: TextIO | None = None,
) -> TracerProvider:
    """Build a tracer provider without installing it globally.

    The provider always exists, so spans and trace ids work with nothing exported.
    ``"none"`` adds no span processor; ``"otlp"`` batches spans to OTLP HTTP at
    ``endpoint`` (the exporter's own default when ``endpoint`` is None); ``"console"``
    writes each finished span to ``console_stream`` (default stdout).
    """
    resource = Resource.create(
        {
            "service.name": service,
            "service.version": _service_version(),
            "deployment.environment": environment,
        }
    )
    provider = TracerProvider(resource=resource, sampler=ParentBased(ALWAYS_ON))
    if exporter == "none":
        return provider
    if exporter == "otlp":
        otlp = (
            OTLPSpanExporter(endpoint=_otlp_traces_endpoint(endpoint))
            if endpoint
            else OTLPSpanExporter()
        )
        provider.add_span_processor(BatchSpanProcessor(otlp))
    elif exporter == "console":
        span_exporter: SpanExporter = ConsoleSpanExporter(
            out=console_stream if console_stream is not None else sys.stdout
        )
        provider.add_span_processor(SimpleSpanProcessor(span_exporter))
    else:
        raise ValueError(f"unknown trace exporter: {exporter!r}")
    return provider


def configure_tracing(
    service: str,
    exporter: TraceExporter,
    endpoint: str | None = None,
    *,
    environment: str = "local",
) -> TracerProvider:
    """Install the process-wide tracer provider and return it.

    OpenTelemetry allows the global provider to be set once per process. A second call
    returns the new provider but leaves the first one installed (and logs a warning).
    """
    provider = build_tracer_provider(service, exporter, endpoint, environment=environment)
    trace.set_tracer_provider(provider)
    return provider
