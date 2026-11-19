# Cairn acceptance test list v1

Status: v1, written in Phase 0 on 27 Oct 2026 (plan item 0.22). Sources: the MVP user stories in section 4 and the MVP exit criteria in section 16 of the [brainstorm](../brainstorm.md), and the phase checklists in the [development plan](../development_plan.md). Labels follow the brainstorm: Proposed target, Estimate, Verify.

This list gives every user story and every MVP exit criterion a named test, the plan items that build and run it, and the phase in which the test lands. Nothing in the product that these tests exercise exists yet. The list is a target for later phases, not a report of results. The product requirements are in `docs/prd.md`, and the threat tests are in the [threat model](threat-model.md).

## 1. How to read this list

- Ids. AT-US1 to AT-US9 map to the user stories US1 to US9. AT-EC1 to AT-EC5 map to the five MVP exit criteria. Each id appears once, in the first column of one row.
- Named tests. Every name in the "Named tests" columns is Proposed. The real function names may change when the test is written, and the row is updated then. A test that uses a threat model name (for example the not-found test shared with TM-03) is one test with two references, not two tests.
- Plan items. Numbers come from the development plan. A test lands in the phase of the plan item that contains it. Where a story needs work from several phases, the Phase column gives the phase in which the full story can be tested end to end, and the plan items show the earlier parts.
- Phase 0 groundwork. Some Phase 0 unit tests already exist for a building block of a story, such as the scenario oracle or the canary token generator. They test the scenario tooling, not the product, so they do not make a story "active". They are listed so that the link from the story to its fixture is visible.
- Targets. The thresholds in the exit criteria rows are Proposed targets from the brainstorm. They are targets, not results.

## 2. Status values

| Status | Meaning |
|---|---|
| planned | The test is named but not written. This is the status of every row in v1 |
| active in Phase 0 | A Phase 0 test for the story itself exists and runs in `make gates`. No row has this status in v1 |
| implemented | The test is written and runs in CI, but there is no passing evidence yet |
| passing | The test passes on a named commit, and the evidence is linked from `docs/evidence/` |

A test earns "passing" only with evidence. The evidence is a run log, a results file or a recording in `docs/evidence/` (or `docs/spikes/` for spike output), named by the phase and the plan item, and it records the commit hash, the date and the exact command. The status column changes in the same change that adds the evidence. A missed target keeps its measured value in the evidence and is reported as missed.

## 3. User stories

| Id | Statement | Named tests (Proposed) | Plan items | Phase | Status | Phase 0 groundwork |
|---|---|---|---|---|---|---|
| AT-US1 | Ben asks why payment-service uses PostgreSQL and gets a cited answer with the approval date and the implementing PR (journey J1) | `test_ask_why_postgres_cited_answer_with_approval_and_pr`, `test_citation_offsets_resolve_to_source_text` | 1.11, 1.19, 1.27 for cited answers; 2.3, 2.13 for decisions; 3.6 for PR ingestion; 4.1, 4.6 for the answer card | 4 | planned | none |
| AT-US2 | Ben opens the decision page and sees its timeline, current status and any superseding decision | `test_decision_page_shows_timeline_status_and_superseding`, `test_adr012_superseded_by_adr018_status_at_four_dates` | 2.9, 2.13, 2.18 | 2 | planned | none |
| AT-US3 | Asha asks what the team believed on 10 March and gets the as-of answer, clearly labeled | `test_as_of_answer_never_cites_evidence_recorded_later`, `test_flag_vendor_backdating_known_today_vs_believed_then`, `test_as_of_accuracy_c1_dev` | 2.9, 2.15, 2.16, 2.21, 2.24 | 2 | planned | `test_adr007_archaeology_question` and `test_adr014_backdating_at_the_reference_date` check the scenario oracle |
| AT-US4 | Dev works a review queue of candidate decisions and conflicts, and verifies, edits, disputes or rejects each one (journeys J4 and J6) | `test_review_queue_verify_edit_dispute_reject`, `test_adr015_conflict_shows_both_sides_and_is_not_auto_resolved` | 4.4, 4.5, 4.6, 4.14 | 4 | planned | none |
| AT-US5 | Priya removes Ben from a team on GitHub and the affected content disappears from search, answers, decision pages and MCP within the revocation SLO (journey J3) | `test_revocation_timing_p95`, `test_new_enemy_revoke_then_add_sensitive_file`, `test_canary_matrix_after_revocation_state` | 3.7, 3.8, 3.13, 3.18, 3.19, 3.22, 3.27; the MCP leg is covered by 4.15 | 3 | planned | `test_readers_of_each_scope_kind` checks who may read each scope |
| AT-US6 | Carla confirms that HR canary strings never appear for any engineering persona on any path | `test_canary_matrix_search_paths`, `test_canary_matrix_all_paths_and_states` | 1.22, 2.20, 3.22, 4.15, 5.14 | 1 | planned | `test_token_matches_the_documented_derivation_and_format` checks canary generation |
| AT-US7 | Claude Code acting for Asha calls pre_change_brief for a file path and receives decisions, constraints and owners within Asha's access (journey J5) | `test_pre_change_brief_returns_adr009_within_asha_access`, `test_mcp_token_scope` | 4.8, 4.9, 4.15, 4.17, 4.23 | 4 | planned | none |
| AT-US8 | Asha sees when each source last synced and when an answer depends on stale evidence | `test_freshness_badges_per_source_and_per_answer`, `test_answer_flags_stale_evidence` | 3.12, 3.14, 3.23 | 3 | planned | none |
| AT-US9 | Any user gets "not found in sources you can access", with what was searched, instead of a guess | `test_not_found_reply_identical_with_and_without_hidden`, `test_abstention_lists_what_was_searched` | 1.11, 1.23, 4.3, 4.13 | 1 | planned | none |

