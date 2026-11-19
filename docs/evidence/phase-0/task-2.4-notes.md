# Task 2.4 notes: Property 6 test (log line format and trace correlation)

File: `packages/core/tests/unit/test_telemetry_format_properties.py` (new). No change to `telemetry.py`; no bug found.

## Tests

- `test_line_is_well_formed_without_a_span`: no current span, and a `NonRecordingSpan` with an invalid context. Neither `trace_id` nor `span_id` may appear.
- `test_line_carries_the_innermost_span_ids`: 1 to 4 nested spans from `build_tracer_provider("svc", "none")` (never installed globally). At each level the line carries that span's ids, the ids return to the outer span after the inner one closes, and they disappear once all spans are closed. Nested spans share one trace id and have distinct span ids.

## Asserted per line

ASCII only and `splitlines()` gives one line; valid JSON object; `ts` matches `YYYY-MM-DDTHH:MM:SS.mmmZ` and is within 1.1 ms below `record.created`; `level == record.levelname`, `logger == record.name`, `msg == record.getMessage()`, `service`; `trace_id` is 32 lowercase hex and `span_id` 16, both equal to the span context; extras named like a core field or `exc*` are present as `extra_<key>` and the core fields are unchanged; `exc_type` and `exc` present with exception info.

## Generators

- Text: any code point (lone surrogates and control characters included) mixed with fixed awkward strings (newlines, CRLF, U+2028/2029, ANSI escape, quotes).
- Message: either free text with no args (a bare `%` stays), or a template of `%s`, `%r`, `%d` pieces with matching args and `%%` escapes.
- Logger name: any text, empty, dotted. Level number 0 to 100 (custom numbers give "Level N" names). `created`: 0 to year 9999 with fractional seconds. Service name: any text.
- Extras: up to 5 keys from colliding names (`ts`, `level`, `logger`, `service`, `trace_id`, `span_id`, `exc`, `exc_type`), `exc*` look-alikes, and any text; record attribute names are excluded, as `Logger.makeRecord` forbids them. Values: None, bool, int, NaN and infinity, text, bytes, `object()`, exceptions, and recursive lists, tuples, frozensets and dicts (int and text keys); plus chains nested 9 to 14 levels (past the formatter's limit of 8).
- Exception info: a real raised `ValueError`, `KeyError`, `RuntimeError`, `OSError` or `ZeroDivisionError` with arbitrary message, or none.

`max_examples` comes from the root `conftest.py` profile (dev 100, ci 200), not set in the test.

## Verification

```text
$ uv run pytest packages/core/tests/unit/test_telemetry_format_properties.py packages/core/tests/unit/test_telemetry.py
53 passed        (also with HYPOTHESIS_PROFILE=ci)
$ make fmt-check lint types
fmt-check: 48 files already formatted; lint: All checks passed!
types: 1 error, in packages/core/tests/unit/test_config_properties.py:67 (another agent's file:
       `blacklist_categories` tuple type); none in my file.
```
