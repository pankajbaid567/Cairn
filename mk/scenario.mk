# Scenario tooling (design §10, §12; Requirements 13 and 10.4).
#
# `make scenario-validate` runs `cairn-scenario validate` on the Kestrel Pay
# scenario, scenarios/kestrel_pay/scenario.yaml. It exits 0 when the file is
# valid, 1 when the validator reports violations and 2 when the file is missing,
# unreadable or not valid YAML.
# Point it elsewhere with `make scenario-validate SCENARIO_FILE=path/to/file.yaml`.
SCENARIO_FILE ?= scenarios/kestrel_pay/scenario.yaml
.PHONY: scenario-validate
scenario-validate: ## Validate the Kestrel Pay scenario file
	$(UV_RUN) cairn-scenario validate $(SCENARIO_FILE)
