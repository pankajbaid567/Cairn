# Task 2.3 notes: Property 5, configuration errors name keys, not values

Date: 27 Oct 2026. Validates Requirement 7.11. No bug found in `cairn_core.config`; `config.py` is unchanged.

## File

`packages/core/tests/unit/test_config_properties.py` (new, 2 tests, tagged `Feature: cairn-phase-0-foundations, Property 5: ...`). Uses the profile `max_examples` from the root `conftest.py` (100 dev, 200 ci).

## Isolation

No fixtures. Each example patches `os.environ` with `unittest.mock.patch.dict`, removes every Cairn alias, sets only the generated environment, and changes into a fresh temporary directory (no `.env`). stdout and stderr are captured with `contextlib.redirect_*`, since `capsys` is function-scoped and Hypothesis does not reset it between examples.

## Generators

- Class: one of `CommonSettings`, `ApiSettings`, `WorkerSettings`, `AdminSettings`.
- Valid base environment: every enum member, ports 1 to 65535, timeouts 0.001 to 30, optional http or https endpoint, 5 or 6 field cron, and random `mk<16 hex>` + arbitrary Unicode text for hosts and every secret (no NUL or surrogates).
- Defects (1 to 4 per example, unique by key):
  - `CAIRN_ENV`, `CAIRN_LOG_LEVEL`, `OTEL_TRACES_EXPORTER`: marker + text (unknown enum value).
  - `CAIRN_DB_PORT`: marker + text, or a signed integer of 7 to 16 digits (out of range).
  - `CAIRN_DB_TIMEOUT_S`: marker + text, or a distinctive float above 30 or below 0.
  - `OTEL_EXPORTER_OTLP_ENDPOINT`: marker + text (malformed URL).
  - `CAIRN_HEARTBEAT_CRON` (worker): marker plus 0 to 3 or 6 to 8 more words (wrong field count).
  - Each required secret of the class: absent, or set to the empty string.

## Assertions

Invalid case: `SystemExit` code 2; stdout empty; stderr is one line starting `invalid configuration: `; the reported key set equals the defect key set exactly (every offending key named, no valid key blamed); no marker-carrying value (bad or valid, secrets included) and no long numeric bad value appears in stderr. Short valid values such as `ci` or `INFO` are deliberately not searched for, since they could match by chance.

Valid case: no exit, an instance of the requested class, empty stdout and stderr.

## Verification

```text
$ uv run pytest packages/core/tests/unit/test_config_properties.py packages/core/tests/unit/test_config.py
26 passed in 0.43s       (--hypothesis-show-statistics: 100 passing, 0 failing, 0 and 1 invalid cases)
$ make types
mypy --strict packages/core: Success (11 files); mypy base: Success (37 files)
$ ruff format --check . / ruff check on my file
48 files already formatted / All checks passed!
```

`make lint` fails with one finding outside my file: RUF100 unused `noqa: BLE001` in `packages/core/tests/unit/test_telemetry_format_properties.py:117` (another agent's work in progress). Not touched.
