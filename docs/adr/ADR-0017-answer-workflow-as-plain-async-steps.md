---
status: accepted
date: 2026-11-04
decision-makers: Pankaj Baid
---

# ADR-0017: Run the seven-step answer workflow as plain async functions, not a LangGraph graph

Changes decision D14 (a LangGraph workflow with checkpoints). Plan item 4.1 allows this change with an ADR.

## Context and Problem Statement

D14 asked for a graph runtime with the steps analyze, plan, retrieve, expand, rerank, compose and verify, checkpoints, and a trace span per step, running inside `cairn-combined` on a 512 MB, 0.1 CPU instance. The workflow is fixed and linear: there is no branching on model output, no loop, no tool choice and no action tool. Does it need a graph runtime?

## Considered Options

* LangGraph with a checkpointer, as in D14.
* Plain async functions called in a fixed order by one orchestrating function, with a per-step observer.

## Decision Outcome

Chosen option: "plain async functions", because the control flow is a straight line, each step already has a clear input and output, and a graph runtime would add a dependency tree, import time and resident memory to the process with the tightest budget in the system, for no behaviour the straight line lacks.

* Steps: `analyze` and `plan` are pure and use no model; `retrieve` is the ACL-filtered pipeline including the model rerank; `expand` reads the first valid date of decisions the question names; `rerank` applies the score threshold and packs the evidence; `compose` calls the model once; `verify` drops what does not check out.
* Each step runs inside an observer context; the API opens one trace span per step (`workflow.<step>`), and the step names and milliseconds are returned with the answer. An abstention carries neither, so the not-found reply stays one constant.
* Checkpoints: none. The workflow is short (seconds) and idempotent; a failed run is retried by the caller, and the only expensive side effects (model calls) go through the neuron budget and the answer cache.
* The model sees only delimited evidence blocks labelled with source kind and tier; the workflow has no tools to call.

**Not measured:** LangGraph's actual memory footprint was never measured here. The decision rests on the absence of any need for a graph, not on a measured failure to fit. If a later phase adds branching (for example a second retrieval pass), revisit this ADR.

## Consequences

* Good: no new dependency, no new lock entries, nothing extra resident.
* Good: every step is an ordinary function, testable alone (`analyze` and `plan` are unit tested).
* Bad: no automatic checkpoint or resume, and no graph visualisation.
* Bad: a future branching workflow would need hand-written control flow or this decision revisited.

### Confirmation

`apps/api/tests/integration/test_workflow.py` checks the seven steps in order with timings, one span per step, and that an abstention carries no trace. `make freetier-check` must pass with the workflow loaded (plan 4.20).
