# Task 1.4 notes: property test for env generation

Date: 27 Oct 2026. Property 19 (Requirement 22.2). No bug found; `scripts/dev_env.py` is unchanged. Dependencies and uv.lock unchanged.

## File

`tests/scripts/test_dev_env_properties.py`, one Hypothesis test: `test_env_generation_fills_secrets_and_keeps_everything_else`. It uses the profile from the root `conftest.py` (100 examples on dev, 200 on ci) and sets no `max_examples` of its own.

## Generators

Each generated line carries the output it must produce, so the expectation does not rely on the parser under test.

| Strategy | Produces |
|---|---|
| `comment_lines` | `#` plus printable ASCII (may contain `change-me` or `KEY=change-me`), optionally indented |
| `blank_lines` | empty or whitespace-only lines |
| `plain_lines` | `[export ]KEY<spacing>value[rest]` with unquoted values (not `change-me`), single or double quoted values (not `change-me`), and look-alikes: `change-me-later` (bare and quoted), `Change-Me`, `change_me`, `change-me extra`, `"change-me"x`, `'change-me`, an empty value with a `# change-me` comment |
| `secret_lines` | `[export ]KEY<spacing>` plus `change-me`, `"change-me"` or `'change-me'`, then nothing, spaces, or a `# comment` |
| template | 0 to 25 lines of any kind, then one non-blank last line, with or without a final newline |

Keys match `[A-Za-z_][A-Za-z0-9_]{0,15}`. Spacing around `=` varies. Text is printable ASCII only, because `str.splitlines` also splits on `\x0b`, `\x1c` and Unicode separators. The last line is never blank so the template round-trips through `splitlines`. The token source is a counter (`token-0000000000000000000001`, ...), wrapped in a call counter.

## What is checked

- The token source is called once per secret line and no more, so tokens come out in file order.
- `render(...).splitlines()` equals the expected lines: same line count, secret lines are `before + token + after`, every other line is byte-for-byte unchanged (comments, blanks, keys, non-secret values, look-alikes).
- The output ends with exactly one newline.
- No placeholder remains where a secret was: `secret_keys(output) == []`, each secret line holds a token, and the text before the trailing comment has no `change-me`.
- `secret_keys(template)` equals the keys of the lines generated as secrets, in order.

## Result

```text
$ uv run pytest tests/scripts/test_dev_env_properties.py tests/scripts/test_dev_env.py
20 passed in 0.17s
$ HYPOTHESIS_PROFILE=ci uv run pytest tests/scripts/test_dev_env_properties.py
200 passing, 0 failing, 7 invalid test cases (filter retries only)
$ make fmt-check lint types
33 files already formatted / All checks passed!
mypy --strict packages/core: no issues in 6 source files
mypy base: no issues in 27 source files
```

## Notes

- The Hypothesis feature tag is a `# noqa: E501` comment near the top of the file, because the line is over the 100 character ruff limit. The test docstring points to it.
- The `.env.example` check in `test_dev_env.py` stays the pin for the real template; this property covers arbitrary templates.
