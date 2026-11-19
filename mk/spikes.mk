# Spike harnesses (design §13 and §14; Requirements 15 and 16).
#
#   make s1-generate   write an S1 dataset to var/s1/$(S1_NAME) (refuses a non-empty directory)
#   make s1-smoke      2,000 rows, dim 64: generate twice (different batch sizes), compare
#                      the content hashes, load into a throwaway cairn_test_<hex> database
#
# s1-generate options: S1_NAME, S1_ROWS, S1_DIM, S1_ACL_MODE (uncorrelated|clustered) and
# S1_ARGS for anything else, for example `make s1-generate S1_ARGS=--overwrite`. 200,000
# rows need CAIRN_DB_MEM_LIMIT=2g to load (design §13). Output stays in var/ (gitignored).
# s1-smoke needs the db service (`make up-db`); the throwaway database is dropped at the end.
S1_NAME ?= default
S1_ROWS ?= 50000
S1_DIM ?= 384
S1_ACL_MODE ?= uncorrelated
S1_ARGS ?=
S1_SMOKE_DIR ?= var/s1-smoke
.PHONY: s1-generate s1-smoke
s1-generate: ## Generate the S1 ACL corpus into var/s1/<S1_NAME>
	$(UV_RUN) cairn-s1 generate --name $(S1_NAME) --rows $(S1_ROWS) --dim $(S1_DIM) \
		--acl-mode $(S1_ACL_MODE) $(S1_ARGS)
# The two runs differ only in batch size, so equal hashes also prove batch independence.
# They overwrite var/s1-smoke, which only this target writes.
s1-smoke: ## S1 smoke: generate twice, compare content hashes, load 2,000 rows (needs make up-db)
	$(UV_RUN) cairn-s1 generate --out $(S1_SMOKE_DIR)/a --rows 2000 --dim 64 --clusters 16 \
		--noise-groups 20 --queries 20 --batch-rows 2000 --overwrite
	$(UV_RUN) cairn-s1 generate --out $(S1_SMOKE_DIR)/b --rows 2000 --dim 64 --clusters 16 \
		--noise-groups 20 --queries 20 --batch-rows 700 --overwrite
	@a=$$($(UV_RUN) cairn-s1 hash $(S1_SMOKE_DIR)/a); b=$$($(UV_RUN) cairn-s1 hash $(S1_SMOKE_DIR)/b); \
		if [ "$$a" != "$$b" ]; then echo "s1-smoke: content hashes differ ($$a vs $$b)" >&2; exit 1; fi; \
		echo "s1-smoke: content hashes match ($$a)"
	$(UV_RUN) cairn-s1 load $(S1_SMOKE_DIR)/a

# --- S2: RLS and EXPLAIN (design §14; Requirement 16) --------------------------------------
#
#   make s2-run     the real S2 run: 20,000 rows, 3 repeats, report to docs/spikes/S2
#   make s2-smoke   2,000 rows, dim 64, 1 repeat, report to var/s2-smoke (proves the harness)
#
# Both need the db service (`make up-db`). They recreate a cairn_spike_* database (the
# shared content database is never touched). s2-smoke drops its database at the end;
# s2-run keeps cairn_spike_s2 so the plans can be inspected by hand. Options: S2_ROWS,
# S2_DIM, S2_REPEATS, S2_OUT and S2_ARGS for anything else (`cairn-s2 run --help`).
# The real run and ADR-0006 belong to task 1.1.
S2_ROWS ?= 20000
S2_DIM ?= 384
S2_REPEATS ?= 3
S2_OUT ?= docs/spikes/S2
S2_ARGS ?=
S2_SMOKE_DIR ?= var/s2-smoke
.PHONY: s2-run s2-smoke
s2-run: ## S2: build the spike database, measure index use under FORCE RLS, write docs/spikes/S2 (needs make up-db)
	$(UV_RUN) cairn-s2 run --rows $(S2_ROWS) --dim $(S2_DIM) --repeats $(S2_REPEATS) \
		--out $(S2_OUT) $(S2_ARGS)
s2-smoke: ## S2 smoke: 2,000 rows, one repeat, report to var/s2-smoke (needs make up-db)
	$(UV_RUN) cairn-s2 run --rows 2000 --dim 64 --repeats 1 --recall-queries 5 \
		--database cairn_spike_s2_smoke --out $(S2_SMOKE_DIR) --drop-after
