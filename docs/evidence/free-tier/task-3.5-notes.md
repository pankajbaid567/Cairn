# Task 3.5: S2 at 20,000 rows on native full-text search

Date: 2 Nov 2026 (UTC date in the report names; local clock was 3 Nov 01:10 IST). Requirement 3.7; design §5; plan items 0.14 and 1.1. Input for ADR-0006 in Phase 1. The task does not decide the ADR.

## What was run

| Item | Value |
|---|---|
| Hash of the 27 Oct files | `var/s2-27oct.sha`, written before the run |
| Command | `make s2-run S2_ARGS="--database cairn_spike_s2_t35 --drop-after"` |
| Rows, dimensions, repeats | 20,000, 384, 3 (the `mk/spikes.mk` defaults: `S2_ROWS`, `S2_DIM`, `S2_REPEATS`) |
| Dataset | S1 generated, uncorrelated ACLs, `content_sha256` `9f452554…880d`, the same hash as the 27 Oct dataset |
| Image | `pgvector/pgvector:0.8.6-pg18`, digest `sha256:2ba9ca5f2e7daa0f0e7723cba1ee9167bab54efd3640516a44ac1a928dd67e7a`; PostgreSQL 18.6; `vector` 0.8.6, `btree_gist` 1.8 |
| New files | `docs/spikes/S2/2026-11-02-report.md`, `docs/spikes/S2/2026-11-02-results.json` (schema `cairn-s2-results/2`) |
| Plans | 117, default-deny checks passed |
| Duration | a few minutes; `cairn-db-1` memory stayed at about 190 MiB of its 1 GiB limit at the two samples taken |

Two deviations from the Verify line, both for the database rules of this run:

- `make s2-run` was given `S2_ARGS` so that the harness used its own `cairn_spike_s2_t35` database and dropped it at the end. The default (`cairn_spike_s2`, kept after the run) is not named `cairn_spike_s2_*`.
- The first attempt was cut off by a wrong timeout (1.5 s instead of 1,500 s) before it wrote anything. No database was left behind; the run above is the second attempt.

Outcome of the check: `shasum -a 256 -c var/s2-27oct.sha` printed `OK` for `2026-10-27-report.md` (`01f88620…6942`) and `2026-10-27-results.json` (`79f1b034…4a8b09f`; full values in `var/s2-27oct.sha`). The harness needed no source change at this size. After the run the server held `cairn`, `cairn_dbos`, `postgres`, `template0` and `template1` only; the `cairn_fv4_*` objects were not touched.

## Environment differences from 27 Oct

The 27 Oct run used ParadeDB 0.26.3, which tuned the server on first start. The pgvector image uses PostgreSQL defaults plus the compose limits. This matters when reading the absolute times.

| Setting | 27 Oct (ParadeDB image) | 2 Nov (pgvector image) |
|---|---|---|
| `shared_buffers` | 327688kB | 163848kB |
| `effective_cache_size` | 983048kB | 5242888kB |
| `work_mem` | 15360kB | 4096kB |
| `max_parallel_workers_per_gather` | 4 | 2 |
| Heap size of `s2.chunks` | 41,418,752 bytes | 12,959,744 bytes (the BM25 index is gone; `chunks_fts_idx` is 2,023,424 bytes) |

The query terms are the same topic token group (`t38_0 … t38_3`). Under the `simple` configuration they match 301 of 20,000 rows (1.5%), read from the superuser FTS plan (`Bitmap Index Scan` on `chunks_fts_idx`, 301 rows). Everything below holds for a rare term. A common term was not measured.

## Full-text search under FORCE RLS

`cairn_app`, median of 3 runs, ranges over the three iterative-scan settings (the lexical queries do not read that setting, so the spread is noise). Times in ms.

| Variant | p01 (195 rows visible) | p05 (1,039) | p20 (3,958) | p50 (10,016) | FTS index used |
|---|---|---|---|---|---|
| `fts` (policy only) | 0.10 to 0.29 | 0.45 to 0.53 | 1.58 to 3.49 | 4.35 to 4.81 | no, 12 of 12 |
| `fts_explicit_acl` (policy and `acl && …`) | 0.10 to 0.11 | 0.44 to 0.58 | 1.52 to 1.57 | 3.56 to 4.18 | no, 12 of 12 |
| `hybrid_rrf` | 0.41 to 0.73 | 2.02 to 2.08 | 2.15 to 2.37 | 3.71 to 3.86 | no, 12 of 12 |
| `hybrid_rrf_explicit_acl` | 0.40 to 0.46 | 1.98 to 2.04 | 7.59 to 7.77 | 3.77 to 4.00 | no, 12 of 12 |

