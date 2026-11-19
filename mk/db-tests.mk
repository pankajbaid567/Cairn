# Database test targets (design §15; Requirements 10.7 and 17.1).
#
#   make test-integration   pytest -m integration against the compose db
#   make matrix             pytest -m matrix (permission assertions: roles, default deny)
#   make migrations-check   bootstrap a fresh database, migrate twice, expect no-op
#
# All three need the db service (`make up-db`). The fixtures in cairn_core.testing
# read CAIRN_TEST_ADMIN_HOST/PORT/USER and POSTGRES_PASSWORD from the environment or
# .env, create one throwaway cairn_test_<hex> database, and drop it afterwards. They
# skip (integration tests) or exit 2 (migrations-check) when the db is not reachable,
# and never print a secret value.
.PHONY: test-integration matrix migrations-check
test-integration: ## Run the integration tests (needs make up-db)
	$(UV_RUN) pytest -m "integration and not live"
matrix: ## Run the permission matrix tests (needs make up-db)
	$(UV_RUN) pytest -m matrix
migrations-check: ## Bootstrap a fresh database and apply all migrations twice
	$(UV_RUN) python scripts/migrations_check.py
