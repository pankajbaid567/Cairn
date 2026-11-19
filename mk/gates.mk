# Aggregate quality gates (design §10, PD-17; Requirements 10.1, 10.4).
#
#   make gates   run every Phase 0 quality gate in order, then `down`
#
# CI calls the same targets one by one (.github/workflows/ci.yml), so a green local
# `make gates` is the CI run. `make phase0-exit` (mk/exit.mk) builds on this target.
#
# Order matters, so the targets run one after the other from a shell loop, also under
# `make -j`. A failing gate stops the run and leaves the stack as it is, for
# debugging; `make down` removes the containers (never the volumes).
#   - compose-check runs before up-db: the compose file is validated before it is used.
#   - licenses runs before docs-check: it rewrites docs/licenses.md, which the link
#     check reads.
#   - smoke starts the core profile itself (it depends on `up`).
#   - freetier-check (mk/freetier.mk) comes last, after the core `down`: the core and
#     freetier profiles never share the 3.83 GiB Docker VM. It builds ft-app, runs its
#     smoke, managed-mode matrix and RAM check (about 8 to 10 minutes) and removes the
#     freetier containers itself. `workers-ai-live-check` is not a gate (Requirement 16.11).
#
# `make -n gates` prints every gate and the commands inside it without running any.
# Needs Docker running and .env (`make env`, then `make env-update` for the freetier keys).
# `secrets-history` is not part of the list: it needs a git checkout and runs in CI.

GATES_STATIC := fmt-check lint types test-unit web-check scenario-validate compose-check actionlint licenses docs-check audit secrets
GATES_STACK := up-db test-integration matrix migrations-check eval-smoke smoke
GATES_FREETIER := freetier-check
GATES_ALL := $(GATES_STATIC) $(GATES_STACK) down $(GATES_FREETIER)

.PHONY: gates
gates: ## Run every quality gate in order (needs Docker and .env), core stack down before freetier-check
	@set -e; for gate in $(GATES_ALL); do \
		echo "==> make $$gate"; \
		$(MAKE) --no-print-directory $$gate; \
	done; \
	echo "==> gates passed: $(GATES_ALL)"