Plan shape in all 48 lexical and hybrid plans under RLS: `Bitmap Index Scan` on `chunks_acl_gin_idx` with the policy qual as index condition, then a `Bitmap Heap Scan` that applies `body_tsv @@ …` as a `Filter`. At p50 that means 10,016 rows fetched and 9,873 removed to keep 143 (4.8 ms); at p01, 195 fetched, 193 removed, 2 kept (0.29 ms). With the explicit predicate the `Filter` also carries `acl && '{1003}'`, and nothing changes in the plan shape. `chunks_fts_idx` never appears in a `cairn_app` plan. The ACL filter location is `index` for every lexical and hybrid plan.

The rows scanned grow with the number of rows the policy admits, so the cost grows with visibility, not with the match count. This run shows that over 195 to 10,016 visible rows at 20,000 rows. It does not show how the cost behaves at 200,000 rows or more.

Cause (from task 3.1, unchanged): `ts_match_vq` (`@@`) and `arrayoverlap` (`&&`) are not leakproof (`proleakproof = f`), so PostgreSQL evaluates the RLS policy before them and cannot take them as index conditions. The numbers here confirm the consequence at 20,000 rows.

## The same queries without RLS

The superuser rows show what the planner does when the policy is not in the way. They are a stand-in for planner behavior, not a measurement of a SECURITY DEFINER function (none was built in this task).

| Variant, superuser | p01 | p05 | p20 | p50 | FTS index used |
|---|---|---|---|---|---|
| `fts` (no ACL predicate) | 1.88 to 2.15 | (same query) | (same query) | (same query) | yes, 3 of 3 |
| `fts_explicit_acl` | 0.23 to 0.29 | 0.32 to 0.35 | 0.71 to 0.77 | 1.33 to 1.38 | yes, 12 of 12 |
| `hybrid_rrf_explicit_acl` | 0.58 to 0.73 | 2.44 to 2.84 | 1.22 to 1.45 | 1.70 to 1.85 | yes, 12 of 12 |

With the explicit predicate and no RLS, the plan is a `BitmapAnd` of `chunks_acl_gin_idx` and `chunks_fts_idx`, 301 FTS rows intersected with the ACL rows, and the heap fetch drops to the 143 matching rows at p50.

Against the RLS plans of the same query and principal:

| Principal | `fts_explicit_acl` under RLS | `fts_explicit_acl`, superuser | Faster |
|---|---|---|---|
| p01 | 0.10 to 0.11 | 0.23 to 0.29 | RLS plan |
| p05 | 0.44 to 0.58 | 0.32 to 0.35 | superuser plan, by about 0.1 to 0.2 ms |
| p20 | 1.52 to 1.57 | 0.71 to 0.77 | superuser plan, about 2x |
| p50 | 3.56 to 4.18 | 1.33 to 1.38 | superuser plan, about 3x |

At p01 the 195 admitted rows are fewer than the 301 FTS matches, so the ACL-only bitmap is cheaper than the intersection. Above that the FTS index pays off, and the gap widens with the number of visible rows. The absolute differences are at most about 3 ms at this size.

## Next to the 27 Oct BM25 numbers (a different engine)

The 27 Oct figures come from ParadeDB's `bm25` index (a custom scan that reads the lexical query itself), with different server settings (table above). They are not a like-for-like comparison with native GIN full-text search. Times in ms, `cairn_app` under RLS, ranges over the iterative-scan settings, from `2026-10-27-report.md`.

| Variant | 27 Oct, BM25: p01 | p05 | p20 | p50 | 2 Nov, FTS: p01 | p05 | p20 | p50 |
|---|---|---|---|---|---|---|---|---|
| lexical, policy only | 33.0 to 74.3 | 25.3 to 40.4 | 11.1 to 19.7 | 23.1 to 35.3 | 0.10 to 0.29 | 0.45 to 0.53 | 1.58 to 3.49 | 4.35 to 4.81 |
| lexical, explicit predicate | 0.40 to 0.70 | 0.65 to 1.25 | 1.27 to 1.56 | 2.17 to 4.76 | 0.10 to 0.11 | 0.44 to 0.58 | 1.52 to 1.57 | 3.56 to 4.18 |
| hybrid, policy only | 21.9 to 155.9 | 28.5 to 60.1 | 29.4 to 37.4 | 16.0 to 26.7 | 0.41 to 0.73 | 2.02 to 2.08 | 2.15 to 2.37 | 3.71 to 3.86 |
| hybrid, explicit predicate | 0.97 to 1.27 | 1.89 to 3.32 | 2.15 to 2.28 | 2.50 to 2.77 | 0.40 to 0.46 | 1.98 to 2.04 | 7.59 to 7.77 | 3.77 to 4.00 |

What differs in kind: under BM25 the explicit predicate was the difference between tens of milliseconds and about a millisecond (the plans differ: a parallel top-k scan with the policy as a recheck filter without it, a normal scan with it; I did not establish why the first was slower). Under native FTS the planner already starts from the ACL index, so with or without the explicit predicate the RLS plan is the same and the times are within noise. At this size the FTS-under-RLS latencies are lower than the BM25 ones in the policy-only rows and comparable in the explicit rows. The superuser BM25 baseline with the explicit predicate was 6 to 44 ms; the FTS equivalent is 0.2 to 1.4 ms.

