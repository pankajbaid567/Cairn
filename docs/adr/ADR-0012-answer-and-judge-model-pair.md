---
status: proposed
date: 2026-11-04
decision-makers: Pankaj Baid
---

# ADR-0012: Answer with gemma-4-26b-a4b-it and judge with glm-4.7-flash, until the live model pick decides otherwise

Refines [ADR-0015](ADR-0015-workers-ai-models-and-neuron-budget.md). Decision PD1-13 of the Phase 1 design. **Status proposed: the pick has not been run live.** It needs about 2,100 neurons of real Workers AI calls on 20 questions and the user's token.

## Context and Problem Statement

Which answer model and which judge model, from different model families, give correct cited answers within 5 s p95 and an acceptable cost per query? The family rule is enforced by the settings validator, so a same-family pair cannot be configured.

## Considered Options

* Answer `@cf/google/gemma-4-26b-a4b-it` (Google), judge `@cf/zai-org/glm-4.7-flash` (Zhipu): the ADR-0015 defaults, both on Workers AI.
* Answer `@cf/meta/llama-3.1-8b-instruct-fp8-fast` (Meta).
* An Anthropic or OpenAI answer model with the user's own key (adapters exist; both are optional).

## Decision Outcome

Provisional choice: the defaults. `cairn-eval pick-models` compares the candidates on the first 20 dev questions with the judge prompt `judge-v1` and picks the highest judged correctness with p95 at most 5 s, then the lower cost. The budget per answer becomes 1.5 times the chosen model's p95 cost per query, rounded up (Estimate before the run: about 37 neurons per answer and 61 per judged answer).

## Consequences

* Good: no paid dependency; both models run on the free allocation.
* Bad: the reasoning models spend output tokens on reasoning, so `max_tokens` has to cover both, and an empty reply with `finish_reason: length` is treated as a failure.
* The answer path never returns an uncited answer: a provider failure is a 503 with a fixed code.

## Confirmation

Run `cairn-eval pick-models --run-id <id>` (user-run, can span UTC days with `--resume`). The rows go to `docs/evidence/phase-1/model-pick/<run_id>/rows.jsonl` and the chosen pair and budget replace this section.
