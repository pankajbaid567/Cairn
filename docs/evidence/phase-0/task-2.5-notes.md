# Task 2.5 notes: Property 7, log redaction

Date: 27 Oct 2026. Dependencies and `uv.lock` unchanged.

## Files

| File | State |
|---|---|
| `packages/core/tests/unit/test_telemetry_redaction_properties.py` | New, 7 properties and 1 parametrized example test (13 cases) |
| `packages/core/src/cairn_core/telemetry.py` | Fixed one bug (below) |

## Generators

- `secret_keys`: 20 secret names (password, passwd, passphrase, pwd, secret, token, authorization, credential(s), cookie, dsn, apikey, api/access/private/signing/encryption/auth key, whole words `pass` and `key`), 0 to 2 prefix and suffix words, spelled with a random separator (`_ - . __ space / :` or none), per-word lower/upper/capitalized case, camelCase or PascalCase. `pass` and `key` never use an empty separator, since the module matches them as whole words only.
- `plain_keys`: 1 to 3 ordinary words, same spelling rules. The vocabulary includes the lookalikes `bypasses`, `rls`, `passed`, `bypass`, `bypassed`, `monkey`, `turkey`, `keyboard`, `passenger`, `passport`, `compass`, so `role_bypasses_rls` and `ROLE_BYPASSES_RLS` appear in generated keys.
- `trees`: dicts, lists and tuples, three levels of wrapping under the top-level extras dict. A secret key holds a payload (string, list or dict containing `SECRETVAL_` markers) and is expected as `[REDACTED]`. Other values are `plainval_` strings, ints, bools or None and are expected unchanged. Each strategy yields `(logged value, expected value)`, so no test asks `is_secret_key` what to expect.

## Properties

1. Formatted line: one line, no `SECRETVAL_` anywhere, the output fields are exactly the core fields plus the extras, and each equals its expected value.
2. `redact_fields` equals the expected structure, and the input is deep-equal to a copy taken before the call.
3. `RedactionFilter` returns True, leaves every standard `LogRecord` attribute alone, rewrites only extras to the expected structure, is idempotent, and does not change the formatter output.
4. Ordinary keys only: nothing is redacted by `redact_fields`, the filter or the formatter.
5. `is_secret_key` is True for every generated secret spelling and False for every generated ordinary key.
6. Nesting 0 to 14 levels deep: a secret below the depth limit never survives in `redact_fields`, the filter or the formatter.

## Bug found and fixed

`redact_fields` (and so `RedactionFilter`) stopped descending at depth 8 and returned the remaining containers untouched, so `{"payload": <8 levels>{"password": "x"}}` kept the password. The JSON formatter was not affected, because `_to_jsonable` truncates at a shallower depth, but a handler that used `RedactionFilter` with a different formatter would have leaked it. Counterexample from Hypothesis: `levels=8, marker='SECRETVAL_00000000'`.

Fix: at the depth limit `_redact_value` returns `"[TRUNCATED]"` instead of the container, the same marker the formatter uses. Output lines are unchanged for all depths the formatter already handled; existing tests stay green. A shared `_TRUNCATED` constant replaces the literal in `_to_jsonable`.

## Verification

```text
$ uv run pytest packages/core/tests/unit/test_telemetry_redaction_properties.py packages/core/tests/unit/test_telemetry.py
64 passed
$ HYPOTHESIS_PROFILE=ci uv run pytest packages/core/tests/unit
134 passed
$ make fmt-check lint types
49 files already formatted / All checks passed! / mypy strict: no issues in 12 source files / mypy base: no issues in 37 source files
```

`max_examples` comes from the root `conftest.py` profiles (dev 100, ci 200); the tests do not override it.
