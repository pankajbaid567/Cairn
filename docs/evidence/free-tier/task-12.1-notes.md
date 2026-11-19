# Task 12.1: ADR-0013 to ADR-0015, the ADR index and the checker

Date: 3 Nov 2026. Requirements 7.3, 19.1 to 19.4, 19.7; decision FT-25; design §16. No Cloudflare, Neon or Render call was made. `.env` and the volume `cairn_pgdata` were not touched. `brainstorm.md` (task 13.1) and `packages/retrieval/tests` were not edited.

## Files

| File | Change |
|---|---|
| `docs/adr/ADR-0013-postgres-pgvector-fts-neon-store.md` | New, `accepted`, 2026-11-03. Supersedes ADR-0001. Store on PostgreSQL 18 with pgvector and native full-text search; managed-mode bootstrap; OQ-1 plaintext-password decision; the `0001_baseline` in-place edit with both checksums; the S2 finding that the full-text index is unused under FORCE RLS because `@@` and `&&` are not leakproof (0 of 48 plans at 20,000 rows); the old store named only in the "History" subsection |
| `docs/adr/ADR-0014-free-tier-deployment-cloudflare-render-neon.md` | New, `accepted`. Cloudflare Pages + Pages Function, one Render free web service in combined mode, Neon free; rejected Oracle A1, Supabase, Render Postgres, Cloudflare Containers, Neon with the old store (not named) and local-only; DBOS in `cairn_dbos` and the outbox; sleep semantics; the FV5 finding that DBOS polling (about 9.5 transactions/s) keeps Neon awake while Render is awake, so Neon CU-hours (about 400 awake hours at 0.25 CU) bind before Render's 750 hours; the design §18 table of Phase 1 defaults; the measured RAM result |
| `docs/adr/ADR-0015-workers-ai-models-and-neuron-budget.md` | New, `accepted`. Workers AI over REST, retry policy, interfaces and fakes, the four default models with licenses (FV10: Gemma 4 is Apache-2.0, so OQ-5 needs no handling) and neuron rates (FV9), the family rule, the neuron budget and its Postgres store, FV9 reasoning tokens, egress and data terms |
| `docs/adr/ADR-0001-postgres-paradedb-pgvector-store.md` | `status: superseded by ADR-0013` and a pointer line under the title; nothing else changed (date kept) |
| `docs/adr/ADR-0004-layered-authorization.md` | One sentence in "The S2 fallback": full-text instead of ParadeDB, plus the 2 Nov finding; date set to 2026-11-03 |
| `docs/adr/ADR-0005-evaluation-protocol.md` | B0 and B1 lines (native full-text ranking and a hosted reranker instead of BM25) and the S3 sentence (Workers AI catalog instead of local models); date set to 2026-11-03 |
| `docs/adr/README.md` | Index rows for ADR-0013 to ADR-0015, ADR-0001 marked `superseded by ADR-0013`, new dates for ADR-0004 and ADR-0005, status sentence, reserved table ADR-0006 to ADR-0012 (0006, 0007, 0008, 0011, 0012 Phase 1; 0009, 0010 spike tracker; next free ADR-0016) with the ADR-0006 and ADR-0007 questions reworded, supersession bullet |
| `scripts/check_docs.py` | `ADR_NUMBERS = (1, 2, 3, 4, 5, 13, 14, 15)` replaces `ADR_COUNT`; `ADR_EXPECTED_STATUS = {1: "superseded by ADR-0013"}` and `expected_adr_status()`; keyword table for ADR-0013, ADR-0014 and ADR-0015 (design §16); docstring. `THREAT_IDS` is untouched (task 12.2) |
| `tests/scripts/test_check_docs.py` | Samples for a superseded status (ADR-0001 only, rejected elsewhere), a missing new ADR, a missing keyword in each new ADR, reserved numbers not checked, and the repository's own ADR group |
| `tests/scripts/test_check_docs_properties.py` | Property 18 over all eight numbers, with the expected status per number and the new keyword phrases |
| `tests/scripts/test_no_paradedb.py` | Two tests: ADR-0013 names the old store only under a heading titled "History" (heading-stack scan), and the scanner's own samples. The allowlists are unchanged |

## Facts used and where they come from

FV0 (platform facts, rejected options), FV1 (image pin), FV3 and the OQ-1 decision, FV4 (extra grants), FV5, FV6, FV9, FV10, FV11, FV13, task 2.2 (checksums), task 3.1 and 3.5 (leakproofness, plan counts, latencies), task 6.3 and the 3 Nov RAM report (94.34 and 94.52 MiB, 0 of 298 failed), task 8.3 (budget), design §16 and §18.

## Verify

| Command | Result |
|---|---|
| `uv run pytest tests/scripts/test_check_docs.py tests/scripts/test_check_docs_properties.py tests/scripts/test_no_paradedb.py` | 90 passed |
| `uv run python scripts/check_docs.py --only adr --only links` | ok |
| `make docs-check` | ok (all six groups) |
| `make fmt-check lint types` | clean |
| `uv run pytest tests/scripts -m "not integration and not freetier"` | 416 passed, 2 failed: `test_phase0_exit.py::test_recorded_hashes_match_the_real_documents` and `test_main_returns_zero_in_preview_on_this_repo`. Both fail on `brainstorm.md changed: sha256 …`, which is task 13.1's edit; `source-docs.sha256` is regenerated in task 13.3 |

## Notes

- The new ADRs and the History rule: ADR-0014 and ADR-0015 do not name the old extension or its vendor, so they stay clean when task 12.3 widens `test_no_paradedb.py` to `docs/`. ADR-0013 names them only in its History subsection and refers to the scan test by description elsewhere, because that file's name contains the vendor name.
- Design §16 says ADR-0001 "nothing else changes"; its date stays 2026-10-27. ADR-0004 and ADR-0005 received in-place corrections and their dates moved.
- A single read-only `git status` was run by mistake while editing, against the ground rule "never run git commands". It changed nothing.
