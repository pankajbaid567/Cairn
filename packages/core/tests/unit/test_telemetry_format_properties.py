"""Property test for JsonFormatter line format and trace correlation.

Feature: cairn-phase-0-foundations, Property 6: Log lines are well-formed and trace-correlated

**Validates: Requirements 9.1, 9.2**

No network, no global tracer provider, no change to the root logger: records are built by
hand and formatted directly, and spans come from a provider that is never installed.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime
from typing import Any

from hypothesis import given
from hypothesis import strategies as st
from opentelemetry import trace
from opentelemetry.trace import INVALID_SPAN_CONTEXT, NonRecordingSpan

from cairn_core.telemetry import JsonFormatter, build_tracer_provider

TS_RE = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$")
TRACE_ID_RE = re.compile(r"^[0-9a-f]{32}$")
SPAN_ID_RE = re.compile(r"^[0-9a-f]{16}$")
CORE_FIELDS = ("ts", "level", "logger", "msg", "service")
# Extra names that the formatter must keep away from its own fields.
COLLIDING_KEYS = ("ts", "level", "logger", "service", "trace_id", "span_id", "exc", "exc_type")
RECORD_ATTRS = frozenset(logging.makeLogRecord({}).__dict__) | {"message", "asctime", "taskName"}

PROVIDER = build_tracer_provider("svc", "none")
TRACER = PROVIDER.get_tracer("t")

# --- generators ------------------------------------------------------------------------

# Every code point, lone surrogates and control characters included.
any_text = st.text(
    alphabet=st.characters(min_codepoint=0, max_codepoint=0x10FFFF, exclude_categories=()),
    max_size=40,
)
tricky_text = st.one_of(
    any_text,
    st.sampled_from(
        ["", "\n", "\r\n", "a\nb\nc", "\u2028x\u2029", "\x00\x1b[0m", "é\U0001f600", '"\\']
    ),
)


@st.composite
def messages(draw: st.DrawFn) -> tuple[str, tuple[Any, ...] | None]:
    """A message and its %-arguments; the template always matches the arguments."""
    if draw(st.booleans()):
        return draw(tricky_text), None  # no args: '%' in the text is left alone
    parts: list[str] = []
    args: list[Any] = []
    for _ in range(draw(st.integers(1, 4))):
        parts.append(draw(tricky_text).replace("%", "%%"))
        conv = draw(st.sampled_from(["%s", "%r", "%d"]))
        parts.append(conv)
        args.append(draw(st.integers(-(10**9), 10**9)) if conv == "%d" else draw(tricky_text))
    parts.append(draw(tricky_text).replace("%", "%%"))
    return "".join(parts), tuple(args)


odd_leaves = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(),
    st.floats(allow_nan=True, allow_infinity=True),
    tricky_text,
    st.binary(max_size=8),
    st.builds(object),
    st.sampled_from([ValueError("boom"), KeyError("k")]),
)
json_keys = st.one_of(tricky_text, st.integers(), st.sampled_from(COLLIDING_KEYS))
extra_values = st.recursive(
    odd_leaves,
    lambda inner: st.one_of(
        st.lists(inner, max_size=3),
        st.tuples(inner, inner),
        st.frozensets(st.integers(), max_size=3),
        st.dictionaries(json_keys, inner, max_size=3),
    ),
    max_leaves=10,
)
# Deep chain, deeper than the formatter's nesting limit.
deep_values = st.integers(9, 14).map(lambda n: _nest(n))


def _nest(n: int) -> Any:
    value: Any = "leaf"
    for _ in range(n):
        value = {"k": [value]}
    return value


extra_keys = st.one_of(
    st.sampled_from(COLLIDING_KEYS),
    st.sampled_from(["exc_info_extra", "excellent", "request_id", "path", "stack"]),
    tricky_text.filter(bool),
).filter(lambda k: k not in RECORD_ATTRS)
extras = st.dictionaries(extra_keys, st.one_of(extra_values, deep_values), max_size=5)

exception_types = st.sampled_from([ValueError, KeyError, RuntimeError, OSError, ZeroDivisionError])


@st.composite
def exc_infos(draw: st.DrawFn) -> Any:
    if not draw(st.booleans()):
        return None
    exc_type = draw(exception_types)
    try:
        raise exc_type(draw(tricky_text))
    except BaseException as exc:  # building a real traceback on purpose
        return type(exc), exc, exc.__traceback__


@st.composite
def records(draw: st.DrawFn) -> logging.LogRecord:
    msg, args = draw(messages())
    exc_info = draw(exc_infos())
    record = logging.LogRecord(
        name=draw(st.one_of(tricky_text, st.sampled_from(["", "cairn.api", "a.b.c", "root"]))),
        level=draw(st.one_of(st.sampled_from([10, 20, 30, 40, 50]), st.integers(0, 100))),
        pathname=__file__,
        lineno=1,
        msg=msg,
        args=args,
        exc_info=exc_info,
    )
    # Years 1970 to 9999, with sub-millisecond noise.
    record.created = draw(st.floats(0, 253_402_300_799, allow_nan=False, allow_infinity=False))
    for key, value in draw(extras).items():
        record.__dict__[key] = value
    return record


services = st.one_of(tricky_text, st.sampled_from(["cairn-api", "cairn-worker", "svc"]))

# --- checks ----------------------------------------------------------------------------


def check_line(
    line: str, record: logging.LogRecord, service: str, ctx: trace.SpanContext | None
) -> dict[str, Any]:
    """Assert everything Property 6 promises about one formatted line."""
    # One line: json escapes newlines and every non-ASCII separator.
    assert line.isascii()
    assert len(line.splitlines()) == 1
    assert "\n" not in line
    assert "\r" not in line

    out = json.loads(line)
    assert isinstance(out, dict)

    for field in CORE_FIELDS:
        assert field in out
    assert TS_RE.match(out["ts"])
    parsed = datetime.strptime(out["ts"], "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)
    assert 0 <= record.created - parsed.timestamp() < 0.0011
    assert out["level"] == record.levelname
    assert out["logger"] == record.name
    assert out["msg"] == record.getMessage()
    assert out["service"] == service

    # Extras cannot replace core fields; a colliding extra moves to extra_<key>.
    for key in record.__dict__.keys() - RECORD_ATTRS:
        if key in CORE_FIELDS or key in ("trace_id", "span_id") or key.startswith("exc"):
            assert f"extra_{key}" in out

    if ctx is None:
        assert "trace_id" not in out
        assert "span_id" not in out
    else:
        assert TRACE_ID_RE.match(out["trace_id"])
        assert SPAN_ID_RE.match(out["span_id"])
        assert out["trace_id"] == format(ctx.trace_id, "032x")
        assert out["span_id"] == format(ctx.span_id, "016x")

    if record.exc_info and record.exc_info[0] is not None:
        assert out["exc_type"] == record.exc_info[0].__name__
        assert isinstance(out["exc"], str)
    return out


def check_nested(
    formatter: JsonFormatter,
    record: logging.LogRecord,
    depth: int,
    seen: list[trace.SpanContext],
) -> None:
    """Open ``depth`` nested spans; at each level the line carries the innermost span."""
    if depth == 0:
        return
    with TRACER.start_as_current_span(f"level-{depth}") as span:
        ctx = span.get_span_context()
        seen.append(ctx)
        check_line(formatter.format(record), record, formatter.service, ctx)
        check_nested(formatter, record, depth - 1, seen)
        # Back in this span after the inner one closed: ids return to this span.
        check_line(formatter.format(record), record, formatter.service, ctx)


# --- properties ------------------------------------------------------------------------


@given(record=records(), service=services)
def test_line_is_well_formed_without_a_span(record: logging.LogRecord, service: str) -> None:
    """Feature: cairn-phase-0-foundations, Property 6: Log lines are well-formed and
    trace-correlated (no current span: neither trace_id nor span_id)."""
    formatter = JsonFormatter(service)
    check_line(formatter.format(record), record, service, None)
    # An invalid span context counts as no span.
    with trace.use_span(NonRecordingSpan(INVALID_SPAN_CONTEXT)):
        check_line(formatter.format(record), record, service, None)


@given(record=records(), service=services, depth=st.integers(1, 4))
def test_line_carries_the_innermost_span_ids(
    record: logging.LogRecord, service: str, depth: int
) -> None:
    """Feature: cairn-phase-0-foundations, Property 6: Log lines are well-formed and
    trace-correlated (valid span current: ids equal the innermost span's context)."""
    formatter = JsonFormatter(service)
    seen: list[trace.SpanContext] = []
    check_nested(formatter, record, depth, seen)

    assert len(seen) == depth
    assert len({c.trace_id for c in seen}) == 1  # nested spans share one trace
    assert len({c.span_id for c in seen}) == depth  # each has its own span id
    # After every span has closed, the correlation is gone again.
    check_line(formatter.format(record), record, service, None)