## Vector search: planner choice at 1% and 5%, and recall

Same as the old report in the pattern, with the new image. ACL location `index` means the planner used `chunks_acl_gin_idx` and sorted the visible rows exactly; `filter` means an HNSW index scan with the policy applied as a filter on its output.

| Principal | `hnsw` (policy only) | `hnsw_explicit_acl` |
|---|---|---|
| p01 (1%) | ACL bitmap and exact sort, HNSW not used, recall 1.000 | same |
| p05 (5%) | HNSW with post-filter | ACL bitmap and exact sort, HNSW not used, recall 1.000 |
| p20, p50 | HNSW with post-filter | HNSW with post-filter |

Planner choices were the same as on 27 Oct for the `hnsw` variants. For `hybrid_rrf` the HNSW side was not used at p01 and p05 (27 Oct: only p01), and for `hybrid_rrf_explicit_acl` at p01, p05 and p20 (27 Oct: p01 and p05). I did not investigate why; the FTS side and the table size changed.

HNSW recall at 10 (against exact search with the same visibility, 10 query vectors), `cairn_app`, from the `hnsw` variant, where the HNSW index is read:

| Principal | `iterative_scan = off` | `relaxed_order` | `strict_order` |
|---|---|---|---|
| p05 | 0.210 (4 rows returned) | 0.990 (10 rows) | 0.990 (10 rows) |
| p20 | 0.730 (8 rows) | 1.000 | 1.000 |
| p50 | 1.000 | 1.000 | 1.000 |
| p01 | 1.000 (exact path) | 1.000 | 1.000 |

Without iterative scan the post-filter can return fewer than 10 rows (4 at p05, 8 at p20). Both iterative modes returned 10 rows at all selectivities. On 27 Oct the same cells were 0.210, 1.000 and 1.000 at p05; the 0.990 is new. The dataset hash is equal, so the HNSW graph built in this run differs a little from the earlier one. I did not check why. `hnsw_explicit_acl` shows the same recall for the cells where HNSW is used (p05 off 0.210 in the superuser baseline, p20 and p50 as above). Iterative-scan latencies are all under 1 ms for these cells.

## What this means for ADR-0006 (input only)

The question was whether an explicit predicate in a SECURITY DEFINER function could let the FTS index be used when RLS alone cannot. The numbers show:

1. Under RLS alone, with or without an explicit predicate in the query text, the FTS index was used in 0 of 48 lexical and hybrid plans at 20,000 rows. The explicit `acl && …` predicate changes nothing for `cairn_app`, because the policy runs first and the added `@@` and `&&` quals are non-leakproof filters.
2. Without the policy in the way, the same explicit-predicate query used `chunks_fts_idx` in 24 of 24 superuser plans (lexical and hybrid), through `BitmapAnd` with the ACL index. It was about 2x to 3x faster than the RLS plan at p20 and p50 and slower at p01 (195 admitted rows, fewer than the 301 matches). So the planner can use the FTS index when the check is an explicit predicate and not a policy.
3. The size of the effect at this scale is at most about 3 ms per query. Both approaches meet any plausible latency target at 20,000 rows. The cost under RLS grows with the number of rows the policy admits and was not measured above 20,000 rows or for a common term.
4. No SECURITY DEFINER function was built or measured. A function would give the planner the explicit predicate only if the function's owner bypasses RLS (superuser or BYPASSRLS), because `FORCE ROW LEVEL SECURITY` also applies to the table owner. In that case the explicit predicate is the only access check, and the default-deny checks of this harness (which test the policy) would need an equivalent for the function. On the managed free tier the owner would be a non-superuser role; whether it can hold BYPASSRLS there is part of FV3 and was not tested here.
5. The vector side behaves the same under RLS and in the baseline: the planner chooses between the ACL bitmap (exact) and HNSW with a post-filter, and iterative scan restores 10 rows with recall 0.99 to 1.00 in this data.

## Files

- `docs/spikes/S2/2026-11-02-report.md`, `docs/spikes/S2/2026-11-02-results.json`
- `var/s2-27oct.sha` (untracked, scratch)
- Unchanged: `docs/spikes/S2/2026-10-27-report.md`, `docs/spikes/S2/2026-10-27-results.json`

## Verify

`shasum -a 256 docs/spikes/S2/2026-10-27-* > var/s2-27oct.sha && make up-db && make s2-run && shasum -a 256 -c var/s2-27oct.sha`

Result (2 Nov 2026): hash taken first; `make up-db` healthy; `make s2-run` (with the `S2_ARGS` above) exit 0; hash check `OK` for both 27 Oct files.
