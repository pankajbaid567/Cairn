# Task 1.2 notes: Python tooling, Hypothesis profiles, pre-commit, Makefile

Date: 27 Oct 2026. Dependencies and uv.lock unchanged (`uv lock --check` passes).

## Files

| File | State |
|---|---|
| `pyproject.toml` | `[tool.uv] python-preference = "only-managed"`, `[tool.ruff]`, `[tool.mypy]` (pydantic plugin, `explicit_package_bases`, `mypy_path`, jsonpatch/jsonpointer override), `[tool.pydantic-mypy]`, `[tool.pytest.ini_options]` |
| `Makefile`, `mk/python.mk` | shared variables, `help`, `include mk/*.mk`; `fmt`, `fmt-check`, `lint`, `types`, `test-unit` |
| `conftest.py` | Hypothesis profiles `dev` (100) and `ci` (200), selected by `HYPOTHESIS_PROFILE` |
| `.pre-commit-config.yaml` | pre-commit-hooks, ruff-pre-commit, gitleaks docker hook, local mypy and web hooks |
| `tests/tooling/test_test_setup.py`, `<member>/tests/unit/test_import.py` | marker and profile checks; one import smoke test per member |

## Hook revs (latest tags on 27 Oct 2026)

| Repo | Rev |
|---|---|
| pre-commit/pre-commit-hooks | v6.0.0 |
| astral-sh/ruff-pre-commit | v0.17.3 (equals ruff in the dev group) |
| gitleaks/gitleaks | v8.30.1; image `ghcr.io/gitleaks/gitleaks:v8.30.1@sha256:c00b6bd0…bbb7f` |

## Notes for later tasks

- Layout: unit tests sit in `<member>/tests/unit/`, cross-cutting tooling tests in `tests/`. Basenames repeat across members, which `--import-mode=importlib` and mypy's `explicit_package_bases` allow. Unmarked tests are unit tests.
- `make test-unit` runs `pytest -m "not integration and not smoke"`. It therefore also runs `matrix` tests. Tag a test `matrix` only if it works without the database, or also mark it `integration` (the design says the Phase 0 role tests are both).
- 4.2: put the database fixtures in `cairn_core.testing` and expose them from the root `conftest.py` (import them there). Mark tests `@pytest.mark.integration`. Add `test-integration` (`pytest -m integration`) and `matrix` (`pytest -m matrix`) in `mk/db-tests.mk`. Fixtures must skip, not fail, when the db is down.
- `filterwarnings = ["error"]` is on. A test that triggers a library warning must filter it explicitly with `pytest.mark.filterwarnings`.
- `make types` skips directories without a `.py` file, so empty `scripts/` does not break it. mypy base config covers `tests` and `conftest.py`.
- `UV_RUN` is `uv run --locked --all-packages`. Container builds that use the image's own Python must set `UV_PYTHON_PREFERENCE=only-system`.
- The `web-check` pre-commit hook calls `make web-check`, which task 7.1 adds. The hook only fires for web source files under `apps/web/`, never for `.gitkeep`.
- Hypothesis `HYPOTHESIS_PROFILE=ci` is derandomized with no deadline, so CI runs are reproducible.
