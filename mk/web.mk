# Web app gates (design §3 and §10; Requirements 3.3, 3.4, 21.2, 21.4).
#
# The host has Node 20 and the web app needs Node 24 (engine-strict), so by
# default every web target runs inside a pinned Node 24 container (PD-09).
# WEB_RUNNER=host runs on the host toolchain instead (CI and anyone with Node 24
# and pnpm 10.8.1), for example `make web-check WEB_RUNNER=host`.
#
# Container layout: apps/web is bind-mounted at /work. node_modules lives in the
# named volume cairn-web-node-modules, so Linux-native binaries never land on
# the host. pnpm's store and the corepack shims live in cairn-web-cache.
# Docker creates an empty apps/web/node_modules mount point on the host; it is
# gitignored and stays empty.

WEB_DIR := $(ROOT)/apps/web
WEB_RUNNER ?= container

# Node 24 LTS image: exact tag plus the digest of the multi-arch index (V8).
NODE_IMAGE ?= node:24.22.1-alpine3.24@sha256:06d7adf3e0ef61a13f1d28682726366f97a15aa1888c934d3ecec86c33918b53

WEB_NODE_MODULES_VOLUME := cairn-web-node-modules
WEB_CACHE_VOLUME := cairn-web-cache

# $(call web_run,shell commands): run pnpm commands in apps/web on the chosen runner.
ifeq ($(WEB_RUNNER),host)
web_run = cd $(WEB_DIR) && $(1)
else ifeq ($(WEB_RUNNER),container)
# The container runs as the host user so that dist/ and pnpm-lock.yaml are not
# root-owned. The volumes are created root-owned, so a short root step hands them
# to that user first. corepack provides pnpm at the version in packageManager.
web_run = \
	docker run --rm --user 0 \
		-v $(WEB_NODE_MODULES_VOLUME):/work/node_modules -v $(WEB_CACHE_VOLUME):/cache \
		$(NODE_IMAGE) chown -R $$(id -u):$$(id -g) /work/node_modules /cache && \
	docker run --rm --user $$(id -u):$$(id -g) \
		-v $(WEB_DIR):/work -v $(WEB_NODE_MODULES_VOLUME):/work/node_modules \
		-v $(WEB_CACHE_VOLUME):/cache -w /work \
		-e HOME=/cache/home -e CI=true -e COREPACK_HOME=/cache/corepack \
		-e COREPACK_ENABLE_DOWNLOAD_PROMPT=0 -e npm_config_store_dir=/cache/pnpm-store \
		-e npm_config_package_import_method=copy -e PATH=/cache/bin:/usr/local/bin:/usr/bin:/bin \
		$(NODE_IMAGE) sh -euc 'mkdir -p /cache/bin /cache/home && corepack enable --install-directory /cache/bin pnpm && $(1)'
else
$(error WEB_RUNNER must be "container" or "host", got "$(WEB_RUNNER)")
endif

.PHONY: web-check web-fmt web-lock web-clean

web-check: ## Web gates: eslint, prettier, tsc, vitest, build (Node 24 container; WEB_RUNNER=host for host)
	$(call web_run,pnpm install --frozen-lockfile && pnpm run check)

web-fmt: ## Format the web app in place (prettier --write)
	$(call web_run,pnpm install --frozen-lockfile && pnpm run format)

web-lock: ## Refresh apps/web/pnpm-lock.yaml from package.json
	$(call web_run,pnpm install --lockfile-only)

web-clean: ## Remove the web container volumes and build output
	-docker volume rm $(WEB_NODE_MODULES_VOLUME) $(WEB_CACHE_VOLUME)
	rm -rf $(WEB_DIR)/dist
