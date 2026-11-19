# Core profile stack targets (design §4 and §10; Requirements 4.1 to 4.5, 4.9, 7.2, 17.6).
#
#   make up      build and start db, db-init, api, worker and web; wait until healthy
#   make down    stop and remove the containers and the network; volumes are kept
#   make logs    show the recent log lines of the core services
#   make smoke   run `up`, then the smoke tests in tests/smoke against the running stack
#
# `up` is `docker compose --profile core up --wait --build`. Compose v2.40.3 accepts the
# db-init one-shot under --wait (Verify V3), so the sequential fallback in design §4
# (`up --wait db`, `run --rm db-init`, `up --wait api worker web`) is not needed.
#
# `down` never passes --volumes: the database volume (cairn_pgdata-pgvector) survives it. To
# delete the volume on purpose use `make db-reset`, which asks first. The old volume
# cairn_pgdata is no longer declared, so no target here reaches it (FU7 removes it).
#
# Variables:
#   UP_BUILD=--build   rebuild images on `up` (default; layers are cached, so it is cheap
#                      when nothing changed). `make up UP_BUILD=` skips the build step.
#   LOG_TAIL=200       lines per service for `make logs`
#   SERVICE=api        limit `make logs` to one service
#
# Compose reads .env from the repo root (`make env` writes it). Nothing here prints a
# secret value: `ps` and `logs` show container state and application log lines only.

COMPOSE ?= docker compose
UP_BUILD ?= --build
LOG_TAIL ?= 200
SERVICE ?=

.PHONY: up down logs smoke

# On failure: the state of every container of the project (including exited one-shots), then
# the last 50 log lines of every core service, so the failing service's reason is on screen.
# Compose's own exit status is kept.
up: ## Build and start the core profile and wait until it is healthy
	@$(COMPOSE) --profile core up --wait $(UP_BUILD) || { \
		status=$$?; \
		echo "up: failed (exit $$status); container state and last 50 log lines follow" >&2; \
		$(COMPOSE) --profile core ps -a >&2 || true; \
		$(COMPOSE) --profile core logs --no-color --tail 50 >&2 || true; \
		exit $$status; \
	}

down: ## Stop the core profile and remove its containers (volumes are kept)
	$(COMPOSE) --profile core down --remove-orphans

logs: ## Show the recent logs of the core services (LOG_TAIL=n, SERVICE=name)
	$(COMPOSE) --profile core logs --no-color --tail $(LOG_TAIL) $(SERVICE)

# Depends on `up`, so it starts (or confirms) the stack first and fails with compose's
# own output when the stack cannot become healthy. The tests themselves skip when no core
# service is running (for example when pytest is run by hand with the stack down).
smoke: up ## Start the core profile, then run the smoke tests against it
	$(UV_RUN) pytest -m smoke tests/smoke
