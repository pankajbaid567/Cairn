# Source document edits and hash record (task 13.3)
Date: 3 Nov 2026.

`brainstorm.md` and `development_plan.md` were edited by this spec only: `brainstorm.md` in task 13.1 (design §16 table rows, ADR-0013, ADR-0014, ADR-0015) and `development_plan.md` in task 13.2 (full revision, scope expanded by the user). `docs/evidence/phase-0/source-docs.sha256` was regenerated with `shasum -a 256 brainstorm.md development_plan.md` after both edits, in `sha256sum` format.

## Digests
| Document | Old sha256 (before this spec) | New sha256 | Lines |
|---|---|---|---|
| `brainstorm.md` | `c2df77fec58281dcb0cc8484e53078018f0852a2164cc8c834a3e4107045fc2d` | `043c9c5109e2dd6df30fdbf5b67722d1960e9775ca341469b85adc890a76f578` | 1,145 to 1,162 |
| `development_plan.md` | `9f3a942dbe28dc5a2804ce1df452164137deec75e6f287affd12d243f6ac6732` | `0c86255a8e50ed860220c42e0462d3486471d1cd4670b3929aaec636dce4a53e` | 493 to 646 |

The old digests are those of the backups in `var/source-docs-before/` (`brainstorm.md` also equals the digest that `source-docs.sha256` held before task 13.2). The record held the new `development_plan.md` digest from task 13.2 until this task wrote the new `brainstorm.md` digest, which is the only value this task changed in the file.

## Files
| File | Content |
|---|---|
| `var/source-docs-before/brainstorm.md` | Copy taken before task 13.1 |
| `var/source-docs-before/brainstorm.diff` | `diff -u` of brainstorm.md, 15 hunks, 172 lines |
| `var/source-docs-before/development_plan.md` | Copy taken before task 13.2 |
| `var/source-docs-before/development_plan.diff` | `diff -u` of development_plan.md, 14 hunks, 717 lines |
| `docs/evidence/phase-0/source-docs.sha256` | Hash record with the new digests |

`var/` is gitignored, so the backups and diffs stay on this machine. To re-derive the diffs: `diff -u var/source-docs-before/<name>.md <name>.md`. Both diff files were compared with a fresh `diff -u` of the backup and the current document: the hunks and bodies are identical, and only the timestamps in the two header lines can differ.

## brainstorm.md: changed lines
Line numbers are those of the new file (old file in brackets where the hunk shifted them). Counts are lines added and removed. Every hunk maps to a row of the design §16 table.

| Hunk | Section | Change | Design §16 row | ADR | + / - |
|---|---|---|---|---|---|
| 52 | §1 TL;DR | "One database" bullet: PostgreSQL 18 with pgvector and native full-text search, Neon in production | TL;DR bullet | 0013 | 1 / 1 |
| 67 to 75 | Decision log | D5 store on plain PostgreSQL 18 with pgvector (Neon); D6 native full-text search, no ParadeDB; D7 "on Neon" and the 1 GB revisit condition | D5, D6, D7 | 0013 | 3 / 3 |
| 78 to 86 | Decision log | D16 Workers AI embeddings and rerank, free Workers AI answer model, Anthropic and OpenAI optional; D18 profiles with freetier, demo on Cloudflare Pages + Render free + Neon free, or local plus video | D16, D18 | 0015, 0014 | 2 / 2 |
| 155 to 167 | §3 change table | Row 10 native full-text search for B0 to B3; row 16 free hosting outcome | rows 10 and 16 | 0013, 0014 | 2 / 2 |
| 449 | §9 diagram | Node "PostgreSQL 18 with pgvector, Neon in production" | §9 diagram node | 0013 | 1 / 1 |
| 507 to 514 | §9 stack table | Rows Database and Lexical | stack table rows | 0013 | 2 / 2 |
| 520 to 526 | §9 | Compose profile sentence (pgvector Postgres, RAM measured in Phase 0, freetier profile) | compose profile sentence | 0014 | 1 / 1 |
| 724 to 730 | §13 | Retrieval candidates: native full-text search | retrieval candidates | 0013 | 1 / 1 |
| 834 to 840 | §16 | Variant sentence: B1 adds native full-text ranking, RRF and a hosted cross-encoder | variant sentence (B1) | 0013, 0015 | 1 / 1 |
| 913 to 920 | §17 | Spike rows S2 (full-text GIN and pgvector under FORCE RLS) and S3 (Workers AI models and the neuron budget) | §17 spike rows S2, S3 | 0013, 0015 | 2 / 2 |
| 946 to 952 | §19 | Risk 11: ParadeDB dropped, only Grafana is AGPL-3.0 | risk 11 | 0013 | 1 / 1 |
| 966 to 972 | §19 | Open question 12 answered by ADR-0014 (Oracle A1 and Supabase free rejected) | open question 12 | 0014 | 1 / 1 |
| 985 to 991 | §20 | Résumé bullet: stack list without ParadeDB | résumé bullet | 0013 | 1 / 1 |
| 1072 to 1094 | References | R64 to R80 appended (Render, Neon, Cloudflare, Workers AI model pages, Supabase) | References | n/a | 17 / 0 |
| 1155 to 1162 | Appendix C | Rows S2 and S3 | Appendix C rows S2, S3 | 0013, 0015 | 2 / 2 |

