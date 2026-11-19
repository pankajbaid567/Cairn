# Task 1.1 notes: repo layout and uv workspace

Date: 27 Oct 2026. Tooling: uv 0.11.25; `.venv` built on uv-managed CPython 3.12.12.
Versions are the latest stable releases on PyPI on that day, queried from the PyPI JSON API.

## Pinned direct dependencies

| Member | Dependency | Pin |
|---|---|---|
| cairn-core | click | 8.5.0 |
| cairn-core | opentelemetry-api, opentelemetry-sdk, opentelemetry-exporter-otlp-proto-http | 1.45.1 |
| cairn-core | opentelemetry-instrumentation-psycopg | 0.66b1 |
| cairn-core | psycopg[binary] (psycopg-binary 3.3.6 through the extra) | 3.3.6 |
| cairn-core | pydantic | 2.14.1 |
| cairn-core | pydantic-settings | 2.16.1 |
| cairn-api | fastapi | 0.162.0 |
| cairn-api | opentelemetry-instrumentation-fastapi | 0.66b1 |
| cairn-api | uvicorn | 0.54.0 |
| cairn-worker | dbos | 3.3.0 |
| cairn-eval | click | 8.5.0 |
| cairn-eval | jsonpatch | 1.38 |
| cairn-eval | jsonschema | 4.26.0 |
| cairn-eval | numpy | 2.5.4 |
| cairn-eval | pgvector | 0.5.1 |
| cairn-eval | pyarrow | 26.0.0 |
| cairn-eval | pydantic | 2.14.1 |
| cairn-eval | pyyaml | 6.0.3 |
| dev group | httpx | 0.28.1 |
| dev group | httpx2 | 2.16.0 |
| dev group | hypothesis | 6.170.12 |
| dev group | mypy | 2.4.0 |
| dev group | pip-audit | 2.10.1 |
| dev group | pip-licenses | 5.5.5 |
| dev group | pre-commit | 4.7.0 |
| dev group | pyarrow-stubs | 20.0.0.20260819 |
| dev group | pytest | 9.1.1 |
| dev group | pytest-asyncio | 1.4.1 |
| dev group | ruff | 0.17.3 |
| dev group | types-jsonschema | 4.26.0.20261006 |
| dev group | types-pyyaml | 6.0.12.20260906 |
| build (every member) | hatchling | 1.32.4 |

cairn-mcp, cairn-connectors and cairn-retrieval depend only on cairn-core. Workspace members are referenced by name with `{ workspace = true }` sources and carry no version specifier.

uv.lock: 111 registry packages, every sdist and wheel with a sha256 hash; celery, redis and kombu are absent.

## Deviations from the design

| Design | Done here | Why |
|---|---|---|
| httpx is the HTTP test client (dev group) | httpx kept, httpx2 added | Starlette 1.10.0 (pulled in by FastAPI 0.162.0) builds `TestClient` on httpx2 and raises `StarletteDeprecationWarning` when only httpx is installed. With both installed, `import fastapi.testclient` is warning-free |
| jsonpatch in the dev group | Declared in cairn-eval | The user's dependency list puts JSON Patch under eval, and library code that applies broken-fixture patches must declare what it imports. Tests still get it |
| No CLI library named | click 8.5.0 in cairn-core and cairn-eval | dbos and uvicorn already depend on click, so the lock gains nothing. Click usage errors exit with 2, matching the exit codes in design §7 and §12 |
| opentelemetry-sdk and exporter for core | opentelemetry-api also declared directly | `cairn_core.telemetry` imports `opentelemetry.trace` |
| "latest stable" for every pin | OpenTelemetry instrumentation pinned to 0.66b1 | These packages have no stable release. 0.66b1 is the release paired with SDK 1.45.1 |
| zstd support | No extra package | The pyarrow 26 wheel includes zstd. Checked by writing and reading back a ZSTD-compressed Parquet file |
| §1 layout | `apps/web/` created empty; empty directories hold `.gitkeep` | Requirement 1.1 lists apps/web; task 7.1 fills it. `.gitkeep` keeps the directories after the user runs `git init` |

## Notes for later tasks

- The lock is final for Phase 0. Adding `[project.scripts]` entries or package data does not change uv.lock; changing a dependency or a member `version` does. Only `cairn-mcp` declares a script so far. Add `cairn-db`, `cairn-scenario`, `cairn-s1` and `cairn-s2` with their modules, then run `uv sync`.
- 1.2, mypy: jsonpatch and jsonpointer are untyped single-file modules with no maintained stubs (`types-jsonpatch` is an unofficial 2022 alpha and is not used). Add an override with `ignore_missing_imports = true` for `jsonpatch` and `jsonpointer`. Every other import is typed or stubbed: a throwaway probe importing them all (with the pydantic plugin, pyarrow Parquet writing and `fastapi.testclient`) passed `mypy --strict`.
- 1.2, mypy and pytest: test files with the same basename in several members need `--import-mode=importlib` (pytest) and `explicit_package_bases` or unique names (mypy).
- 1.2, pytest: every third-party import is warning-free under `python -W error`, so `filterwarnings = ["error"]` is an option.
- 4.1, 5.1, 6.1: runtime images get no dev dependencies, so `cairn_api.healthcheck` should use `urllib.request`, since httpx and httpx2 are dev-only.
- 6.1: dbos 3.3.0 depends on click, croniter, python-dateutil, psycopg[binary], pyyaml and sqlalchemy[asyncio]. The `otel` extra is not installed (design: `enable_otlp: False`).
- FastAPI 0.162.0 depends on opentelemetry-api (>=1.44.0) directly and pulls in Starlette 1.10.0.
- 12.1: jsonschema's format checker handles `date` out of the box. `date-time` and `uri` checks need extra packages that are not installed, so those formats pass the schema stage and are checked by the pydantic models.
- Python: conda's base environment is active in this shell, so a fresh `uv sync` picks Anaconda's 3.12.2 unless it gets `--managed-python`. The current `.venv` uses uv-managed 3.12.12. Both satisfy `==3.12.*`.
- Freshness: ruff 0.17.3 came out today. jsonpatch 1.38 and hypothesis 6.170.12 came out yesterday. pytest-asyncio 1.4.1, fastapi 0.162.0, pydantic-settings 2.16.1 and httpx2 2.16.0 are under a week old. If a release cooldown is wanted, uv's `exclude-newer` can enforce one; none is set.
- Build dependencies are not in uv.lock. Each member pins `hatchling==1.32.4` in `[build-system]`.
- `source-docs.sha256` verifies from the repo root with `shasum -a 256 -c docs/evidence/phase-0/source-docs.sha256` (`sha256sum -c` on Linux).
