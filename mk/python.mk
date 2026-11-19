# Python quality gates (design §2 and §10; Requirements 2.1 to 2.3).

# mypy (PD-03): --strict over packages/core, the base config from pyproject.toml
# over every other Python directory. mypy rejects a directory that holds no .py
# file, so directories that are still empty are skipped until code lands.
MYPY_STRICT_DIRS := packages/core
MYPY_BASE_DIRS := apps/api apps/worker apps/combined apps/mcp packages/connectors packages/retrieval \
	packages/eval scripts tests
MYPY_BASE_FILES = $(wildcard conftest.py)

# $(call py_dirs,dirs): the directories that contain at least one .py file.
py_dirs = $(strip $(foreach d,$(1),$(if $(shell find $(d) -name '*.py' -print 2>/dev/null | head -n 1),$(d))))

.PHONY: fmt fmt-check lint types test-unit

fmt: ## Sort imports and format Python code in place
	$(UV_RUN) ruff check --select I --fix .
	$(UV_RUN) ruff format .

fmt-check: ## Check Python formatting (ruff format --check)
	$(UV_RUN) ruff format --check .

lint: ## Lint Python code (ruff check)
	$(UV_RUN) ruff check .

types: ## Type-check: mypy --strict on packages/core, base config elsewhere
	$(UV_RUN) mypy --strict --cache-dir .mypy_cache/strict $(call py_dirs,$(MYPY_STRICT_DIRS))
	$(UV_RUN) mypy --cache-dir .mypy_cache/base $(call py_dirs,$(MYPY_BASE_DIRS)) $(MYPY_BASE_FILES)

test-unit: ## Run the unit tests (everything not marked integration, smoke, obs, freetier or live)
	$(UV_RUN) pytest -m "not integration and not smoke and not obs and not freetier and not live"
