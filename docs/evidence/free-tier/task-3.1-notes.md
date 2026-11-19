# Task 3.1: S2 harness from BM25 to native full-text search

Date: 2 Nov 2026. Requirements 3.1 to 3.6 and 3.8; decisions FT-05 and FT-28; design §5; plan items 0.13, 0.14, 1.1, D6.

Starting state (task 2.3): `cairn-db-1` on `pgvector/pgvector:0.8.6-pg18`, api, worker and web up; the S2 harness integration test was a strict xfail because the `bm25` access method no longer exists.

## FV12 first

Resolved on the running `db` before any code change; recorded in [verify/FV12.md](verify/FV12.md). The `simple` parser splits `t3_5` at `_` into the lexemes `t3` and `5`, and `websearch_to_tsquery('simple', 't3_5 or t4_1')` gives `'t3' <-> '5' | 't4' <-> '1'`. In S1 bodies the phrase matches exactly the bodies holding the token, so the generator and the configuration stay as they are.

## Changes

`packages/eval/src/cairn_eval/spikes/s2_rls_explain/`:

- `queries.py`: `VARIANTS` are `fts`, `fts_explicit_acl`, `hnsw`, `hnsw_explicit_acl`, `hybrid_rrf`, `hybrid_rrf_explicit_acl`; `TS_CONFIG = "simple"`, `TERM_SEPARATOR = " or "`, `TSV_COLUMN = "body_tsv"`, `lexical_text()`. `fts_query()` builds `SELECT id, ts_rank_cd("body_tsv", q) AS score FROM "s2"."chunks", websearch_to_tsquery('simple', '<terms joined with or>') AS q WHERE "body_tsv" @@ q [AND acl && '{…}'::int[]] ORDER BY score DESC, id LIMIT n`; the terms reach the statement as one `sql.Literal`. `hybrid_query()` uses `fts_query(…, RRF_DEPTH)` as its lexical side (CTE `lex`). `build_query()` rejects any name outside `VARIANTS`.
- `harness.py`: `tsv_column_statement()` (`ALTER TABLE s2.chunks ADD COLUMN body_tsv tsvector GENERATED ALWAYS AS (to_tsvector('simple', body)) STORED`) and `fts_statement()` (`CREATE INDEX chunks_fts_idx ON s2.chunks USING gin (body_tsv)`), run by `build_spike` after the load, before the HNSW and ACL GIN indexes and `ANALYZE`; `build_spike` returns `LexicalIndexOutcome`. Removed: `create_bm25_index`, `bm25_statement`, `BM25_COLUMNS` and the `(id, body)` fallback. `collect_environment` asks for `vector` and `btree_gist` only and records the generated column (`tsv_column`, from `information_schema.columns.generation_expression`). `image_reference()` takes the first `pgvector/pgvector:<tag>[@sha256:…]` outside a comment, which is the `x-pg-image` anchor; on the repository file it gives `pgvector/pgvector:0.8.6-pg18` and `sha256:2ba9ca5f…7e7a`. `_config_dict` adds `ts_config`.
- `analyze.py`: `IndexNames(fts="chunks_fts_idx", hnsw=…, gin=…)`; `PlanFacts.uses_fts_index` replaces `uses_bm25_scan`. `uses_fts_index` and `uses_hnsw_index` are true when an index scan node (`Index Scan`, `Index Only Scan`, `Bitmap Index Scan`) names the index. `acl_filter_location` is `index` (an index scan node on `chunks_acl_gin_idx`), else `filter` (a `Filter` or `Recheck Cond` that mentions `acl` as a word), else `none`, read anywhere in the tree; `acl_filter_evidence` keeps both kinds. The old `unknown`, `absent`, `pushed_into_custom_scan`, `index_condition` and `post_filter` values, the `Tantivy Query`, `Index` and `Table` readings and the recognized-scan rule are gone; a plan the analyzer cannot read reports `none` (Requirement 3.5).
- `report.py`: `LexicalIndexOutcome(index_definition)` replaces `Bm25Outcome`; `S2Report.lexical`; results key `lexical_index`; `RESULTS_SCHEMA` is now `cairn-s2-results/2` (the 27 Oct file is version 1). The summary says "FTS index not used"; the Markdown names the engine, shows the FTS index and the generated column, and the results tables have an "FTS index" column.
- `__init__.py`: exports `LexicalIndexOutcome`; `Bm25Outcome` and `create_bm25_index` removed.

