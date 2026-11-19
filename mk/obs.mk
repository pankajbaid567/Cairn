# Observability profile targets (design §4, §9, §10; Requirements 4.6, 9.8, 9.9).
#
#   make up-obs     start the core profile plus the obs profile (OpenTelemetry Collector,
#                   Prometheus, Grafana) and export api and worker spans to the collector
#   make down-obs   stop and remove the three obs containers only; core keeps running
#
# `up-obs` is `OTEL_TRACES_EXPORTER=otlp docker compose --profile core --profile obs up
# --wait --build`. The variable only reaches compose's interpolation, so .env is not edited,
# and a plain `make up` still starts api and worker with the exporter off (`none`). Because
# the setting is part of the api and worker definitions, compose recreates just those two
# when you switch between `up` and `up-obs`; db, db-init and web are not touched.
#
# Endpoints (127.0.0.1 only):
#   Grafana      http://127.0.0.1:3000   user admin, password GRAFANA_ADMIN_PASSWORD from .env
#   Prometheus   http://127.0.0.1:9090
#   Collector    http://127.0.0.1:4318   OTLP/HTTP; spans are logged: make logs-obs
# Move a port with CAIRN_GRAFANA_PORT, CAIRN_PROMETHEUS_PORT or CAIRN_OTLP_PORT.
#
# `down-obs` removes containers, never volumes (prometheus-data and grafana-data stay). It
# leaves api and worker running with the exporter still on; run `make up` to switch it off
# again. `make down` (stack.mk) stops the core profile and does not touch obs containers.
#
# Nothing here prints a secret value.

COMPOSE ?= docker compose
UP_BUILD ?= --build
LOG_TAIL ?= 200

OBS_SERVICES := otel-collector prometheus grafana

.PHONY: up-obs down-obs logs-obs obs-check

# Same failure report as `up`, for the obs services as well as the core ones.
up-obs: ## Start core and obs (collector, Prometheus, Grafana); api and worker export OTLP
	@OTEL_TRACES_EXPORTER=otlp $(COMPOSE) --profile core --profile obs up --wait $(UP_BUILD) || { \
		status=$$?; \
		echo "up-obs: failed (exit $$status); container state and last 50 log lines follow" >&2; \
		$(COMPOSE) --profile core --profile obs ps -a >&2 || true; \
		$(COMPOSE) --profile core --profile obs logs --no-color --tail 50 >&2 || true; \
		exit $$status; \
	}
	@echo "up-obs: Grafana http://127.0.0.1:$${CAIRN_GRAFANA_PORT:-3000} (admin), Prometheus http://127.0.0.1:$${CAIRN_PROMETHEUS_PORT:-9090}"

down-obs: ## Stop and remove the obs containers only (core and volumes are kept)
	$(COMPOSE) --profile obs rm --stop --force $(OBS_SERVICES)

logs-obs: ## Show the recent collector logs, which include the exported spans (LOG_TAIL=n)
	$(COMPOSE) --profile obs logs --no-color --tail $(LOG_TAIL) otel-collector

# Depends on `up-obs`, so it starts (or confirms) core and obs with span export first. One
# /readyz call must reach the collector log and Prometheus (task 10.2; Requirements 9.8, 9.9).
# It leaves the stack as it is; run `make down-obs` and `make up` to return to the default.
obs-check: up-obs ## Start core and obs, then check that a /readyz span reaches the collector and Prometheus
	$(UV_RUN) pytest -m obs tests/obs
