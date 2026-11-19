# Task 13.2 notes: development_plan.md revised for the free-tier target

Date: 1 Nov 2026.

## Scope

The spec planned targeted edits to a few rows of `development_plan.md` (design §16). The user expanded the scope to a full revision: every phase, table and checklist now matches the free-tier target from ADR-0013 (store), ADR-0014 (deployment and combined process) and ADR-0015 (models and neuron budget). Task 13.2 in `tasks.md` and the development-plan rows of design §16 were updated to say so.

Rules kept:

- Structure, phase.item numbering, Build, Test, Docs and Demo groups, exit gates and cut lists stay.
- No existing item was renumbered or removed. New items take the next free number in their phase and sit in their group, so numbers in a group are not always consecutive. The plan now says this under "How to use this plan".
- No checkbox ticked and no progress-log row added (FU9).
- brainstorm.md was not edited. Every link from the plan to a brainstorm section still resolves (checked with a slug script over both files; 36 links, 0 broken). New in-file anchor: `#deployment-target`. Phase headings are unchanged, so their anchors are too.
- New files referenced by the plan (`docs/deploy.md`, ADR-0013 to ADR-0015, `render.yaml`) are written in code spans rather than as links, because later tasks create them.

## Files

| File | Change |
|---|---|
| `var/source-docs-before/development_plan.md` | Copy before the edit (sha256 `9f3a942d…6732`) |
| `var/source-docs-before/development_plan.diff` | `diff -u` of the revision, 14 hunks |
| `development_plan.md` | Revised, 493 to 646 lines (sha256 `0c86255a…a53e`) |
| `docs/evidence/phase-0/source-docs.sha256` | Regenerated with `shasum -a 256 brainstorm.md development_plan.md`; brainstorm.md is unchanged (`c2df77fe…fc2d`). Task 13.3 regenerates it again after task 13.1 |
| `.kiro/specs/cairn-free-tier-retarget/tasks.md` | Task 13.2 block only: title, scope note, verify command and plan references |
| `.kiro/specs/cairn-free-tier-retarget/design.md` | §16: the sentence before the source-document table and the development_plan.md rows |

## Section by section

### Header and "How to use this plan"

- Window kept (27 Oct to 20 Nov). Added the revision date and the ADRs.
- Status line: Phase 0 skeleton (0.1 to 0.24) built locally 27 to 30 Oct on the ParadeDB image and being retargeted (0.25 to 0.54); ticking stays with the user.
- Decisions line names what ADR-0013 to ADR-0015 supersede or amend (D5 to D7, D18, D16).
- New bullets: stable item numbers; the retarget pushed Phase 0 past 29 Oct and later phases absorb the slip through the cut-line ladder; every item must fit the deployment target; ADR number allocation (0006, 0007, 0008, 0011, 0012 Phase 1; 0009, 0010 spikes; 0013 to 0015 retarget; next free 0016).

### Deployment target (new)

- Topology table (production and local per piece) and a diagram, including the Phase 3 edge webhook receiver.
- Free-tier limits table. `docs/evidence/free-tier/verify/FV0-platform-facts.md` did not exist when this task ran (task 1.1 writes it), so the values come from the retarget design's facts table and each row names its source docs with a pointer to FV0 for the URLs.
- Ten design rules: fit 512 MB (FT-18 budget), no in-process models, no always-on worker, sleep and wake with no keep-alive, the neuron budget, Neon's 1 GB storage, 100 CU-hours, edge quotas, no paid dependency, and laptop-only owner credentials.

### Phase table, global definition of done, quality gates

- Phase table: Phase 0 shows "built 27 to 30 Oct, retarget in progress" and the free-tier gate; Phase 1 adds "fits 512 MB"; Phase 3 adds drain-before-serve; Phase 5 adds the neuron cap; Phase 6 names the free-tier deployment.
- Definition of done gains three items: runs inside the free-tier emulation within the memory budget, no new paid dependency, neuron cost estimated and routed through the budget. The integration-test item notes that CI uses fakes or cached embeddings.
- Quality gates: Integration on the pgvector PostgreSQL 18 image (ADR-0013); new rows for the managed-mode matrix and the free-tier gate; eval smoke on cached embeddings with fakes for answers; types and unit tests cover the Pages Function; the secret scan covers `render.yaml` and `wrangler.toml`; a sentence explains that core and freetier never run together on the 3.83 GiB Docker VM.

### Phase 0

