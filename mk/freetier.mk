# Free-tier profile targets (free-tier design §9; Requirements 10.7, 10.8 and 11.1 to 11.6).
#
#   make freetier-up      build and start ft-db, ft-admin-init, ft-db-init and ft-app; wait until healthy
#   make freetier-smoke   pytest -m freetier tests/freetier against the running profile
#   make matrix-managed   the permission matrix against ft-db as cairn_admin (managed mode)
#   make freetier-ram     idle and small-load memory of ft-app against the FT-18 budget; writes evidence
#   make freetier-check   freetier-up, freetier-smoke, matrix-managed, freetier-ram, freetier-down
#   make freetier-down    remove the four freetier containers; core containers and volumes stay
#   make freetier-logs    recent logs of the freetier services (LOG_TAIL=n); CI uploads them on failure
#
# The freetier profile emulates Render free (ft-app: 512 MB, 0.1 CPU) and Neon's owner role
# (ft-db with a non-superuser cairn_admin). The host is an 8 GB machine with a 3.8 GiB Docker
# VM, so never run it with the core profile: `make down` first. Nothing here starts, stops or
# reads the core stack.
#
# `freetier-up` is `docker compose --profile freetier up --wait --build`. At 0.1 CPU ft-app
# needs about 2.5 minutes to become healthy (FV13), so expect a wait.
#
# `freetier-smoke` does not start anything. Its tests skip with a message when ft-app is not
# running. The last test stops ft-app (the 20 second SIGTERM budget), so run `freetier-up`
# again before another smoke run. `matrix-managed` needs only ft-db, which keeps running.
#
# `matrix-managed` is `CAIRN_TEST_ADMIN_MODE=managed pytest -m "matrix or managed"`: the
# fixtures in cairn_core.testing connect to CAIRN_FREETIER_DB_PORT (default 55433) as
# cairn_admin with CAIRN_MANAGED_ADMIN_PASSWORD. One test is skipped by design in this mode
# (the superuser baseline); the managed role test, default deny and bootstrap idempotency run.
#
# `freetier-check` runs its five targets one after the other from a shell loop (also under
# `make -j`) and stops at the first failure. After a failure the profile is left up so you
# can look at it; `make freetier-down` removes it.
#
# `freetier-ram` is `scripts/freetier_ram.py` (design §10, FT-18): it runs `docker compose
# --profile freetier up --wait` itself (so a stopped ft-app, as the smoke module leaves it, is
# started again, about 2.5 minutes), refuses to run while core containers of project cairn are
# up, samples ft-app with `docker stats --no-stream` every 2 s for 60 s idle and 60 s under load
# from two clients (/readyz with the proxy header, /healthz, an unknown path; 0.2 s pause), reads
# OOMKilled and RestartCount, and writes docs/evidence/free-tier/freetier-ram-<date>.md and .json
# (also when a budget is missed). Pass: idle peak <= 300 MiB, load peak <= 384 MiB, failed
# requests <= 2%, no OOM kill, no restart. Exit 0 pass, 1 budget missed, 2 Docker or the profile
# unusable. Options through FT_RAM_ARGS, for example `make freetier-ram FT_RAM_ARGS="--no-up
# --idle-s 10 --load-s 10"` (--no-up, --idle-s, --load-s, --interval-s, --pause-s, --clients,
# --request-timeout-s, --out-dir, --date, --build). The proxy token comes from .env and is
# printed nowhere.
#
# `freetier-down` never passes --volumes or --remove-orphans: the data of ft-db is on tmpfs
# and vanishes with the container, and cairn_pgdata and cairn_pgdata-pgvector stay as they
# are.
#
# Nothing here prints a secret value. Needs Docker running and .env (`make env`, then
# `make env-update` for the freetier keys).

FT_RAM_ARGS ?=
FT_PHASE1_ARGS ?=

FREETIER_CHECK_STEPS := freetier-up freetier-seed freetier-phase1 freetier-smoke matrix-managed freetier-ram freetier-down

.PHONY: freetier-seed freetier-phase1 freetier-up freetier-smoke matrix-managed freetier-ram freetier-check freetier-down freetier-logs

# Same failure report as `up`, for the freetier services.
freetier-up: ## Build and start the freetier profile (ft-db, ft-app) and wait until it is healthy
	@$(COMPOSE) --profile freetier up --wait --build || { \
		status=$$?; \
		echo "freetier-up: failed (exit $$status); container state and last 50 log lines follow" >&2; \
		$(COMPOSE) --profile freetier ps -a >&2 || true; \
		$(COMPOSE) --profile freetier logs --no-color --tail 50 >&2 || true; \
		exit $$status; \
	}

# Imports the rendered Kestrel Pay corpus into ft-db as cairn_ingest (loopback port 55433) and embeds it
# with the fake embedder, so ft-app has content to search. No neurons are spent.
FT_DB_PORT ?= 55433

freetier-seed: ## Import the Kestrel Pay corpus into ft-db (fake embeddings; needs freetier-up)
	$(UV_RUN) cairn-scenario render --out var/corpus/kestrel_pay --overwrite
	CAIRN_DB_HOST=127.0.0.1 CAIRN_DB_PORT=$(FT_DB_PORT) CAIRN_EMBEDDER=fake $(UV_RUN) cairn-import all var/corpus/kestrel_pay --embed-inline

freetier-phase1: ## Phase 1 load against ft-app: login, search and answer under 512 MB and 0.1 CPU (needs freetier-seed)
	$(UV_RUN) python scripts/freetier_phase1.py $(FT_PHASE1_ARGS)

freetier-smoke: ## Run the freetier smoke tests against the running profile (needs freetier-up)
	$(UV_RUN) pytest -m freetier tests/freetier

matrix-managed: ## Run the permission matrix against ft-db as cairn_admin (needs freetier-up)
	CAIRN_TEST_ADMIN_MODE=managed $(UV_RUN) pytest -m "matrix or managed"

freetier-ram: ## Measure ft-app memory idle and under load against FT-18; writes docs/evidence/free-tier/freetier-ram-<date>.md/.json
	$(UV_RUN) python scripts/freetier_ram.py $(FT_RAM_ARGS)

freetier-check: ## freetier-up, freetier-smoke, matrix-managed, freetier-ram, freetier-down; stops at the first failure
	@set -e; for step in $(FREETIER_CHECK_STEPS); do \
		echo "==> make $$step"; \
		$(MAKE) --no-print-directory $$step; \
	done; \
	echo "==> freetier-check passed: $(FREETIER_CHECK_STEPS)"

freetier-down: ## Remove the freetier containers (no volumes; core containers stay)
	$(COMPOSE) --profile freetier down
freetier-logs: ## Show the recent logs of the freetier services (LOG_TAIL=n)
	$(COMPOSE) --profile freetier logs --no-color --tail $(LOG_TAIL)
