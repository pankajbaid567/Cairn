# Deploy helpers (free-tier design §14; Requirements 17.4 to 17.13).
#
#   make web-dist   build apps/web/dist for the Cloudflare Pages Direct Upload
#
# `web-dist` is safe for agents and CI: it only builds static files in the pinned Node
# container (WEB_RUNNER=host uses the host toolchain, as for web-check). It does not log in to
# or call Cloudflare. dist/ then holds index.html, assets/, _routes.json and _headers (copied
# from apps/web/public by Vite). The user uploads it from apps/web with the pinned wrangler
# command in docs/deploy.md, which also picks up apps/web/functions (FU6).
#
# None of the targets here is part of `make gates`.
#
# The three Neon targets are USER-RUN (FU1, FU2): agents and CI never call them. They are the
# only way the owner and admin credentials are used, and only from the laptop (FT-13).
#
#   make env-neon       write .env.neon from .env.neon.example (fresh random secrets for each
#                       change-me; the user-provided keys stay empty). Refuses to overwrite.
#   make remote-init    docker run the cairn-python:local image with --env-file .env.neon:
#                       cairn-db init --allow-plaintext-password (roles, databases, extensions,
#                       migrations on Neon). The flag is the OQ-1 decision of 2 Nov 2026 and
#                       needs CAIRN_DB_SSLMODE=verify-full in .env.neon; cairn-db exits 2
#                       before it connects otherwise.
#   make remote-status  the same container running cairn-db status (read only).
#
# remote-init and remote-status first check, with no connection and without printing a value,
# that .env.neon exists and that every key the template marks user-provided has a value; the
# hint names the keys. If the image cairn-python:local is absent they build it (the same image
# the compose db-init uses).
.PHONY: web-dist
web-dist: ## Build apps/web/dist (static files, _routes.json, _headers) for the Pages Direct Upload
	$(call web_run,pnpm install --frozen-lockfile && pnpm run build)

# --- User-run Neon targets (Requirements 13.5, 13.6, 17.12) ---------------------------------
NEON_ENV_FILE ?= $(ROOT)/.env.neon
NEON_ENV_TEMPLATE ?= $(ROOT)/.env.neon.example
# Not named *_IMAGE: scripts/licenses.py treats every such mk variable as a pinned tool image.
NEON_IMAGE_REF ?= cairn-python:local
# The interpreter that runs scripts/dev_env.py (standard library only). Tests override it.
DEV_ENV_PYTHON ?= $(UV_RUN) python

.PHONY: env-neon remote-init remote-status neon-env-check neon-image

env-neon: ## Neon, user-run: write .env.neon from .env.neon.example (refuses to overwrite)
	@if [ -e "$(NEON_ENV_FILE)" ] || [ -L "$(NEON_ENV_FILE)" ]; then \
		echo "error: $(NEON_ENV_FILE) already exists and was left unchanged. Edit it by hand, or delete it first if you want new random secrets." >&2; \
		exit 1; \
	fi
	$(DEV_ENV_PYTHON) $(ROOT)/scripts/dev_env.py --template $(NEON_ENV_TEMPLATE) --out $(NEON_ENV_FILE)
	@echo "next: fill the empty keys (the Neon endpoint host and the neondb_owner password) in .env.neon, then run make remote-init"

# Internal checks (no ## text, so `make help` skips them). They open no connection and print
# key names only.
neon-env-check:
	@if [ ! -f "$(NEON_ENV_FILE)" ]; then \
		echo "error: $(NEON_ENV_FILE) not found. Run 'make env-neon', then fill in the empty keys (FU1)." >&2; \
		exit 1; \
	fi
	@missing=$$(awk 'NR == FNR { if ($$0 ~ /^[A-Za-z_][A-Za-z0-9_]*=user-provided[ \t]*$$/) { split($$0, a, "="); need[a[1]] = 1 } next } \
		{ i = index($$0, "="); if (i > 1 && $$0 !~ /^[ \t]*#/) { k = substr($$0, 1, i - 1); v = substr($$0, i + 1); \
		gsub(/^[ \t"\047]+|[ \t"\047]+$$/, "", v); val[k] = v } } \
		END { for (k in need) if (val[k] == "") print k }' "$(NEON_ENV_TEMPLATE)" "$(NEON_ENV_FILE)" | sort | tr '\n' ' '); \
	if [ -n "$$missing" ]; then \
		echo "error: $(NEON_ENV_FILE) has empty values for: $$missing" >&2; \
		echo "hint: fill them in from the Neon console (FU1), then run this target again. No connection was opened." >&2; \
		exit 1; \
	fi

neon-image: neon-env-check
	@# always build: the layer cache makes an unchanged tree instant, and a stale image once ran an old `cairn-db init`
	docker build -f $(ROOT)/infra/docker/python-app.Dockerfile --build-arg PACKAGE=cairn-core -t $(NEON_IMAGE_REF) $(ROOT)

remote-init: neon-image ## Neon, user-run: bootstrap roles, databases, extensions and migrations on Neon from .env.neon (FU2)
	docker run --rm --env-file $(NEON_ENV_FILE) $(NEON_IMAGE_REF) cairn-db init --allow-plaintext-password

remote-status: neon-image ## Neon, user-run: print the applied and pending migrations on Neon from .env.neon (FU2)
	docker run --rm --env-file $(NEON_ENV_FILE) $(NEON_IMAGE_REF) cairn-db status