- Goal and deliverables gain the retarget.
- Rewritten items: 0.4 (profiles with freetier, pgvector image), 0.5 (vector and btree_gist, managed mode follows), 0.14 (lexical instead of BM25; 27 Oct ParadeDB run noted), 0.16 (readiness extension list), 0.20 (ADR-0001 superseded in 0.41), 0.24 (adds the measured 0.22 GiB peak from `docs/evidence/phase-0/core-ram-2026-10-30.md`).
- New sub-section "Free-tier retarget" with Build 0.25 to 0.36 (store swap, managed mode, S2 on FTS, combined process, proxy token, freetier profile, `make freetier-check`, Workers AI client, neuron budget, provider interfaces, deploy artifacts, `make env-update`), Test 0.37 to 0.40, Docs 0.41 to 0.44, Demo 0.45, and user-owned FU1 to FU9 as 0.46 to 0.54.
- Exit gate labelled EG1 to EG5 to match `scripts/phase0_exit.py`; EG5 is `make freetier-check`. The FU steps are outside the gate.
- Cut list: adds the optional live check and the hosted deploy steps; never cut adds the managed-mode matrix and the free-tier gate.

### Phase 1

- Deliverables: Workers AI embeddings, native FTS and HNSW, hosted rerank, default Workers AI answer model, demo persona mode, cached-embeddings eval, combined process.
- 1.1 S2 on FTS. 1.2 S3 chooses Workers AI models with neuron cost (ADR-0007 inside ADR-0015). 1.6 embeddings through the outbox (ADR-0011) and the budget. 1.7 FTS with `tsv_en` and `tsv_simple` per design §18, plus HNSW; the "native FTS for the B0 fallback" clause is gone. 1.10 B0 dense-only, B1 FTS plus dense plus RRF plus rerank, with a fallback when rerank fails. 1.11 default answer model and a fixed 503 on provider or budget failure. 1.12 demo persona mode on the deployment. 1.14 default Workers AI pair from different families, paid providers optional, budget in neurons (about 37 per answer, 61 per judged answer, from design §12), fakes first, spread over days. 1.15 cached-embeddings mode. 1.16 waking state. 1.17 S1 local only.
- New: 1.28 Phase 1 jobs in `cairn-combined`; 1.29 free-tier gate with Phase 1 code; 1.30 provider failure paths.
- 1.24 records model keys and neurons; 1.25 adds model costs; 1.26 adds ADR-0011 and ADR-0012; 1.27 demo on the deployed stack (user-run) or local.
- Exit gate adds neurons per answer and the free-tier gate. Cut list replaces "the native FTS fallback index" with the hosted demo run; never cut adds the budget and the gate.

### Phase 2

- LLM calls (2.1, 2.3, 2.8, 2.11) go through the neuron budget, with caching by revision hash, model key and prompt version, or by principal-set and input hashes. 2.12 rebuilds are idempotent DBOS steps that resume after sleep. 2.21 runs locally with cached embeddings; CI runs the deterministic part with fakes. 2.24 local or deployed. Exit gate adds the free-tier gate and the daily cap.

### Phase 3

- New paragraph on why the receiver sits at the edge: Render sleeps 15 min idle and wakes in about 1 min, GitHub expects a response within 10 s.
- 3.3 secret placement (webhook secret in Pages secrets, private key in Render env). 3.4 rewritten as the edge receiver: HMAC in constant time, thin inbox row with no payload text, `ON CONFLICT DO NOTHING`, 202, wake call to `/healthz`; default write path Neon's HTTP driver as a new INSERT-only role `cairn_edge`; fallbacks Hyperdrive or Cloudflare Queues (10,000 operations a day, 24 h retention); the same contract in the API for local runs with a forwarder.
- 3.5 started by the inbox drainer, outbox for extraction. 3.7 permission poll on every start and while awake; no always-on poller. 3.10 reconciler on start and while awake. 3.13 harness records warm or cold, defines revocation by probe send time and freshness by response, counts cold starts in every p95 and also reports warm and cold separately. 3.15 OAuth callback on the Pages origin.
- New: 3.28 drain-before-serve (ACL-class rows applied and the poll run before ready; query paths wait up to 20 s then fail closed with 503 `permissions_syncing`), 3.29 its test, 3.30 an ADR for the edge receiver (next free number, amends ADR-0014) and the TM-15 addition.
- 3.16 covers both receivers and the 10 ms CPU limit; 3.17 covers an edge outage and a stopped or sleeping API; 3.22 adds the pending-at-wake state; 3.18 and 3.23 are local, with 6.6 for the deployment.
- Exit gate: local SLO trials, deployment trials recorded with cold starts if up, the drain test, and the free-tier gate. Never cut adds drain-before-serve.

### Phase 4

- 4.1 LangGraph inside the memory budget, with plain async steps by ADR as the fallback; model calls through the budget. 4.4 judge from a different family. 4.6 screens handle the waking state. 4.8 MCP stdio on the user's machine against local or deployed API, never hosted. 4.9 per-token neuron share. 4.12 if built, inside `cairn-combined` behind the proxy. 4.20 latency locally, deployed warm and cold reported separately. 4.21 and 4.22 updated. Exit gate adds the free-tier gate.

