# Local environment file (design §10; Requirement 22; free-tier Requirement 13).

.PHONY: env env-update

env: ## Write .env from .env.example with random secrets (refuses to overwrite)
	$(UV_RUN) python scripts/dev_env.py

env-update: ## Append the .env.example keys missing from .env (never changes a line)
	$(UV_RUN) python scripts/dev_env.py --add-missing
