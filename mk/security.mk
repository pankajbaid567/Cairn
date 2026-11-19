# Supply-chain and CI-config gates (design §10; Requirements 21.5, 21.6, 22.4, 10.5).
#
#   make audit      pip-audit (hashed uv export) and pnpm audit --audit-level high
#   make secrets    gitleaks over the working tree, values redacted
#   make secrets-history  gitleaks over the full git history (CI; needs .git)
#   make licenses   license policy check; writes docs/licenses.md
#   make actionlint lint the workflow files in .github/workflows
#
# The tool images are pinned by tag and digest (resolved 27 Oct 2026). Override
# them on the command line only to test a newer release. None of these targets
# needs a git repository.

# gitleaks: one pin for the pre-commit hook and this target. The reference is read
# from the hook's `entry:` line in .pre-commit-config.yaml, so a bump there is
# picked up here. The guard below stops the run if that line ever changes shape.
GITLEAKS_IMAGE ?= $(shell sed -n -E 's/^ *entry: (ghcr\.io\/gitleaks\/gitleaks:[^ ]+@sha256:[0-9a-f]+) .*/\1/p' $(ROOT)/.pre-commit-config.yaml)
# actionlint 1.7.12, multi-arch index digest (V7 notes).
ACTIONLINT_IMAGE ?= rhysd/actionlint:1.7.12@sha256:b1934ee5f1c509618f2508e6eb47ee0d3520686341fec936f3b79331f9315667

WORKFLOW_FILES = $(sort $(wildcard $(ROOT)/.github/workflows/*.yml $(ROOT)/.github/workflows/*.yaml))

.PHONY: audit audit-python audit-web secrets licenses actionlint sbom

audit: audit-python audit-web ## Dependency audit: pip-audit and pnpm audit (high and critical fail)

# Hashed requirements from the frozen lock (uv.lock is never rewritten), without
# the workspace members themselves, checked against the PyPI advisory service.
audit-python:
	@tmp=$$(mktemp); trap 'rm -f "$$tmp"' EXIT; \
	$(UV) export --frozen --all-packages --no-emit-workspace --no-header --quiet \
		--format requirements-txt --output-file "$$tmp"; \
	$(UV_RUN) pip-audit --requirement "$$tmp" --require-hashes --disable-pip \
		--progress-spinner off --strict

# pnpm reads the lockfile only, so no install is needed. The web runner is the
# pinned Node 24 container unless WEB_RUNNER=host (see mk/web.mk).
audit-web:
	@$(call web_run,pnpm audit --audit-level high)

secrets: ## Scan the working tree for secrets (gitleaks, redacted)
	@test -n "$(GITLEAKS_IMAGE)" || { echo "error: no pinned gitleaks entry found in .pre-commit-config.yaml" >&2; exit 2; }
	@docker run --rm -v "$(ROOT):/repo:ro" -w /repo $(GITLEAKS_IMAGE) dir /repo \
		--config /repo/.gitleaks.toml --redact --no-banner --verbose

# Full git history, the CI `security` job (checkout with fetch-depth: 0). Same image and
# config as `secrets`; `git` mode reads .git, so it fails without a repository, which is
# why it is not part of `make gates`. The container runs as another user than the
# checkout owner, so git is told to trust /repo through environment config
# (GIT_CONFIG_COUNT), without writing any git config.
.PHONY: secrets-history
secrets-history: ## Scan the full git history for secrets (gitleaks git; needs a checkout with .git)
	@test -n "$(GITLEAKS_IMAGE)" || { echo "error: no pinned gitleaks entry found in .pre-commit-config.yaml" >&2; exit 2; }
	@test -e "$(ROOT)/.git" || { echo "error: $(ROOT) is not a git checkout; secrets-history needs .git (use 'make secrets' for the working tree)" >&2; exit 2; }
	@docker run --rm -v "$(ROOT):/repo:ro" -w /repo \
		-e GIT_CONFIG_COUNT=1 -e GIT_CONFIG_KEY_0=safe.directory -e GIT_CONFIG_VALUE_0=/repo \
		$(GITLEAKS_IMAGE) git /repo --config /repo/.gitleaks.toml --redact --no-banner --verbose

licenses: ## Check license policy and write docs/licenses.md
	@tmp=$$(mktemp -d); trap 'rm -rf "$$tmp"' EXIT; \
	$(UV_RUN) pip-licenses --from=all --format=json > "$$tmp/python.json"; \
	( $(call web_run,pnpm install --frozen-lockfile >&2 && pnpm licenses list --json) ) > "$$tmp/web.json"; \
	$(UV_RUN) python scripts/licenses.py --python-json "$$tmp/python.json" --web-json "$$tmp/web.json"

# actionlint finds the workflows through a git project root and exits 3 without
# one, so the files are passed by name. A missing workflow is a failure, not a pass.
actionlint: ## Lint .github/workflows/*.yml (fails if there is no workflow)
	@test -n "$(WORKFLOW_FILES)" || { echo "error: no workflow files in .github/workflows" >&2; exit 1; }
	@docker run --rm -v "$(ROOT):/repo:ro" -w /repo $(ACTIONLINT_IMAGE) $(patsubst $(ROOT)/%,%,$(WORKFLOW_FILES))

sbom: ## Write CycloneDX SBOMs for Python and the web app into docs/evidence/phase-6 (lock files only)
	$(UV_RUN) python scripts/make_sbom.py