### Phase 5

- Runs locally. A neuron plan table (Estimate) from the design §12 per-call costs: about 21,000 of the 27,000 neurons the cap allows over three days, which assumes a C1 test split of about 75 questions and ablations scored on retrieval metrics except verification.
- 5.2 and 5.3 sized to the plan, C3 local only. 5.4 answers cached. 5.5 glm judge or an optional paid judge. 5.6 synthetic vectors, no neurons, never on Neon. 5.7 experiments profile local only and never deployed. 5.8 cost in neurons and a dollar equivalent (paid rate is a Verify item). New 5.18 schedule against the cap and 5.19 regeneration makes no provider call. Exit gate adds the cap. Cut list adds step 8.

### Phase 6

- Deliverables and 6.3: deployment per ADR-0014 and `docs/deploy.md`, user-run; open question 12 answered; Oracle A1 (halved in June 2026, reclaims idle instances) and Supabase free (pauses after 7 days) rejected. The text "Decide Oracle A1 or local only" is gone.
- 6.1 includes the Function packages. 6.2 security review covers Neon owner BYPASSRLS, hosted secrets, proxy token, edge receiver and `cairn_edge`, egress to Workers AI. 6.4 seed from a data-only dump with stored embeddings, size guard at 512 MB. 6.5 adds the free-tier gate. 6.6 at least 5 of the 20 trials start asleep, cold starts counted. 6.8 README notes the cold start and limits. 6.13 says cold starts counted.
- New: 6.17 free-plan cost check with the "no keep-alive" note (about 186 of 100 CU-hours a month); 6.18 wake test.
- Exit gate: report all-trials and warm-only values if cold starts push freshness past 60 s.

### Spike tracker and Verify items

- S1 local only, status "Harness built in Phase 0". S2 asks about the FTS GIN and HNSW indexes, status "First run 27 Oct on ParadeDB BM25; full-text re-run pending". S3 asks about Workers AI models and the neuron budget.
- Verify list: the ParadeDB item becomes FTS and HNSW under RLS; the Oracle A1 capacity item becomes free-tier limits at deploy time; added the pgvector version on Neon, PostgreSQL 18 and SCRAM on Neon, Workers AI availability and rates, the edge receiver write path and CPU, a local webhook forwarder, GitHub's redelivery window, the paid neuron rate, and Workers AI data terms.

### Cut-line ladder, risk watchlist, progress log, backlog

- Cut ladder steps 8 (judged metrics beyond the neuron plan), 9 (hosted Phase 1 and 2 demos) and 10 (hosted deployment, leaving local plus video and the free-tier gate evidence). Never cut adds the free-tier gate and the budget guard.
- Risks: wording updated for FTS, polling on wake, neurons and the Docker VM. New rows: Render sleep against the SLOs, free-tier limits changing, neuron exhaustion, Neon storage or compute, the combined process over its memory budget, edge receiver limits.
- Progress log unchanged.
- Backlog: a paid upgrade path (paid Render, paid Neon, Workers AI past the allocation, Workers paid plan), each with its own ADR.

## Points for the orchestrator

- Requirement 20.1 says only the design §16 locations change. §16 now describes the full revision, but the requirement text was not edited (out of this task's scope). It may need a matching note.
- Design §18 says "B0 and B1 both use" FTS. Following the user's instruction and brainstorm H1, the plan makes B0 dense-only and B1 FTS plus dense plus RRF plus rerank. The Phase 1 spec revision should use the same split.
- Design §16 says AT-EC2 and AT-EC5 are "measured on a warm instance, with the cold-start wake reported separately". As the user asked, the plan counts cold starts in the reported p95 and also reports warm and cold separately. Task 12.3's acceptance-list note should use the same rule.
- The edge receiver (3.4, 3.28) adds a new role `cairn_edge` and a new ADR (next free number, ADR-0016 at the time of writing). Neither is part of this spec's code. They are Phase 3 plan items.

## Verify

```text
$ test -f var/source-docs-before/development_plan.diff && ! grep -q 'Decide Oracle A1 or local only' development_plan.md \
    && ! grep -q 'ParadeDB BM25 over chunk text' development_plan.md && grep -q 'ADR-0014' development_plan.md \
    && make docs-check && uv run pytest tests/scripts/test_phase0_exit.py
docs-check: ok (prd, adr, threat-model, acceptance, contributing, links)
17 passed
```

The `links` group of `make docs-check` covers `docs/**/*.md` and `CONTRIBUTING.md` only, so the plan's links were also checked separately with a heading-slug script (0 broken).