Spike host guard (FT-28, Requirement 3.8): new `cairn_eval/spikes/host_guard.py` with `is_local_host`, `host_allowed` and `refusal`. Accepted without the flag: loopback addresses (`127.0.0.0/8`, `::1`, also in brackets), `localhost`, and the compose service names `db` and `ft-db`, case-insensitive. Everything else is refused, including an empty host, a socket directory and a comma-separated list. `cairn-s1 load` and `cairn-s2 run` gain `--allow-remote`; after `load_config()` and before `connect_admin()` they check `CAIRN_TEST_ADMIN_HOST` and exit 2 with a message that names the host (never a password).

Also: the `S1Config` topic-token docstring says "lexical query" instead of "BM25 query".

## Plan fixtures

The eight 27 Oct fixtures in `packages/eval/tests/unit/data/s2_plans/` (four BM25 plans plus four HNSW and sequential-scan plans recorded on the old image) were deleted. The 14 new ones come from one real harness run on 2 Nov 2026: 2,000 rows, dim 64, S1 seed 20261027 (`content_sha256` `31b23dec…5dcc`), principals p01 to p50, `iterative_scan` off and strict_order, in the throwaway database `cairn_spike_s2_fixtures`. After the run the ACL GIN index was dropped and the four lexical and hybrid variants were explained again as `cairn_app` (p05, `off`). The database was dropped at the end; the server then held only `cairn`, `cairn_dbos`, `postgres`, `template0` and `template1`. The recording script and its raw output stayed in the gitignored `var/s2-fixtures/`.

| Fixture | Role, variant, principal | Analyzer result |
|---|---|---|
| `fts_rls_acl_index` | cairn_app, fts, p05 | ACL bitmap, `@@` as Filter; FTS index no; `index`; 111 removed |
| `fts_explicit_rls_acl_index` | cairn_app, fts_explicit_acl, p05 | same shape; `index` |
| `hybrid_rls_acl_index` | cairn_app, hybrid_rrf, p05 | ACL bitmap on both sides; FTS and HNSW no; `index` |
| `hybrid_explicit_rls_acl_index` | cairn_app, hybrid_rrf_explicit_acl, p05 | same; `index` |
| `fts_rls_no_acl_index` | cairn_app, fts, p05, no ACL index | Seq Scan; `filter`; 1,998 removed |
| `fts_explicit_rls_no_acl_index` | cairn_app, fts_explicit_acl, p05, no ACL index | Seq Scan; `filter` |
| `hybrid_rls_no_acl_index` | cairn_app, hybrid_rrf, p05, no ACL index | two Seq Scans; `filter`; 3,885 removed |
| `hybrid_explicit_rls_no_acl_index` | cairn_app, hybrid_rrf_explicit_acl, p05, no ACL index | two Seq Scans; `filter` |
| `fts_superuser` | superuser, fts | Seq Scan; `none` |
| `fts_explicit_superuser` | superuser, fts_explicit_acl, p05 | BitmapAnd of `chunks_acl_gin_idx` and `chunks_fts_idx`; FTS index yes; `index` |
| `hybrid_explicit_superuser` | superuser, hybrid_rrf_explicit_acl, p50 | FTS and HNSW index yes; `index` |
| `hnsw_rls_post_filter` | cairn_app, hnsw, p20 | HNSW Index Scan with an `acl` Filter; `filter`; 24 removed |
| `hnsw_rls_gin_instead` | cairn_app, hnsw, p05 | ACL bitmap instead of HNSW; `index` |
| `hnsw_superuser` | superuser, hnsw | HNSW Index Scan; `none` |

## Finding for task 3.5 and ADR-0006: the FTS index is not used under FORCE RLS

In every `cairn_app` plan, at 2,000 rows, `body_tsv @@ q` runs as a `Filter` above the ACL bitmap (or a sequential scan without the ACL index). It never becomes an index condition, so `uses_fts_index` is false for all 48 lexical and hybrid plans under RLS in `make s2-smoke`. The superuser with the explicit predicate does use `chunks_fts_idx` (BitmapAnd with the ACL index).

The cause is leakproofness. The operator functions are not leakproof on this server:

| Operator | Function | `proleakproof` |
|---|---|---|
| `tsvector @@ tsquery` | `ts_match_vq` | f |
| `anyarray && anyarray` | `arrayoverlap` | f |

PostgreSQL evaluates the RLS policy qual before any non-leakproof qual of the query, so a non-leakproof user qual cannot be used as an index condition ahead of the policy. The policy's own `acl && …` qual can use the ACL GIN index; the query's `@@` and its explicit `acl && …` predicate run as filters on the rows the policy admits. This differs from the 27 Oct BM25 run, where the ParadeDB custom scan read the lexical query itself. Whether the planner still picks the ACL bitmap at 20,000 rows, and what that costs, is task 3.5's measurement; the result matters for ADR-0006 (RLS-first queries versus SECURITY DEFINER retrieval functions). Nothing in this task depends on it, and no requirement asks for the FTS index to be used under RLS. The integration test therefore asserts only what holds by construction: every `cairn_app` plan applies the ACL predicate somewhere (`index` or `filter`), baseline plans without the predicate report `none`, and the FTS index is used by at least one superuser plan with the explicit predicate.

