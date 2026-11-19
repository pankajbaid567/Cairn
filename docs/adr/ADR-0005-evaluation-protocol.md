---
status: accepted
date: 2026-11-03
decision-makers: Pankaj Baid
---

# ADR-0005: Evaluate on three corpora and four variants with deterministic checks for permissions and time, and a calibrated judge for the rest

Records decision D17 of the [brainstorm decision log](../../brainstorm.md#1-tldr-and-decision-log), and the model-pinning and judge rules of D16. The plan is in [brainstorm section 16](../../brainstorm.md#16-evaluation-plan).

## Context and Problem Statement

Cairn claims three things that are easy to assert and hard to show: answers are correct and cited, answers respect time, and nobody sees what they cannot open. A demo proves none of them. Vendor numbers in this space are vendor-reported and not independently checked.

The question: what protocol makes Cairn's results reproducible, honest about failures, and comparable between a plain dense baseline and the full system, given a small team, a laptop and hosted models that change under us?

## Decision Drivers

* Permissions, as-of logic and leaks must be checked by code that cannot be argued with, not by a model.
* Judged metrics must be calibrated against humans and report their uncertainty.
* Runs must be reproducible: pinned models, prompts, corpus versions and seeds.
* Results must include failures and per-category breakdowns, not only averages.
* The synthetic corpus will be too clean, so real lifecycle data and a public enterprise benchmark are also needed (brainstorm risk 9).

## Considered Options

* Three corpora (C1 synthetic DecisionBench, C2 real lifecycles, C3 EnterpriseRAG-Bench subset), variants B0 to B3 with ablations, deterministic suites for permissions and time, and a human-calibrated judge from another model family (chosen).
* The synthetic scenario only.
* A public benchmark only.
* A judge model for all metrics, including permissions and time.
* Reference-free RAG scores (RAGAS style) as the main metrics.

## Decision Outcome

Chosen option: "Three corpora, variants B0 to B3 with ablations, deterministic suites for permissions and time, and a human-calibrated judge from another model family", because each corpus covers a gap the others leave, and the split between deterministic and judged checks keeps the claims that matter most free of judge error.

### Corpora

* C1 DecisionBench: the synthetic Kestrel Pay scenario (about 150 questions at freeze, plan item 5.1) with gold decisions, spans, as-of answers, conflicts and permission expectations, split into dev and test. Phase 0 starts with about 40 questions, at least one per twist, validated by `cairn-scenario validate`.
* C2 real lifecycles: PEP and KEP subsets with their status, Replaces and Superseded-By fields, plus ADR histories from published replication packages if their licenses allow (Verify, open question 10). Git history supplies real recorded times.
* C3 EnterpriseRAG-Bench subset: GitHub, Confluence and Slack documents with Basic, Semantic, Project, Conflicting and Info-not-found questions, scored with its correctness, completeness, document recall and invalid-extra-document metrics. It has no ACLs, so it measures retrieval and answering only. Dataset-card terms are Verify.

### Variants and ablations

All variants share the answer model and prompt.

* B0: dense-only retrieval with pgvector.
* B1: adds native full-text search ranking (`ts_rank_cd`, ADR-0013), reciprocal rank fusion and a hosted cross-encoder reranker (ADR-0015).
* B2: adds decision records, temporal versions and graph expansion.
* B3: adds the permission layers L1 to L7 (ADR-0004), freshness, verification and abstention.
* Ablations switch off rerank, graph expansion, the decision index, verification, iterative scans, and each of L4, L5 and L7.

### Metrics and Proposed targets

These are targets we set. They are not results.

| Metric | Proposed target | Corpus |
|---|---|---|
| Recall@5 | >= 0.85 | C1 |
| Citation precision | >= 0.90 | C1, C2 |
| As-of accuracy | >= 0.85 | C1, C2 |
| Conflict surfacing | >= 0.80 | C1 |
| Abstention | >= 0.80 | C1, C3 |
| Leaks (canary strings or restricted ids in any output) | 0 observed, on a finite matrix, which is not proof | Canary matrix |
| Revocation p95 | <= 60 s | Timing harness |
| Freshness p95 | <= 60 s | Timing harness |
| Latency p95, answer cache off | <= 5 s | C1 |
| Cost per query | Reported | All |
| Correctness and completeness | Reported against B0 | C3 |

### Protocol rules

* Pin models, prompts, corpus versions and seeds per run. Models sit behind interfaces. The embedding model and the reranker are chosen in spike S3 from the Workers AI catalog (ADR-0015) and recorded in ADR-0007.
* The judge comes from a different model family than the answer model. It is calibrated on at least 50 human-labeled answers, and agreement is reported. Agreement below 0.8 reopens D17 (brainstorm risk 8). The answer and judge model pair is chosen on 20 questions (open question 5).
* Reference-free scores (RAGAS style) are diagnostics only.
* Permissions, as-of logic and leaks use deterministic tests, never judges. They are reported separately from judged metrics.
* The canary matrix is 6 personas by 13 paths by 5 states, about 390 assertions (Estimate). Each asserts that no canary string or restricted id appears in the output, the user-visible logs or any count. Canaries are generated tokens and literal phrases planted by the scenario, in the families hr, partner, security and incident.
* The ACL-filtered vector study uses about 200K synthetic chunks (Estimate) with ACL labels and principals at 1%, 5%, 20% and 50% selectivity. It measures recall@10 against exact search, p50 and p95 latency, and index size for the strategies listed in brainstorm section 16. Its Phase 0 harness is `cairn-s1`. Its outcome, the chosen strategy, goes to ADR-0008. Its Proposed target is a recall@10 ratio of at least 0.95 at 5% selectivity with p95 <= 150 ms.
* Report bootstrap 95% confidence intervals and per-category results. Label errors with the taxonomy: retrieval miss, ranking miss, extraction error, temporal error, missed conflict, permission over-filtering, false verification reject, abstention error.
* Results are written to Parquet and queried with DuckDB. One command regenerates every table.
* The MVP exit criteria are: 0 observed leaks across all personas, paths and states; revocation and freshness p95 <= 60 s over at least 20 trials; Recall@5 and as-of accuracy >= 0.85 on the C1 test split; a published B0 to B3 table with confidence intervals, failures included; and live demos of J1, J2, J3 and J5 with no manual database edits.

Revisit if (from the decision log): judge agreement with human labels is below 0.8.

### Consequences

* Good, because the claims that matter most (no leaks, correct as-of answers) rest on deterministic checks.
* Good, because the same scenario file drives the validator, the oracle, the canary matrix and the questions, so labels and tests cannot drift apart silently.
* Good, because confidence intervals, per-category tables and an error taxonomy make failures visible.
* Good, because C2 and C3 test the system on data we did not write.
* Bad, because the synthetic corpus is cleaner than real company data, and near-perfect C1 scores would be a warning sign, not a success.
* Bad, because a finite canary matrix cannot prove the absence of leaks.
* Bad, because a calibrated judge needs at least 50 human labels, which is real effort, and hosted models can change under a pinned name.
* Bad, because C2 and C3 redistribution terms are unverified, which may shrink those corpora.

### Confirmation

Active in Phase 0:

* `make scenario-validate` passes on the Kestrel Pay scenario and fails on the three broken fixtures (plan item 0.18). Every twist has a gold question (rule TW-1), and every canary family has a leak question (rule Q-2).
* Canary tokens are deterministic and unique, checked by the unit and property tests of `canary_token` and by rule CAN-3.
* The as-of oracle is tested against the dated cases in ADR-0003, and validator rule Q-3 checks gold lifecycle labels against it.
* The S1 generator and the S2 harness run end to end on small data (`make s1-smoke`, `make s2-smoke`), which makes the ACL-filtered vector study repeatable.

Planned in later phases:

* Plan item 5.12: a re-run with the same seeds reproduces deterministic metrics exactly and judged metrics within their confidence intervals.
* Plan item 5.13: deterministic suites (permissions, as-of, leaks) report separately from judged metrics.
* Plan item 5.10: one command regenerates every table from Parquet through DuckDB.
* Plan item 5.14: the full canary matrix passes on the frozen build.
* ADR-0007 (models, S3) and ADR-0008 (vector ACL strategy, S1) record the pinned models and the chosen strategy.

## Pros and Cons of the Options

### Three corpora, B0 to B3 with ablations, deterministic suites, calibrated judge

* Good, because each corpus and each metric type covers a weakness of the others.
* Bad, because it takes more time to build than a single benchmark.

### The synthetic scenario only

* Good, because it is fully controlled and has canaries and time twists by construction.
* Bad, because it can only show that Cairn handles cases we thought of.

### A public benchmark only

* Good, because the data is not ours.
* Bad, because EnterpriseRAG-Bench has no ACLs and no time axes, so it cannot test the two things Cairn is built for.

### A judge model for all metrics

* Good, because it is cheap and flexible.
* Bad, because a judge can pass a leak or accept a wrong date, and neither failure is acceptable for permissions or as-of logic.

### Reference-free RAG scores as the main metrics

* Good, because they need no gold labels.
* Bad, because they do not measure permission or time behaviour. They stay as diagnostics.

## More Information

* [Brainstorm section 16, evaluation plan](../../brainstorm.md#16-evaluation-plan)
* [Brainstorm section 17, the Kestrel Pay scenario](../../brainstorm.md#17-synthetic-company-kestrel-pay)
* [Brainstorm Appendix C, spike plans](../../brainstorm.md#appendix-c-spike-plans)
* [Development plan, Phase 5](../../development_plan.md)