R numbers: the reference list ended at R63, so the new rows take R64 to R80. No existing R number was changed.

## development_plan.md: changed lines
`development_plan.diff` has 14 hunks, 278 lines added and 125 removed in total. The section-by-section description is in `task-13.2-notes.md`. Hunks by line range in the new file (a hunk can span two sections):

| New-file lines | Sections touched | Change | + / - |
|---|---|---|---|
| 1 to 82 | Header, "How to use this plan", new "Deployment target" | Revision date, status, ADR list, stable item numbers, topology, free-tier limits, design rules | 59 / 10 |
| 93 to 104 | Global definition of done | Free-tier emulation, no paid dependency, neuron cost | 4 / 1 |
| 106 to 141 | Quality gates, start of Phase 0 | pgvector integration image, managed-mode matrix, free-tier gate; Phase 0 goal and deliverables | 16 / 10 |
| 144 to 254 | Phase 0, new "Free-tier retarget" sub-section, start of Phase 1 | Items 0.4, 0.5, 0.14, 0.16, 0.20, 0.24 reworded; new items 0.25 to 0.54; exit gate EG1 to EG5; cut list; Phase 1 goal | 72 / 22 |
| 258 to 306 | Phase 1, start of Phase 2 | Items 1.1, 1.2, 1.6, 1.7, 1.10 to 1.17 reworded; new 1.28 to 1.30 | 16 / 13 |
| 312 to 318 | Phase 2 | One line | 1 / 1 |
| 321 to 416 | Phase 2 end, Phase 3, start of Phase 4 | Neuron budget and caching; edge webhook receiver, drain-before-serve, cold starts in the SLO; new 3.28 to 3.30 | 35 / 28 |
| 421 to 489 | Phase 4 end, Phase 5 | Workflow inside the memory budget, MCP stdio on the laptop; Phase 5 run locally with a neuron plan (5.18, 5.19) | 30 / 15 |
| 495 to 528 | Phase 5 end, Phase 6 start | Deployment per ADR-0014 and `docs/deploy.md`; 6.3 rewritten; new 6.17, 6.18 | 13 / 10 |
| 533 to 546 | Phase 6 end, post-MVP backlog | Rewording for the free-tier target | 3 / 3 |
| 562 to 576 | Backlog end, spike tracker | S2 and S3 rows | 4 / 3 |
| 580 to 599 | Spike tracker end, Verify list | Verify items | 10 / 3 |
| 609 to 619 | Cut-line ladder, start of risk watchlist | Cut steps 8 to 10 | 4 / 1 |
| 622 to 641 | Risk watchlist | Free-tier risk rows | 11 / 5 |

No checkbox was ticked and no progress-log row was added (FU9).

## Not changed, for a later decision
These places in `brainstorm.md` still describe the previous design (BM25, local models, Oracle A1). They are outside the design §16 table, so tasks 13.1 and 13.3 left them as they are, and no edit was made to either source document in this task. Line numbers are those of the current `brainstorm.md` (digest `043c9c51…8f78`).

| Line | Section | Current text (shortened) | What a later edit would say |
|---|---|---|---|
| 315 | §6 product concept diagram | `Evidence index (BM25 and vectors)` | "full-text search and vectors" |
| 517 | §9 stack table, Models row | "local embeddings and reranker from S3"; revisit "Latency target missed" | Workers AI embeddings and hosted rerank (ADR-0015), as D16 now says |
| 533 | §9 repository tree | `packages/retrieval    bm25, dense, fusion, graph, rerank, verify` | Name the lexical module for full-text search (for example `lexical` or `fts`) |
| 673 | §11 permission model | "may stop ParadeDB and pgvector index use" | "may stop full-text (GIN) and pgvector index use" |
| 730 | §13 retrieval pipeline step 5 | `Cross-encoder rerank.` | "Hosted cross-encoder rerank (Workers AI, ADR-0015)" |
| 906 | §18 roadmap, row 1 | "embeddings, BM25 and vectors, ACL tables ..." | "embeddings, full-text search and vectors, ..." |
| 945 | §19 risk 7 | Mitigation "Small models for extraction" | Wording on the answer and extraction models, now Workers AI defaults with a neuron budget |
| 948 | §19 risk 10 | Trigger "A1 capacity errors (Verify)" | Free-plan wake or limit errors (ADR-0014); Oracle A1 is rejected |
| 952 | §19 risk 14 | Mitigation "Compose profiles, small models" | Compose profiles; no in-process models |

Left on purpose as history, not candidates for change:
- Line 71 (D6), line 158 (§3 row 10) and line 511 (stack table Lexical alternatives) name ParadeDB BM25 as the superseded option.
- Lines 1044 and 1045 (references R33, R34) and line 1105 (glossary entry "Cross-encoder") are neutral reference text.
- Line 949 (risk 11) names ParadeDB as dropped.

`development_plan.md` still mentions ParadeDB, BM25 and Oracle A1 only as history or as rejected options (lines 5, 52, 130, 147, 166, 172, 174, 211, 572, 630 and 512); none of those needs a decision.

## Verification
- `shasum -a 256 -c docs/evidence/phase-0/source-docs.sha256` from the repository root.
- `uv run pytest tests/scripts/test_phase0_exit.py`.
- `make phase0-exit-preview`.
Results are in the task report.
