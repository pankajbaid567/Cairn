# Cairn: permission-aware decision memory, and what went wrong building it

## The problem

Engineering teams lose the reasons behind decisions. The reasons live in design docs, chat threads, meeting notes and pull requests, and the people who can read each of those differ. A search tool that indexes everything either leaks (it shows a snippet from a private channel) or is useless (it hides everything). A second problem is time: "what did we decide?" has different answers depending on the date asked about and the date we learned it.

## Design

**Access is the foundation, not a filter on top.** Every chunk, embedding, decision event and support row carries a copy of its ACL. Retrieval runs as a database role that can read content only through row-level security, and every registered query also carries an explicit `acl && groups` predicate; a test strips the predicate and checks that the database alone returns the same rows. This was a measured decision: the full-text index is unused under RLS (0 of 48 plans), and for vectors the filter strategy was chosen from a benchmark at 1, 5, 20 and 50% selectivity (`iterative_strict`, `ef_search` 100, recall 0.99 at 20% against 0.75 for the default).

**Decisions are bitemporal.** Events record what a source said, with a valid time and a recorded time. Derived rectangles (valid period x recorded period) carry a GiST exclusion constraint so a decision never has two statuses at one point. Backdating, supersession and corrections are one move. The first version of the read path used these rectangles directly and failed a test: a reader who could not see the event that made a decision "implemented" saw it "absent", and the rectangle's end date also leaked when a hidden event happened. The read path now derives status from only the events the reader can see.

**Claims are visible through full support sets.** A claim has one or more sets of sources; it is visible only if every item of one set is readable. A conflict is visible only if every side is.

**Live sync is fail-closed.** Webhooks land in a thin inbox at the edge (identifiers only, an insert-only database role). The jobs process applies permission changes first and content after, and every query path waits for permission events received before it, then returns 503 rather than answering with stale access. Sources are re-read on notification, so duplicates and reordering do not matter.

**Answers are verified.** The workflow is seven plain steps; the model is called once, with only evidence the asker may read, as delimited data. A verifier drops claims the cited text does not support, dates that are not in the evidence, citations outside the visible set, and any canary-format token not in the visible evidence. If nothing survives, the reply is one constant "not found".

**Agents get a read-only door.** An MCP server with five tools runs on the user's machine with a per-agent token that acts with exactly its user's access.

## Results (what is and is not known)

Measured and reproducible, with fake models: zero leaks on 46 questions across four retrieval variants and both splits; 50 permission-matrix tests pass; as-of status correct for 12 of 14 questions (the two misses are the system correctly refusing to know something the asker cannot read); freshness p95 0.78 s and revocation p95 0.015 s locally over 20 trials each (the second is the time to fail closed). Full tables with intervals: [results.md](results.md).

**Not known: how good the answers are with real models.** The only evaluation run uses deterministic stand-in models, so retrieval and answer-quality numbers are regression baselines, not results. The accuracy targets (Recall@5 and as-of accuracy of at least 0.85 on the test split) are unchecked. The evaluation set has 46 questions, not the planned 150, so the intervals are wide (for example Recall@5 of B1 on the 12 eligible test questions is 1.000 with an interval of [1.000, 1.000] only because there is no variation, not because it is certain).

## Failures and surprises

* **A test, not a review, found the biggest design bug** (the hidden-event leak above). Seeing "absent" for a decision you can see is wrong, and the boundary of a hidden row is information.
* **The verifier's first version dropped valid claims** because it tokenized `postgresql.` and `postgresql` differently. A unit test on a trivial sentence found it.
* **The free-tier gate caught six bugs that unit tests did not**: a missing import that crashed startup, a `Secure` cookie that a plain-HTTP client would not return, an environment allowlist, a script path, a test oracle that needed a superuser role, and finally a shutdown that exceeded its budget because finalizing a Python process with about a thousand modules takes longer than 30 s at 0.1 CPU. The fix was to exit directly after a clean shutdown.
* **I broke my own code with a lint command**: running ruff with `--select RUF100` deleted justified `noqa` comments across the repository, and there was no git to restore them. I re-added them where ruff flagged lines. The lesson is operational: a repository that is not under version control makes bulk fixes dangerous.
* **Two expected labels disagree with the permission model** (Q029, Q043). The scenario assumes the asker knows about a pull request they cannot read. I kept the permission model and reported the misses.
* **The name collides** with other "Cairn" projects in the same niche. Not resolved.

## What was not done

Deployment, the GitHub organisation and App, real-model runs, judge labels, the usability check, the video, GitHub OAuth, admin view-as, and the C2/C3 corpora. [development_plan.md](../development_plan.md) lists each with its status.
