# Task 14.2 notes: S2 RLS and EXPLAIN harness
Date: 27 Oct 2026. New package `cairn_eval.spikes.s2_rls_explain` (`config`, `queries`, `analyze`, `harness`, `report`, `cli`), the `cairn-s2` script, `s2-run` and `s2-smoke` appended to `mk/spikes.mk`, `docs/evidence/phase-0/verify/V5.md`, and a dry-run report in `docs/spikes/S2/`. No new dependency.
## What it does
`cairn-s2 run` drops and recreates a `cairn_spike_*` database through `bootstrap_database`, loads an S1 dataset with the S1 loader into `s2.chunks` (renaming `chunk_id` to `id`), builds BM25 (with fallback), HNSW and GIN indexes as `cairn_owner`, enables and forces RLS with the `acl_read` policy, runs the default-deny checks, then measures every variant × principal × iterative-scan setting as `cairn_app` and the superuser baseline. Output: `<date>-report.md` and `<date>-results.json` in `--out`.
Defaults: 20,000 generated rows (or `--dataset`), principals p01/p05/p20/p50, iterative scan off/relaxed_order/strict_order, `ef_search` 40, 3 repeats (median-time run kept), recall at 10 averaged over 10 query vectors against exact search under the same role and visibility (index scan types turned off in the same transaction).
## Differences from design §14
- Policy uses `nullif(current_setting(…, true), '')::int[]` (see V5, hazard 1).
- Six variants, not five: `hybrid_rrf_explicit_acl` is added because Requirement 16.3 asks for each query shape with and without the explicit predicate.
- `PlanFacts` has two more fields, `acl_filter_evidence` (every location the plan shows) and `raw_plan` (kept for Requirement 16.8). `analyze_plan` takes optional labels (`variant`, `principal`, `role`, `iterative_scan`) so it stays pure.
- `run(cfg, admin, *, admin_conn=None, admin_user="postgres", progress=None)`: the extra arguments let the CLI and tests reuse one superuser connection. `S2Config` also has `rows`, `dim`, `recall_queries`, `maintenance_work_mem`, `drop_after` and `compose_file`.
- A baseline run without the explicit predicate does not depend on the principal, so it runs once per iterative-scan value with principal `n/a` (117 plans per repeat with the defaults instead of 144).
- The superuser baseline for "explicit" variants keeps the predicate; the baseline for the others has none.
- The image tag and digest are read from the `db` service in `docker-compose.yml` (no `docker` call).
## ACL filter location rules (Requirement 16.4, 16.8)
In order: `pushed_into_custom_scan` (Tantivy query on field `acl`), `index_condition` (`Index Cond` mentioning `acl`, which includes the GIN bitmap), `post_filter` (a node `Filter` or a ParadeDB `heap_filter` mentioning `acl`), `absent`, `unknown` (no scan on the spike table recognized, or an unreadable Tantivy query that mentions `acl`). All locations seen are in `acl_filter_evidence`.
## Verified
- `uv run pytest packages/eval/tests/unit/test_s2_analyze.py`: 39 tests (recorded plans from this server, unrecognized shapes, BM25 fallback with a fake executor that rejects the first statement and one that rejects both, config, SQL builders, summary, report files).
- `packages/eval/tests/integration/test_s2_harness.py`: 2,000 rows, two principals, two iterative-scan values, database `cairn_spike_s2_<hex>` dropped at the end; checks default deny, matrix size, recall range, report files.
- `make s2-smoke` and the 20,000-row `make s2-run` both completed with default deny passed. Peak `cairn-db-1` memory seen: about 200 MiB of its 1 GiB limit.
## Things to know
- `docs/spikes/S2/2026-10-27-*` is a dry run to prove the harness at the design size; task 1.1 re-runs it for ADR-0006. The JSON is about 1.1 MB because it holds every raw plan, one compact line per plan.
- `cairn_spike_s2` is left in place by `s2-run` for manual inspection; drop it with `DROP DATABASE cairn_spike_s2 WITH (FORCE)` as the superuser. `s2-smoke` drops its own database.
- The first 20,000-row run exposed server-side plan reuse (V5, hazard 2) through recall values; it is fixed with `prepare_threshold=None` and the committed report is from the second run.
- Property 17 (task 14.6) is not written here; `analyze_plan` is total on any input and the unit tests cover several malformed shapes.
