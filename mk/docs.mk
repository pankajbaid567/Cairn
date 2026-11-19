# Documentation checks (design §16; Requirements 18.3 to 18.5 and 19.4).
#
# `make docs-check` runs every group. Pick groups with DOCS_ONLY, for example
# `make docs-check DOCS_ONLY="adr links"`. Groups: prd adr threat-model
# acceptance contributing links.

DOCS_ONLY ?=

.PHONY: docs-check

docs-check: ## Check the PRD, ADRs, threat model, acceptance list, CONTRIBUTING and links
	$(UV_RUN) python scripts/check_docs.py $(foreach group,$(DOCS_ONLY),--only $(group))