Notes on the user story rows:

- AT-US1 lands in Phase 4 because the implementing PR comes from the GitHub connector in Phase 3 and the answer card from Phase 4. A first version of the cited answer runs earlier, in Phase 1, over chat, meeting notes and Markdown only.
- AT-US5 shares the revocation timing test with AT-EC2. The story is satisfied by the scripted trials on the local stack in Phase 3. The exit criterion repeats the measurement on the demo deployment in Phase 6.
- AT-US6 starts in Phase 1 with search and answer paths and grows with each phase. The full matrix is an Estimate of about 390 assertions (6 personas, 13 paths, 5 states).
- AT-US9 lands first in Phase 1 with the standard not-found reply, and Phase 4 adds the list of what was searched.

## 4. MVP exit criteria

The thresholds are Proposed targets from section 16 of the brainstorm. Each row is checked in Phase 6 by plan items 6.12 to 6.16, using the evidence from earlier phases where the plan says so.

| Id | Statement | Named tests (Proposed) | Plan items | Phase | Status | Phase 0 groundwork |
|---|---|---|---|---|---|---|
| AT-EC1 | Canary matrix: 0 observed leaks across all personas, paths and states | `test_canary_matrix_full_frozen_build` | 6.12, 5.14, 1.22, 2.20, 3.22, 4.15 | 6 | planned | `test_token_matches_the_documented_derivation_and_format` checks canary generation |
| AT-EC2 | Revocation p95 and freshness p95 are 60 s or less, measured over at least 20 trials on the demo deployment | `test_revocation_timing_p95`, `test_freshness_timing_p95` | 6.13, 6.6, 3.13, 3.18, 3.23 | 6 | planned | none |
| AT-EC3 | Recall@5 and as-of accuracy are 0.85 or higher on the C1 test split | `test_c1_test_split_recall_at_5`, `test_c1_test_split_as_of_accuracy` | 6.14, 5.1, 5.4, 2.21 | 6 | planned | `test_adr007_archaeology_question` checks the scenario oracle behind the as-of gold labels |
| AT-EC4 | A B0 to B3 table with confidence intervals is published, failures included | `test_results_tables_regenerate_from_parquet`, `test_rerun_reproduces_deterministic_metrics` | 6.15, 5.9, 5.10, 5.12, 5.15 | 6 | planned | none |
| AT-EC5 | Live demos of journeys J1, J2, J3 and J5 run with no manual database edits | `test_demo_journeys_j1_j2_j3_j5_without_manual_db_edits` | 6.16, 1.27, 2.24, 3.27, 4.23 | 6 | planned | none |

Notes on the exit criteria rows:

- AT-EC1 reuses the matrix runs of AT-US6, and the Phase 6 run is on the frozen build. A single observed leak fails the row.
- AT-EC2 needs at least 20 trials per measurement, and the p95 and the trial count are both recorded. Phase 3 measures on the local stack. Phase 6 repeats on the demo deployment, or on the local run if the hosted deployment is cut. "The demo deployment" is the topology of [ADR-0014](adr/ADR-0014-free-tier-deployment-cloudflare-render-neon.md): the Cloudflare Pages project and its Function, one Render free web service running `cairn-combined`, and a Neon free project. The trials run on a warm instance, and the cold-start wake (about a minute for Render plus the Neon wake) is measured and reported separately, not inside the p95.
- AT-EC3 reports the C1 test split. The C1 dev split is used while building, and the test split is read for results only after C1 v1 is frozen in 5.1.
- AT-EC4 allows a missed target. The plan requires the miss to be reported with its measured value, so a missed target does not stop the table from being published.
- AT-EC5 covers J1 (ask why), J2 (time travel), J3 (revocation) and J5 (agent brief) from section 8 of the brainstorm. The proposed test is a scripted run plus a recording, and it fails if the run needs a manual database edit. The live demos run on the same ADR-0014 topology, on a warm instance; wake the service before the demo starts, and report the cold-start wake separately from the journey timings.

## 5. Keeping this list correct

- `make docs-check` runs `scripts/check_docs.py`, which requires AT-US1 to AT-US9 and AT-EC1 to AT-EC5 once each, every row with at least one named test, and every relative link in this file to resolve. It does not check that the plan numbers exist or that a test name exists in the code. Reviewers check both when a phase lands.
- When a test is written, replace its Proposed name with the real function name if they differ, and set the status to implemented.
- When the test passes on a named commit, set the status to passing and link the evidence in `docs/evidence/`.
- A new user story or exit criterion gets the next free id. Existing ids are never reused, because tests and other documents cite them.
- Plan items stay in the development plan. This list never ticks a plan item, and the user owns that step.
