# Task 4.1: admin and TLS settings, TLS on every connection
Date: 2 Nov 2026. Requirements 4.1 to 4.5 and 7.2; decisions FT-13 and FT-14; plan items 0.5 and 0.7. The task was resumed after an interrupted run, which had already written FV11, the settings, the call-site changes and most tests; this run checked each site, fixed the type errors and finished the spec text.

## Settings (`cairn_core.config`)
- `CommonSettings.db_sslmode` (`CAIRN_DB_SSLMODE`, default `prefer`, one of `disable`, `prefer`, `require`, `verify-ca`, `verify-full`) and `db_sslrootcert` (`CAIRN_DB_SSLROOTCERT`, optional). `libpq_tls()` returns `{"sslmode": ...}` plus `sslrootcert` when set.
- `AdminSettings.admin_user` (`CAIRN_DB_ADMIN_USER`, default `postgres`), `admin_password` (`CAIRN_DB_ADMIN_PASSWORD`, then `POSTGRES_PASSWORD`), `admin_database` (`CAIRN_DB_ADMIN_DATABASE`, default `postgres`), `admin_mode` (`CAIRN_DB_ADMIN_MODE`: `auto`, `superuser`, `managed`; default `auto`).
- An invalid sslmode or admin mode exits 2 with the key and the pydantic error code, never the value.
- Defaults make `make env-update` unnecessary; the local `.env` was not touched.

## Where TLS and the admin identity are used
| Site | Change |
|---|---|
| `cairn_core.cli.connect_as` | `**libpq_tls(settings)`; bootstrap connects as `admin_user` to `admin_database` (no literal `postgres`) |
| readiness probe (`cairn_core.health`) | `**libpq_tls(settings)` on the `AsyncConnection` |
| worker probe (`cairn_worker.app`) | `**libpq_tls(settings)` |
| `dbos_url()` | query `sslmode` and, when set, `sslrootcert`; password still percent-encoded |
| `cairn_core.testing` | admin connection, per-database DSNs and `FreshDb` DSNs carry `libpq_tls`; admin database from `admin_database` |
| `cairn_core.bootstrap.connect_like` | copies `sslmode` and `sslrootcert` from the admin connection (`tls_parameters`) |
| `scripts/migrations_check.py` | `cli_env()` passes `CAIRN_DB_SSLMODE` and `CAIRN_DB_SSLROOTCERT` to the `cairn-db migrate` child |
| S1 (`spike_owner_dsn`) and S2 (`_connect`, admin connection) | `libpq_tls` / `tls_parameters` |

## Fixes in this run
- mypy: `libpq_tls` and `tls_parameters` return `dict[str, Any]` (psycopg's `connect` overloads reject `**dict[str, str]`); the tests patch `psycopg.connect` through the `psycopg` module instead of re-exported attributes; the `admin_password` field carries a `type: ignore[pydantic-alias]` because the pydantic mypy plugin flags `AliasChoices` on a field that has a default (false positive; tests pin both keys and their order).
- Deleted the gitignored scratch directory `var/fv11`.

## FV11 and the spec text
- `verify/FV11.md`: `sslrootcert=system` loads no roots with psycopg-binary 3.3.6 (libpq 18.0.6, bundled OpenSSL 3.5.8 with a build-time store at `/host/tmp/libpq.build`) in `python:3.12.16-slim`. The default for Neon and Render is `CAIRN_DB_SSLMODE=verify-full` with `CAIRN_DB_SSLROOTCERT=/etc/ssl/certs/ca-certificates.crt`. `require` is not used, because OQ-1's `--allow-plaintext-password` needs `verify-full`.
- `.env.neon.example` has exactly those two values.
- Spec text changed: design FT-14, the `db_sslrootcert` comment in §6, the Render Blueprint and `.env.neon` samples in §14, the transport line in the security section and the FV11 row; requirements 4.4 (adds the path note); tasks.md task 4.1's FV11 bullet and task 9.1's `render.yaml` bullet (render.yaml must use the same path, not `system`). No checkbox marker was touched.

## Unit tests
`test_config.py` (defaults, password fallback and precedence, admin keys, invalid sslmode and mode, `libpq_tls`), `test_testing_tls.py`, `test_bootstrap.py` (`connect_as` and `connect_like` kwargs, admin user and database used by the CLI), `test_health.py` (probe kwargs), `apps/worker/tests/unit/test_worker_app.py` (probe kwargs, `dbos_url` query parameters), `packages/eval/tests/unit/test_spike_tls.py` (S1 and S2), `tests/scripts/test_migrations_check.py`.

## Verify results
- `uv run pytest packages/core/tests/unit/test_config.py packages/core/tests/unit/test_health.py apps/worker/tests/unit` and the TLS tests above: 205 passed.
- `make types`: no issues in 24 + 102 source files. `make up-db`: db healthy. `make test-integration`: 40 passed.
- `make fmt-check lint types test-unit`: 1064 passed, 1 xfailed (the known canary xfail, unchanged).
- The Neon handshake itself is only proven by FU2 (`make remote-init`, user-run).
