# Local Phase 0 exit gate (design §10 and §18; Requirement 23).
#
#   make phase0-exit          gates, s1-smoke and s2-smoke, then check the source documents'
#                             hashes and the RAM report evidence (core and free-tier), then
#                             print the exit-gate checklist (EG1 to EG5, the user steps U1 to
#                             U7 and FU1 to FU9, open V/S items)
#   make phase0-exit-preview  the checks and the printout only; runs none of the above
#
# Needs Docker running and .env (`make env`), like `make gates`. Run it on an otherwise idle
# machine: `gates` builds images and runs the whole stack, and ends with `freetier-check`
# (the free-tier profile, EG5; about 8 to 10 minutes, run after the core `down`). It stops at
# the first failure.
# `s1-smoke` and `s2-smoke` need the database, which `gates` leaves down at its end, so
# `up-db` runs between them. `make -n phase0-exit` lists every step without running any.
#
# The user-owned steps U1 to U7 and FU1 to FU9 are printed as NOT automated. No git command runs.
# The hash record docs/evidence/phase-0/source-docs.sha256 is only read; brainstorm.md and
# development_plan.md are never touched. `make ram-report` is not run here (it adds about
# 2.5 minutes); its newest report in docs/evidence/phase-0 must exist, say pass and name the
# pgvector/pgvector image for the db service. The newest docs/evidence/free-tier/
# freetier-ram-<date>.json (written by `freetier-check`) must exist and say pass.
.PHONY: phase0-exit phase0-exit-preview
phase0-exit: ## Local exit gate: gates, s1-smoke, s2-smoke, source-doc hashes, then the exit checklist
	@set -e; for step in gates up-db s1-smoke s2-smoke; do \
		echo "==> make $$step"; \
		$(MAKE) --no-print-directory $$step; \
	done; \
	echo "==> make down"; \
	$(MAKE) --no-print-directory down
	$(UV_RUN) python scripts/phase0_exit.py --gates "$(GATES_ALL)"
phase0-exit-preview: ## Print the exit checklist and run its file checks; runs no gate (no Docker needed)
	$(UV_RUN) python scripts/phase0_exit.py --preview --gates "$(GATES_ALL)"