## Tests

- `packages/eval/tests/unit/test_s2_analyze.py`: rewritten for the new fixtures (parametrized over the RLS plans with and without the ACL index, the superuser plans, HNSW plans), hand-made trees for the `index`-over-`filter` precedence and `Recheck Cond`, `none` for unreadable shapes, config, summary and report writer (schema 2, `lexical_index`, the FTS row and generated column in the Markdown). The BM25 fallback tests are gone with the fallback.
- `packages/eval/tests/unit/test_s2_queries.py` (new): variant list, the predicate only on explicit variants, the exact `fts` statement, the hybrid's single lexical literal, `or` joining and quoting of hostile terms, unknown variant, the two DDL statements, `image_reference` on a temporary compose file (comment skipped), without a pgvector image, and on the repository file.
- `packages/eval/tests/unit/test_spike_host_guard.py` (new): accepted and refused hosts with and without `--allow-remote`; `cairn-s2 run` with a remote host exits 2 without calling `connect_admin`, goes on to connect with the flag, and accepts `db`. No `.env` is read (the test changes into a temporary directory and sets fake test-only values); the message names the host and no password.
- `packages/eval/tests/unit/test_s1.py`: `cairn-s1 load` with a remote host exits 2 before connecting, connects with `--allow-remote`, and accepts `127.0.0.1`, `localhost`, `db` and `ft-db`.
- `packages/eval/tests/unit/test_s2_plan_analysis_properties.py` (Phase 0 Property 17): adapted to the three locations. The allowed-values check now requires `none` exactly when there is no evidence; the unrecognized-plan property generates nodes that are neither index nor sequential scans and never mention `acl` in a `Filter` or `Recheck Cond`, and expects `none`; the Tantivy-specific generators and the RecursionError case are gone; the deep-tree case now carries an `acl` filter at depth 5,000 and expects `filter`. The retarget's Property 3 is still task 3.3, Properties 2 and 4 are tasks 3.2 and 3.4.
- `packages/eval/tests/integration/test_s2_harness.py`: the TEMPORARY strict xfail of task 2.3 is removed; assertions for the FTS index definition, the generated column, the two extensions, the pgvector image tag, the `websearch_to_tsquery('simple'` statement, results schema 2, and the ACL-location rules above.
- `tests/scripts/test_no_paradedb.py`: every TEMPORARY_ALLOWLIST entry for task 3.1 deleted (11 entries); the two task 10.1 entries stay. No file under `packages/eval` names the old extension or vendor any more.

## Runs

| Command | Result |
|---|---|
| `uv run pytest …test_s2_analyze.py …test_s2_queries.py …test_s1.py` | 76 passed |
| `make types` | strict core 23 files, base 97 files: no issues |
| `make up-db` | `cairn-db-1` healthy (already running) |
| `make s2-smoke` | exit 0; 117 plans; default deny passed; report and results in `var/s2-smoke/2026-11-02-*`; summary: FTS index not used under RLS in 12 of 12 plans for each lexical and hybrid variant; ACL location `index` for all of them, `filter` x6 and `index` x6 for `hnsw` |
| `make fmt-check lint` | 121 files formatted; ruff clean |
| `make test-unit` | 1020 passed, 49 deselected, 1 xfailed (the known `canary_token` strict xfail) |
| `make test-integration` | 40 passed, 1030 deselected (the S2 harness test passes) |
| `uv run pytest tests/scripts/test_no_paradedb.py` | 8 passed |

## End state

- Databases on `cairn-db-1`: `cairn`, `cairn_dbos`, `postgres`, `template0`, `template1` (every `cairn_spike_*` and `cairn_test_*` database was dropped).
- `cairn-db-1`, `cairn-api-1`, `cairn-worker-1` and `cairn-web-1` up and healthy, left running.
- `cairn_pgdata` exists (created 2026-10-27T14:43:33Z) and no container mounts it.
- The 27 Oct 2026 S2 files are unchanged: `2026-10-27-report.md` `01f88620ecf4fa26456377430ba9d767876bfb84b27c1256cee3368c0e176942`, `2026-10-27-results.json` `79f1b03498129ccd14394a808f2b25b21b49863cadf90c51e612647f34a8b09f` (modified 27 Oct 23:30; `make s2-run` was not run).

## Verify

`uv run pytest packages/eval/tests/unit/test_s2_analyze.py packages/eval/tests/unit/test_s2_queries.py packages/eval/tests/unit/test_s1.py && make types && make up-db && make s2-smoke`

Result (2 Nov 2026): passed (exit 0).
