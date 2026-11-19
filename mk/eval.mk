# Phase 1 evaluation targets (design §18, §21).
#
#   make scenario-render   render the Kestrel Pay scenario into var/corpus/kestrel_pay
#   make seed-local        render, then import into the local database with the fake embedder
#   make seed-decisions    load the gold decisions, claims and links (after seed-local)
#   make decisions-demo   run the ADR-007 archaeology and ADR-012 time-travel demos against the API
#   make eval-phase5     B0 to B3 on dev and test plus the deterministic suites (EVAL_MODELS=fake|live)
#   make eval-tables     regenerate docs/results.md from the Parquet files (no provider call)
#   make eval-smoke        B1 on the first 20 dev questions against a freshly seeded test database
#   make eval-phase1       B0 and B1 on all dev questions; writes docs/evidence/phase-1/eval/ and the baseline
#
# EVAL_MODELS is fake (default; no network), cached (Workers AI model keys from the on-disk cache,
# no network) or live (Workers AI; user-run, spends neurons and exits 3 if the estimate does not fit).

EVAL_MODELS ?= fake
CORPUS_DIR ?= var/corpus/kestrel_pay

.PHONY: seed-dump seed-load seed-reset scenario-render seed-local seed-decisions decisions-demo eval-smoke eval-phase1 eval-phase5 eval-tables c1-freeze

scenario-render: ## Render the Kestrel Pay scenario into var/corpus/kestrel_pay
	$(UV_RUN) cairn-scenario render --out $(CORPUS_DIR) --overwrite

seed-local: scenario-render ## Import the rendered corpus into the local database (needs make db-init)
	$(UV_RUN) cairn-import all $(CORPUS_DIR) --embed-inline

seed-decisions: ## Load the Kestrel Pay gold decisions into the local database (run seed-local first; safe to repeat)
	$(UV_RUN) cairn-eval seed-decisions --corpus $(CORPUS_DIR)

decisions-demo: ## ADR-007 archaeology and ADR-012 time travel against the local API (needs seed-local, seed-decisions, make up)
	$(UV_RUN) python scripts/decision_demo.py $(DEMO_ARGS)

eval-smoke: ## Eval smoke gate: B1 on 20 dev questions (EVAL_MODELS=fake|cached)
	$(UV_RUN) cairn-eval smoke --models $(EVAL_MODELS)

eval-phase1: ## B0 and B1 on all dev questions, writes evidence and the smoke baseline (EVAL_MODELS=fake|cached|live)
	$(UV_RUN) cairn-eval phase1 --models $(EVAL_MODELS)

eval-phase5: ## B0 to B3 on dev and test plus the deterministic suites into docs/evidence/phase-5/runs (EVAL_MODELS=fake|live)
	$(UV_RUN) cairn-eval phase5 --models $(EVAL_MODELS)

eval-tables: ## Regenerate docs/results.md from the Parquet files through DuckDB (makes no provider call)
	$(UV_RUN) cairn-eval tables

c1-freeze: ## Record the C1 question set and corpus hashes in docs/evidence/phase-5/c1-freeze.json
	$(UV_RUN) python scripts/freeze_c1.py

seed-dump: ## Dump the seeded local database to var/seed/kestrel.seed.gz (data only, embeddings included)
	$(UV_RUN) cairn-eval seed-dump dump

seed-load: ## Load the seed dump into an empty content database as cairn_owner (no neurons spent)
	$(UV_RUN) cairn-eval seed-dump load

seed-reset: ## Replace the content with the seed dump (truncates the content tables first)
	$(UV_RUN) cairn-eval seed-dump load --reset
