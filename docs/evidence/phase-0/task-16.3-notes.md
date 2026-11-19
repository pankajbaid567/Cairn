# Task 16.3 notes: threat model v1

Date: 27 Oct 2026. Only `docs/threat-model.md` and this file were created. No code, dependency, git, `brainstorm.md` or `development_plan.md` change.

## Files

| File | State |
|---|---|
| `docs/threat-model.md` | New. Eight sections: scope and assets, actors, trust-boundary diagram (Mermaid flowchart), threat table, OWASP mapping, residual risks, Phase 0 controls, review plan |
| `docs/evidence/phase-0/task-16.3-notes.md` | New. This file |

## TM id mapping

Numbering is identical in the design (section 16) and brainstorm section 15, so there is no renumbering.

| Id | Threat | Planned test (Proposed name) | Phase |
|---|---|---|---|
| TM-01 | Retrieval leak | `test_canary_matrix_search_paths` | 1 |
| TM-02 | Derived leak | `test_canaries_in_hidden_only_support_sets` (2.17) | 2 |
| TM-03 | Existence leak | `test_not_found_reply_identical_with_and_without_hidden` (1.23) | 1 |
| TM-04 | Revocation lag | `test_revocation_timing_p95` (3.18) | 3 |
| TM-05 | Cache leak | `test_cross_persona_cache` (3.21) | 3 |
| TM-06 | Indirect prompt injection | `test_injected_issue_scenario` (4.16) | 4 |
| TM-07 | Knowledge poisoning | `test_poisoned_doc_questions_c1` | 5 |
| TM-08 | Embedding inversion | `test_deletion_purges_embeddings` (3.20) | 3 |
| TM-09 | Webhook forgery | `test_bad_webhook_signature_rejected` (3.16) | 3 |
| TM-10 | MCP token misuse | `test_mcp_token_scope` (4.17) | 4 |
| TM-11 | RLS bypass | `test_app_role_cannot_bypass_rls` (0.17, plus schema invariant test) | 0 |
| TM-12 | Cross-tenant leak | `test_two_tenant_fixture` | 5 |
| TM-13 | LLM egress | `test_llm_payload_log_review` | 1 |
| TM-14 | Supply chain | `test_supply_chain_gates_in_ci` (label for `make audit secrets licenses`) | 0 |

## Table layout and checker rules

The threat table has the columns Test id, Threat, Vector, Mitigation, Planned test, Phase, so every row has six filled cells. Phases are bare digits.

The checker counts a TM id as a definition when it is the whole content of a table cell, in any table. Two tables in the first draft tripped `TM-ID-DUPLICATE`, and both were rewritten:

- The OWASP table lists threat names with the id inside a longer cell (`Indirect prompt injection (TM-06)`) and ranges such as `TM-01 to TM-05 and TM-13`.
- The Phase 0 controls table names threats in words (RLS bypass, Supply chain, and so on), not by bare id.

Prose mentions of ids are not counted.

## Differences from the design and brainstorm

- Planned test names. The design gives test descriptions only (for example "canary matrix, search paths"). I added `test_` names so the planned test cell is concrete. They are marked Proposed in the document, and the later phases may rename them.
- TM-14 has no single pytest function in the design. The document says so and uses a label name for the CI gate run.
- Added to the brainstorm table: a mitigation in TM-14 of "dependency audit, secret scan, license check" (the design section 10 gates), and TM-06 mitigation "untrusted sources excluded by default" (brainstorm section 14, MCP). The TM-11 mitigation says "less restricted tables" as in the design, where the brainstorm says "public tables".
- OWASP: the brainstorm table names prompt injection without an OWASP number. I left it unnumbered and did not add LLM01.
- Residual risks: the four from brainstorm section 15, plus public-source poisoning, audit retention (brainstorm open question 14, review around 7 Nov), no authentication in Phase 0 (design Security Considerations) and the Phase 0 superuser and owner credentials with the OQ-2 write-role plan.
- Controls table has no status column. Several controls (compose ports, gitleaks, license checker) belong to tasks other agents are writing now, and `docker-compose.yml` did not exist when I wrote this. Status stays in the evidence folder and `make gates`, and the document says so.
- The trust-boundary diagram shows the worker writing as `cairn_owner`, with the note that a separate ingest role comes in Phase 1 (OQ-2). This is the design's current position, not a final one.

## Links

The document links only to `../brainstorm.md` and `../development_plan.md`. ADRs, the PRD, `CONTRIBUTING.md` and `docs/acceptance-tests.md` are mentioned as plain text (ADR-0006 appears once, as a plain reference to the S2 outcome on 30 Oct).

## Verification

```text
$ uv run python scripts/check_docs.py --only threat-model
docs-check: ok (threat-model)
$ uv run python scripts/check_docs.py --only links
docs-check: ok (links)
```

Not verified: that the Mermaid diagram renders (no renderer was run; labels are quoted and contain no semicolons or hash signs), and that R-numbers match the live URLs (taken from the brainstorm reference table as written).
