# Database stack targets (design §4, §5, §6; Requirements 4.x, 5.x).
#
#   make compose-check   docker compose config -q for every profile
#   make up-db           start the db service and wait until it is healthy
#   make db-init         bootstrap roles, databases and extensions, then migrate
#   make db-status       print applied and pending migrations (read-only)
#   make db-reset        stop the stack and delete the database volume (asks first)
#   make db-reset-guard  check that DB_VOLUME names the pgvector volume (changes nothing)
#
# Compose reads .env from the repo root (`make env` writes it). None of these
# targets prints a secret value. `make up`, `down` and the other stack targets
# belong to mk/stack.mk (task 8.1) and reuse COMPOSE.

COMPOSE ?= docker compose
# The database volume. Compose names it <project>_<volume>: project `cairn`, volume
# `pgdata-pgvector` (design §1, FT-02). The old volume `cairn_pgdata` is kept for
# the user to remove (FU7); db-reset-guard refuses it and every other name.
DB_RESET_VOLUME := cairn_pgdata-pgvector
DB_VOLUME ?= $(DB_RESET_VOLUME)

.PHONY: compose-check up-db db-init db-status db-reset db-reset-guard

# The check uses the tracked template, not .env: `config` then validates the file
# the same way on a laptop and in CI, and needs no secret. The placeholders satisfy
# every ${VAR:?}. Profiles without services (experiments) are checked together with
# core, because compose refuses to render a selection that holds no service. The
# freetier profile has services of its own and is checked alone (Requirement 10.7).
compose-check: ## Validate docker-compose.yml for every profile
	@for profile in core obs experiments freetier; do \
		case "$$profile" in core|freetier) set -- --profile "$$profile";; *) set -- --profile core --profile "$$profile";; esac; \
		$(COMPOSE) --env-file $(ROOT)/.env.example "$$@" config -q \
			|| { echo "compose-check: profile $$profile is invalid" >&2; exit 1; }; \
		echo "compose-check: profile $$profile ok"; \
	done

up-db: ## Start the db service and wait for it to be healthy
	$(COMPOSE) --profile core up --wait --no-deps db

# `run` builds the image on first use; --build rebuilds it when the code changed.
# It starts db first if it is not running, then removes the one-shot container.
db-init: ## Bootstrap roles, databases and extensions, then run migrations
	$(COMPOSE) --profile core run --rm --build db-init

# Host side, through the published port on 127.0.0.1 and the values in .env.
db-status: ## Show applied and pending migrations
	$(UV_RUN) cairn-db status

# Only compares the name: no docker call, no compose call (Requirement 1.5). db-reset
# depends on it, so a wrong DB_VOLUME stops db-reset before it stops or deletes anything.
db-reset-guard: ## Refuse any DB_VOLUME other than cairn_pgdata-pgvector (changes nothing)
	@if [ "$(DB_VOLUME)" != "$(DB_RESET_VOLUME)" ]; then \
		echo "db-reset-guard: refusing DB_VOLUME=$(DB_VOLUME); db-reset deletes only $(DB_RESET_VOLUME)" >&2; \
		exit 1; \
	fi

# Deleting the volume loses every database in it, and the old role passwords go
# with it. Run it after regenerating .env, or to start from an empty database.
# Non-interactive runs must say so: make db-reset CONFIRM=yes.
db-reset: db-reset-guard ## Delete the database volume (asks for confirmation; CONFIRM=yes skips)
	@if [ "$(CONFIRM)" != "yes" ]; then \
		[ -t 0 ] || { echo "db-reset: refusing without a terminal; use CONFIRM=yes" >&2; exit 1; }; \
		read -r -p "Delete the Docker volume $(DB_VOLUME) and all databases in it? Type 'yes': " answer; \
		[ "$$answer" = "yes" ] || { echo "db-reset: aborted" >&2; exit 1; }; \
	fi
	$(COMPOSE) --profile core down --remove-orphans
	@if docker volume inspect $(DB_VOLUME) >/dev/null 2>&1; then \
		docker volume rm $(DB_VOLUME) >/dev/null && echo "db-reset: removed volume $(DB_VOLUME)"; \
	else \
		echo "db-reset: volume $(DB_VOLUME) does not exist"; \
	fi
