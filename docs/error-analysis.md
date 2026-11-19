# Error analysis

What failed, why, and what the next run must check. The tables are in [results.md](results.md) (generated); this note explains them. It describes the **fake-model** run recorded on 2026-11-04. A fake run says nothing about quality with real models, so every "next run" line is a thing to look at when the live numbers exist.

## Failure classes (taxonomy)

`leak`, `citation_failure`, `degraded`, `not_retrieved` (eligible question, no gold source in the top 5), `partially_retrieved`. Each row gets the first class that applies, in that order.

## What the fake run showed

1. **No leaks and no citation failures on any variant, either split.** The deterministic permission suite found 0 leaks, and the canary matrix passes (50 tests, [evidence](evidence/phase-5/matrix-2026-11-04.txt)).
2. **`not_retrieved` and `partially_retrieved` happen almost only in B0** (dense only). Example: dev Q002 and test Q044, Q049. B1 (hybrid + rerank) fixes them; the paired difference is +0.08 (dev) and +0.21 (test) with intervals touching zero, because there are only 18 and 12 eligible questions. Do not read the fake embedder's ranking as evidence about a real one: the fake is a hashing model, and the lexical half of B1 is doing the work here.
3. **One `partially_retrieved` remains in B1/B2/B3: dev Q017.** Several gold sources are needed and the fake reranker keeps only some of them in the top 5. Next run: check whether a real reranker and a real embedder retrieve the missing source.
4. **B2 (decision index) changes nothing on this set.** Promoting a decision's source events to the top of the list helps only when the gold source is an event source that lexical and dense retrieval both miss, and here B1 already finds them. Next run: if B2 still equals B1, report it as no measurable gain rather than dropping the variant silently.
5. **B3 answered every question, including the ones that should abstain.** `abstention correct` is 23/30 on dev and 12/16 on test. The deterministic extractive stand-in model answers whenever any evidence is packed, and the fake reranker scores unrelated passages high enough to pack them. A real model plus a real reranker score threshold is what produces abstention. Next run: this is the first number to read, because a system that never abstains fails the permission questions in the way users notice (a confident answer from irrelevant text).
6. **The verifier dropped no claims in the fake run** (the extractive model copies sentences from the evidence, so every claim is supported). Its behaviour is tested directly in `test_verify.py`; a live run will show real drop rates.

## Deterministic suites

* **As-of status: 12 of 14 questions correct** (dev 7/8, test 5/6). The two misses are explained, not hidden: Q029 (asker `dev`, ADR-004) and Q043 (asker `mira`, ADR-007) expect the status `implemented`, but the event that makes it so comes from a pull request the asker cannot read (`pr-platform-infra-31`, `pr-payment-service-123`). Cairn derives status from the events a person can see ([ADR-0009](adr/ADR-0009-decision-store-and-claim-visibility.md)), so it answers `accepted`. The scenario's expected value assumes knowledge the asker does not have. This is a disagreement between the scenario's labels and the permission model, and the permission model wins; the labels for these two questions should say what a reader of that source set can know. Until then both stay as reported misses.
* **Must-not-cite: 0 violations in 4.** An as-of search never returned a source recorded after the requested date.

## Not measured yet

* Answer correctness (needs a calibrated judge: at least 50 human labels, see [evaluation-plan.md](evaluation-plan.md)).
* Anything about real models: Recall@5 target (>= 0.85) and as-of accuracy target (>= 0.85) are checked on the live run. On this fake run the deterministic as-of accuracy is 0.857 and Recall@5 for B1 is 0.98 (dev) and 1.00 (test), which the intervals show are not yet distinguishable from each other with so few questions.
* Ablations for L4, L5 and L7 as toggles (they are tested as always-on guarantees, not compared).
* C2 and C3 corpora.
