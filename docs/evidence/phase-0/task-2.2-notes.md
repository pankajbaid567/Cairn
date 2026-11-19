# Task 2.2 notes: `cairn_core.telemetry`

Date: 27 Oct 2026. Dependencies and uv.lock unchanged (`uv lock --check` passes, 118 packages).

## Files

| File | State |
|---|---|
| `packages/core/src/cairn_core/telemetry.py` | New |
| `packages/core/tests/unit/test_telemetry.py` | New, 51 test cases (parametrized) |

## Public interface

| Name | What it does | Global side effects |
|---|---|---|
| `JsonFormatter(service)` | `LogRecord` to one JSON line: `ts` (RFC 3339 UTC, ms, `Z`), `level`, `logger`, `msg`, `service`; `trace_id` (32 hex) and `span_id` (16 hex) while a valid span is current; then extras; `exc_type` and `exc` for exception info | none |
| `redact_fields(mapping)`, `is_secret_key(key)` | Pure helpers behind the redaction. Nested mappings and lists are redacted recursively; the input is not modified | none |
| `RedactionFilter` | `logging.Filter` that replaces secret-like extras on the record in place and never drops a record | none until attached |
| `build_tracer_provider(service, exporter, endpoint=None, *, environment="local", console_stream=None)` | Builds a `TracerProvider` (resource `service.name`, `service.version`, `deployment.environment`; `ParentBased(ALWAYS_ON)`). `none`: no processor. `otlp`: `BatchSpanProcessor(OTLPSpanExporter)`. `console`: `SimpleSpanProcessor(ConsoleSpanExporter)` | none |
| `configure_logging(service, level="INFO", *, stream=None)` | Installs one JSON handler on the root logger (stdout by default), with the redaction filter. Idempotent. Unknown level raises `ValueError` before anything changes | root logger |
| `configure_tracing(service, exporter, endpoint=None, *, environment="local")` | `build_tracer_provider` plus `trace.set_tracer_provider`. Returns the provider | global tracer provider |

`TraceExporter = Literal["none", "otlp", "console"]` matches `CommonSettings.otel_traces_exporter` in design §7.

## Behavior worth knowing

- Redaction is by key, after lower-casing and dropping separators, so `API_KEY`, `api-key`, `apiKey` and `Api Key` all match. Substrings: password, passwd, passphrase, pwd, secret, token, authorization, credential, cookie, dsn, apikey, accesskey, privatekey, signingkey, encryptionkey, authkey. Whole words: `pass` and `key`.
- Message text is not scanned. Only field values are redacted, as in Requirement 9.3. Never put a secret in the format string or arguments.
- An extra that collides with a core field or starts with `exc` is kept as `extra_<key>`, so a caller cannot forge `service`, `ts` or `trace_id`.
- Non-JSON values (bytes, objects) become `str(value)`, NaN and infinity become strings, and nesting deeper than 8 becomes `"[TRUNCATED]"`. Output is ASCII (`ensure_ascii`), so newlines and non-ASCII text stay on one line.
- OTLP: an explicit `endpoint=` is used verbatim by the exporter, unlike the `OTEL_EXPORTER_OTLP_ENDPOINT` variable. `build_tracer_provider` therefore appends `/v1/traces` to a base URL (`http://otel-collector:4318` or with a trailing slash; a URL already ending in `/v1/traces` is kept). With `endpoint=None` the exporter's own defaults apply (the env variable, else `localhost:4318`).
- OpenTelemetry allows setting the global provider once per process. A second `configure_tracing` call returns its new provider but the first stays installed, and OTel logs a warning.

## Verification

```text
$ uv run pytest packages/core/tests/unit/test_telemetry.py
51 passed
$ uv run ruff format --check packages/core / uv run ruff check packages/core
6 files already formatted / All checks passed!
$ make types
mypy --strict packages/core: no issues in 6 source files
mypy base: no issues in 18 source files
$ make test-unit
110 passed
$ make lint
2 errors, both E501 in scripts/check_docs.py (lines 262 and 523), not mine
```

`make lint` fails only because of `scripts/check_docs.py`, another agent's work in progress. My files are clean under ruff.

## Tests

- Formatter: core fields, `%` arguments, multi-line and non-ASCII message, odd extras, collision renaming, exception info, trace ids inside and outside a span, a non-recording span.
- Redaction: 22 secret key spellings, 8 ordinary keys that must not match (`role_bypasses_rls`, `passed`, `bypass`, `monkey`), nested values, input not mutated, filter behavior.
- `configure_logging`: JSON on a stream, idempotent, level respected, bad level leaves handlers alone. The root logger is restored after each test.
- Tracing, with no network and no global provider: `none` adds no exporter and spans still carry valid ids and the resource; `otlp` is checked through a stand-in for `OTLPSpanExporter` that is an `InMemorySpanExporter` and records the endpoint (base URL, trailing slash, full traces URL, none); a real `OTLPSpanExporter` is also built and shut down with no spans, so nothing is sent; `console` writes the span JSON to a `StringIO`; an unknown exporter raises; `configure_tracing` is checked with `trace.set_tracer_provider` patched.

## Deviations from the design

- `JsonFormatter` takes only the service name; it has no `SECRET_KEY_PATTERN` constant. The design's regex `pass(word)?|secret|…` would match "bypass" and "passed" (for example `role_bypasses_rls` from the readiness probe), so redaction is done by `is_secret_key` as described above. It is broader than Requirement 9.3's list (adds credential, cookie, access/private/signing key, whole-word `key`) and does not match plain `pass` inside other words.
- Added `build_tracer_provider` (the design has only `configure_tracing`), so providers can be tested without touching the global state, plus the keyword arguments `environment`, `console_stream` and `stream`.
- The design's `configure_tracing` takes `endpoint: str | None`; callers pass `str(settings.otel_endpoint)`, which may carry a trailing slash (handled).

## Notes for later tasks

- Task 2.4 and 2.5 (property tests): format a `LogRecord` directly with `JsonFormatter("svc").format(record)`; for trace correlation use `build_tracer_provider("svc", "none").get_tracer("t").start_as_current_span(...)`, which needs no global provider. For redaction, call `is_secret_key` or `redact_fields` for the pure rule, or the formatter for the end-to-end line. Hypothesis-generated keys with unusual characters work, since keys go through `str()`.
- 5.1 (api): call at startup, after `load_settings` and before the app is built:

  ```python
  configure_logging("cairn-api", settings.log_level)
  configure_tracing("cairn-api", settings.otel_traces_exporter,
                    str(settings.otel_endpoint) if settings.otel_endpoint else None,
                    environment=settings.env)
  ```

  Instrument FastAPI and psycopg after `configure_tracing`, so they pick up the global provider. Pass uvicorn `--log-config none --no-access-log` as in design §7; uvicorn's loggers then propagate to the root JSON handler.
- 6.1 (worker): same two calls with `"cairn-worker"`. The heartbeat span can use `trace.get_tracer(__name__)`. DBOS config keeps `enable_otlp: False`.
- Log with `logger.info("event", extra={...})` and keep field names descriptive. A field named `key`, `token` or `dsn` is redacted, so use `idempotency_id` or similar for non-secret identifiers.
- The CLI (`cairn-db`, 3.x) can call `configure_logging("cairn-db", level)` too. Bootstrap must still avoid logging plaintext passwords in messages, since the filter cannot see inside message text.
- In tests that call `configure_logging`, restore the root logger afterwards, as `clean_root_logger` does in `test_telemetry.py`.
