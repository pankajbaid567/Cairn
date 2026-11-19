# Task 3.4 notes: property test for bootstrap password hygiene

Property 4: Bootstrap never exposes plaintext passwords (Requirements 5.7, 5.8). No dependency changes. No bug found in `bootstrap.py` or `cli.py`; no source file touched.

File: `packages/core/tests/unit/test_bootstrap_properties.py` (8 tests, tagged in the module docstring).

## Generators

- Password: printable ASCII (codepoints 32 to 126) pieces plus nasty pieces (`'`, `''`, `"`, `\`, `\'`, `$`, `$$`, `$1`, `%`, `%s`, `%(x)s`, `{}`, `{0}`, space, `;`, `--`), shuffled with a marker `~mk-<16 hex>`. The marker is at least 20 characters and `~` is not a base64 character, so it cannot occur in a verifier or in fixed SQL. Assertions check the whole password and the marker.
- `Secrets`: owner, app, DBOS and superuser passwords, unique by marker.
- Encryptors: `fake` (well-formed fixed verifier), `python` (`ScramEncryptor(None)`), `libpq` (a stub `pgconn.encrypt_password`), `libpq_broken` (raises `OperationalError` with the password in its text, which must fall back to Python without leaking).
- Failure cases: statement failure (hostile server error that echoes all four secrets, at `ROLE`, `CREATE DATABASE`, `CREATE EXTENSION` or `GRANT CONNECT`), membership refusal (either runtime role, three granted roles), non-ASCII password for a random role.
- Fake cluster and CLI harness: copied from `test_bootstrap.py`, with an injectable failure hook. The CLI harness sets the environment, works in a fresh directory, replaces `connect_as`, `run_bootstrap` (calls the real `bootstrap()` on the fake cluster) and `migrate`, and restores the root logger afterwards (`configure_logging` changes it).

## Properties

| Test | Checks |
|---|---|
| `test_role_statements_hold_only_verifiers_and_exact_attributes` | Every rendered statement: no password or marker, exact attribute text and `CREATE`/`ALTER` verb, `NOINHERIT` only for `cairn_app`, `PASSWORD 'SCRAM-SHA-256$` verifier; for real encryptors the verifier equals the SCRAM verifier of that role's password |
| `test_a_value_that_is_not_a_verifier_is_refused_without_echo` | `role_statement` raises `BootstrapError` for free text, a valid verifier with text before or after it, an injection attempt and an md5 hash; message, repr and traceback never hold the value |
| `test_a_non_ascii_password_is_refused_without_echo` | `scram_sha256_verifier` and `role_statements` raise `BootstrapError` with no password, marker or non-ASCII text |
| `test_bootstrap_exposes_no_password_anywhere` | `bootstrap()` statements, every log record (message, all fields, formatted), `repr(result)` and `describe()` hold no secret; each role statement carries the right verifier |
| `test_running_bootstrap_again_repeats_the_same_statements` | Runs 2 and 3 are identical once verifier text is masked; run 1 equals run 2 after `CREATE ROLE` to `ALTER ROLE` and dropping `CREATE DATABASE`; verifiers differ between runs (fresh salt) |
| `test_bootstrap_failures_do_not_leak_a_password` | Statement failure, membership refusal and non-ASCII password: exceptions (str, repr, traceback chain), statements and logs clean; refusals happen before any write |
| `test_cli_failures_do_not_leak_a_password` | Same cases through `cairn-db bootstrap` and `init`: exit 1, output names classes and roles only, `migrate` never runs |
| `test_cli_output_holds_no_password` | `cairn-db bootstrap` and `init` success: output (which includes the JSON log lines), cluster statements and log records are clean |

## Notes

- PBKDF2 with 4096 iterations runs in about 2 ms (OpenSSL), so the real Python verifier is used in every example and no test sets `max_examples`. All tests set `deadline=None` because one example can do up to nine derivations on a busy machine.
- In the statement-failure cases the exception text itself is hostile on purpose (it echoes the secrets, as a buggy server might). The assertions skip that exception's own text and traceback chain and check what bootstrap and the CLI add: statements, logs and CLI output.
- No fixtures are used (Hypothesis does not reset function-scoped fixtures); each example patches the environment, working directory and logging itself and restores them.

## Verification

```text
$ uv run pytest packages/core/tests/unit/test_bootstrap_properties.py packages/core/tests/unit/test_bootstrap.py
58 passed in 4.25s
$ HYPOTHESIS_PROFILE=ci uv run pytest packages/core/tests/unit/test_bootstrap_properties.py
8 passed in 6.91s
$ ruff format --check / ruff check / mypy --strict on the new file and packages/core/src
clean
$ make types
Success (strict: 18 files; base: 52 files)
```

`make fmt-check` and `make lint` currently fail only in other agents' files (`apps/worker/src/cairn_worker/app.py` and the worker tests; a second file needs formatting, not mine); the new test file is clean under both.
