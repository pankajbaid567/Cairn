# Evaluation plan and what is run by whom

How the evaluation runs, what each command does, the neuron schedule, and the steps only you can run. Results: [results.md](results.md) (generated). Reading guide: [error-analysis.md](error-analysis.md).

## Commands

| Command | What it does | Spends neurons? |
|---|---|---|
| `make eval-phase5` | B0, B1, B2, B3 on the dev and test splits against a throwaway database, plus the deterministic suites. Writes `docs/evidence/phase-5/runs/<date>-<mode>/` | only with `EVAL_MODELS=live` |
| `make eval-tables` | Regenerates `docs/results.md` from the Parquet files through DuckDB | never (a test blocks sockets) |
| `make c1-freeze` | Records the scenario hash, corpus hash and question counts | no |
| `make matrix` | The permission matrix, including all five MCP tools | no |
| `uv run cairn-s1 bench` | The ACL-filtered vector study (1, 5, 20, 50% selectivity), local database | no |

## Variants

* **B0**: dense retrieval only.
* **B1**: lexical + dense, reciprocal rank fusion, model rerank.
* **B2**: B1, and the sources of the decisions a question is about are promoted into the top results.
* **B3**: B2 through the full answer workflow with the verifier (answers, abstention, citations).

Models, prompts and seeds are pinned: the manifest of each run records the embedder and reranker keys, the scenario hash, the corpus hash, the lock-file hash and the seed.

## Neuron schedule for the live run (5.18)

The daily cap is 9,000 neurons per UTC day (never raised). The estimated cost per query, from `docs/results.md` (B3 about 15 neurons, B1 about 0.6, B0 about 0.03) puts one full pass of B0 to B3 over 46 questions at roughly 46 x (0.03 + 0.6 + 0.6 + 15) = about 750 neurons, plus seeding the corpus once (embedding about 33 documents, a few neurons per document). A full pass is therefore well inside one day.

* Day 1: seed, B0/B1/B2 on dev and test (about 60 neurons), then B3 on dev (about 230).
* Day 1 or 2: B3 on test (about 240); judge calls on the answers go through `glm-4.7-flash` (the judge model family differs from the answer model's) and are budgeted separately before they run; if a day's headroom is short, finish the judge on the next UTC day.
* Never raise the cap. If a run would pass it, the budget guard refuses the call and the run stops cleanly; resume with the cache.

Live numbers will replace these estimates automatically: the tables show the neurons actually reserved next to the estimate.

## Judge calibration (5.5)

Write `docs/evidence/phase-5/runs/<label>/judge-labels.csv` with columns `qid,human,judge` (1 = correct, 0 = not). The tables compute raw agreement and Cohen's kappa with a bootstrap interval, and refuse to count the judge until there are 50 labels. A person must do the labelling.

## Steps only you can run

1. `make eval-phase5 EVAL_MODELS=live` with the Workers AI token in `.env` (spends neurons; exits non-zero if a day's cap would be passed).
2. Label at least 50 answers for judge calibration.
3. C2 (PEP/KEP lifecycles and ADR histories) and C3 (EnterpriseRAG-Bench subset): check each dataset's licence first (open questions 10), then load only what the licence allows.
4. The optional Graphiti baseline (S6) and Qdrant/pgvectorscale arms are stretch items and are not built.
