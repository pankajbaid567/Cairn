# Task 16.1 notes: `docs/prd.md` (PRD v1)

Date: 27 Oct 2026. Only `docs/prd.md` and this file were created. No dependencies, scripts or other docs touched.

## Sections in `docs/prd.md`

| Heading | Source | Checker heading matched |
|---|---|---|
| Summary | brainstorm 1 | none (extra) |
| Problem and why now | 4 | problem |
| Personas | 4 | personas |
| Jobs to be done | 4 | jobs |
| User stories (US1 to US9, with acceptance pointers) | 4 | user stories |
| MVP scope (P0 table, product rules, P1 list, screens, accessibility aims) | 7, 8 | scope |
| Non-goals | 7 | non-goals |
| Journeys (J1 to J6, adoption loops) | 8 | journeys |
| Metrics (north star, guardrails, activation, quality table) | 8, 16 | metrics |
| MVP exit criteria (EC1 to EC5) | 16 | exit criteria |
| Assumptions, risks and open questions | 1, 19 | none (extra) |
| Traceability (US and EC to phases) | 18 | none (extra) |

## Links

Relative links used: `../brainstorm.md`, `../development_plan.md`, `adr/ADR-0001-postgres-paradedb-pgvector-store.md`, `adr/ADR-0005-evaluation-protocol.md`. `docs/acceptance-tests.md` (task 16.5) and `CONTRIBUTING.md` are plain text, so the PRD does not break `links` while they are missing.

## Differences from the brainstorm

- EC1 to EC5 are the five numbered criteria of section 16, given ids. US1 to US9 and J1 to J6 are verbatim in meaning, lightly shortened. The acceptance pointers, the traceability table and the `Summary` section are new; the phase mapping is my reading of the section 18 roadmap, not a brainstorm table.
- Priya's job and persona rows are kept; Dev's row says "review queue with owners" and drops the decision-debt view, because that view is P2 in section 7.
- Agents are one persona row ("agents acting for a user") with Claude Code acting for Asha as the example, as the design outline asks.
- Risks and open questions are condensed to the eight risks and nine questions that matter to MVP scope; the full tables stay in brainstorm section 19. Two questions were merged (embedding model and reranker).
- The PRD states that it makes no WCAG compliance claim.
- Style: no em dashes, no exclamation marks, no "ACME" (checked with grep).

## Verification

```text
$ uv run python scripts/check_docs.py --only prd
docs-check: ok (prd)
$ uv run python scripts/check_docs.py --only links
docs-check: ok (links)
```
