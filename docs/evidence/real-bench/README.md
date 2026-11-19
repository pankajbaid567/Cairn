# Real-corpus benchmark: 150 Python Enhancement Proposals

Run on 2026-11-19. 150 PEPs (2007 chunks), 200 questions, Titan v2 and Cohere Embed v3 (1,024 dimensions), `gpt-oss-20b` LLM reranker, `gpt-oss-120b` answers, `qwen3-32b` judge, all on Amazon Bedrock in ap-south-1; local PostgreSQL 18 with pgvector.

What is real: the PEP text, titles, authors, dates and statuses. What is synthetic: the three people who ask and the access rules (about 1 PEP in 11 is steering-only, 1 in 6 core-only, chosen by a hash of the PEP number). The questions are AI-drafted (content questions have a quote that was checked to appear verbatim); header and unanswerable questions are made by code. Dev and test are a 50/50 split by question id; choices were made on dev and the test split is reported separately.

## Retrieval ablation (answerable questions, document level)

A question counts as a hit when its source PEP is among the top 5 (or 10) documents. 95% intervals are bootstrap over questions (2,000 resamples).

### Dev (72 questions)

| Variant | Hit@5 | Hit@10 | MRR@10 | nDCG@10 |
|---|---|---|---|---|
| lexical | 0.917 (0.847–0.972) | 0.931 (0.861–0.986) | 0.753 (0.673–0.830) | 0.798 (0.726–0.864) |
| dense_titan | 0.944 (0.889–0.986) | 0.944 (0.889–0.986) | 0.917 (0.854–0.965) | 0.924 (0.863–0.971) |
| dense_cohere | 0.944 (0.889–0.986) | 0.944 (0.889–0.986) | 0.900 (0.831–0.958) | 0.912 (0.848–0.965) |
| hybrid_titan | 0.972 (0.931–1.000) | 0.972 (0.931–1.000) | 0.893 (0.831–0.947) | 0.913 (0.859–0.959) |
| hybrid_cohere | 0.958 (0.903–1.000) | 0.958 (0.903–1.000) | 0.884 (0.819–0.942) | 0.903 (0.845–0.952) |
| hybrid_titan_rerank | 0.986 (0.958–1.000) | 0.986 (0.958–1.000) | 0.914 (0.861–0.958) | 0.933 (0.890–0.969) |
| hybrid_cohere_rerank | 0.972 (0.931–1.000) | 0.972 (0.931–1.000) | 0.928 (0.875–0.972) | 0.940 (0.891–0.979) |

### Test (71 questions)

| Variant | Hit@5 | Hit@10 | MRR@10 | nDCG@10 |
|---|---|---|---|---|
| lexical | 0.873 (0.789–0.944) | 0.930 (0.859–0.986) | 0.707 (0.617–0.787) | 0.762 (0.684–0.832) |
| dense_titan | 0.958 (0.901–1.000) | 0.958 (0.901–1.000) | 0.944 (0.880–0.986) | 0.947 (0.891–0.990) |
| dense_cohere | 0.958 (0.901–1.000) | 0.958 (0.901–1.000) | 0.887 (0.815–0.948) | 0.904 (0.841–0.959) |
| hybrid_titan | 0.958 (0.901–1.000) | 0.958 (0.901–1.000) | 0.896 (0.829–0.951) | 0.911 (0.851–0.961) |
| hybrid_cohere | 0.958 (0.901–1.000) | 0.972 (0.930–1.000) | 0.875 (0.807–0.932) | 0.900 (0.843–0.948) |
| hybrid_titan_rerank | 0.958 (0.901–1.000) | 0.958 (0.901–1.000) | 0.910 (0.844–0.961) | 0.922 (0.861–0.968) |
| hybrid_cohere_rerank | 0.944 (0.887–0.986) | 0.944 (0.887–0.986) | 0.837 (0.765–0.902) | 0.864 (0.799–0.921) |

### All (143 questions)

| Variant | Hit@5 | Hit@10 | MRR@10 | nDCG@10 |
|---|---|---|---|---|
| lexical | 0.895 (0.839–0.944) | 0.930 (0.888–0.965) | 0.730 (0.672–0.784) | 0.780 (0.732–0.827) |
| dense_titan | 0.951 (0.916–0.979) | 0.951 (0.916–0.979) | 0.930 (0.892–0.965) | 0.936 (0.897–0.969) |
| dense_cohere | 0.951 (0.916–0.986) | 0.951 (0.916–0.986) | 0.894 (0.846–0.936) | 0.908 (0.865–0.946) |
| hybrid_titan | 0.965 (0.930–0.993) | 0.965 (0.930–0.993) | 0.894 (0.852–0.933) | 0.912 (0.874–0.946) |
| hybrid_cohere | 0.958 (0.923–0.986) | 0.965 (0.937–0.993) | 0.880 (0.834–0.919) | 0.902 (0.862–0.937) |
| hybrid_titan_rerank | 0.972 (0.944–0.993) | 0.972 (0.944–0.993) | 0.912 (0.871–0.948) | 0.928 (0.893–0.959) |
| hybrid_cohere_rerank | 0.958 (0.923–0.986) | 0.958 (0.923–0.986) | 0.883 (0.838–0.924) | 0.902 (0.864–0.938) |

### Paired differences, all answerable questions (positive = first is better)

| Comparison | Δ nDCG@10 | Δ Hit@5 |
|---|---|---|
| hybrid vs dense (Titan) | -0.023 (-0.046 to -0.002) | +0.014 (+0.000 to +0.035) |
| hybrid vs lexical-only | +0.132 (+0.092 to +0.175) | +0.070 (+0.035 to +0.112) |
| LLM rerank on vs off (Titan) | +0.015 (-0.013 to +0.044) | +0.007 (+0.000 to +0.021) |
| LLM rerank on vs off (Cohere) | +0.001 (-0.031 to +0.032) | +0.000 (-0.028 to +0.028) |
| Cohere v3 vs Titan v2 (hybrid) | -0.011 (-0.032 to +0.010) | -0.007 (-0.021 to +0.000) |
| Cohere v3 vs Titan v2 (hybrid + rerank) | -0.025 (-0.056 to +0.004) | -0.014 (-0.042 to +0.007) |

### Latency per search (ms, 4 searches in parallel, so these include some queueing)

| Variant | p50 | p95 | searches where a stage degraded |
|---|---|---|---|
| lexical | 934 | 13851 | 2 of 200 |
| dense_titan | 1030 | 15703 | 3 of 200 |
| dense_cohere | 4023 | 51234 | 10 of 200 |
| hybrid_titan | 899 | 9956 | 1 of 200 |
| hybrid_cohere | 3241 | 55401 | 16 of 200 |
| hybrid_titan_rerank | 2544 | 32125 | 14 of 200 |
| hybrid_cohere_rerank | 3271 | 51844 | 21 of 200 |

Permission leaks (a restricted PEP in the results of someone who may not read it): 0 across all variants and 33 restricted questions each ({'lexical': 0, 'dense_titan': 0, 'dense_cohere': 0, 'hybrid_titan': 0, 'hybrid_cohere': 0, 'hybrid_titan_rerank': 0, 'hybrid_cohere_rerank': 0}).

## Answers (embedder titan, rerank score floor 2.0)

| Measure | Result |
|---|---|
| Questions that should be answered: answered | 96 of 143 (67%) |
| Questions that should be refused: refused | 50 of 57 (88%) |
| Answered questions that cite the gold PEP | 91 of 96 (95%) |
| Answered questions that cite anything | 96 of 96 |
| Restricted PEP cited to someone who may not read it | 0 |
| AI judge: correct / partial (of answered and judged 96) | 78 (81%) / 12 |
| AI judge: every claim supported by the cited quotes | 67 of 96 (70%) |
| Calls that errored | 0 |

| Kind (expected) | Right decision (answer vs refuse) |
|---|---|
| content (answer) | 55 of 94 |
| header (answer) | 24 of 26 |
| permission (abstain) | 26 of 33 |
| permission (answer) | 17 of 23 |
| unanswerable (abstain) | 24 of 24 |

### Examples of questions wrongly refused

- (content) Why might an author receive a non‑sequential PEP number instead of the next available one? — gold PEP [1], retrieved [1], packed [1, 1, 1]
- (content) Why does the proposal recommend using UTF-8 rather than UTF-16 for certain encodings? — gold PEP [100], retrieved [263, 538, 756, 597, 100], packed [263, 538, 756, 597, 100]
- (content) Why does the existing Python object comparison protocol not work well for NumPy array comparisons? — gold PEP [207], retrieved [207, 688], packed [207, 207, 207, 207, 207]
- (content) When using the proposed '@' operator for outer product iteration, what form should the resulting tuples have? — gold PEP [211], retrieved [211, 646, 225], packed [211, 211]
- (content) How does the built‑in set type let you perform common set operations using operators? — gold PEP [218], retrieved [225, 584, 218], packed [225, 584]
- (content) When the attribute name passed to __findattr__ is 'aq_base', what value does the method return? — gold PEP [231], retrieved [231, 363, 395, 258], packed []
- (content) Why does the upload client need to use the same multipart/form-data page format as an old Netscape Navigator when uploading a file? — gold PEP [243], retrieved [691, 470, 700, 243, 497], packed [691, 470]
- (content) What does calling adapt on an instance of C with protocol B return? — gold PEP [246], retrieved [246, 307], packed [246, 246]
- (content) Why does the design propose keeping the styling component small and modular? — gold PEP [258], retrieved [258, 8, 501, 292, 572], packed []
- (content) What modification was introduced in version 1.8 concerning the coding regular expression? — gold PEP [263], retrieved [263, 641, 8, 614, 637], packed [263]
- (content) Why was the originally proposed iterzip() function not added as a built-in? — gold PEP [279], retrieved [279, 455, 218, 575], packed [279]
- (content) In the proposed integer for-loop syntax, what restriction is placed on the two comparison operators used in the loop header? — gold PEP [284], retrieved [284, 207, 572, 225], packed [284, 284, 207]

### Examples of questions that should have been refused but were answered

- (permission, asked by bob) Why does the proposal say the problem cannot wait until the next Python release? — cited [608]
- (permission, asked by bob) Why does this proposal claim its string interpolation feature is simpler than the earlier alternative that introduced a new '$' prefix? — cited [750]
- (permission, asked by bob) How should a generator behave when it is closed using the exception‑semantics approach? — cited [342]
- (permission, asked by bob) Why must you verify a contributor's PSF CLA before merging their bug tracker change? — cited [512]
- (permission, asked by alice) How does the proposal describe a cryptographically strong pseudo random number generator? — cited [524]
- (permission, asked by alice) Why does the proposal aim to cause minimal disruption to existing projects on PyPI? — cited [470]
- (permission, asked by bob) What is the name pattern of the file that, if present, is used to initialize sys.path for the interpreter? — cited [405, 405]


## How to read this (written after the run, for this run only)

What the numbers support:

- **Hybrid search beats keyword-only search.** nDCG@10 is higher by 0.13 (95% interval +0.09 to +0.18) and Hit@5 by 0.07. This corpus is large enough (2,007 chunks) to show it, unlike the 33-chunk demo corpus where every variant scored about 1.0.
- **Dense-only Titan is already strong, and hybrid is not clearly better than it.** Hybrid's Hit@5 is slightly higher (+0.014) but its nDCG@10 is slightly lower (-0.023, interval -0.046 to -0.002): the keyword leg adds noise at the top of the list on these questions.
- **The LLM reranker did not help significantly.** +0.015 nDCG@10 with Titan (interval -0.013 to +0.044) and +0.001 with Cohere. It adds about 1.6 s at the median, so on this evidence it is not worth its latency and cost for ranking. (It also sets the score floor that decides when to refuse, which this ablation does not separate.)
- **Cohere Embed v3 did not beat Titan v2.** Titan is slightly ahead on every hybrid comparison, though no interval excludes zero. Cohere also truncates a chunk to its first 2,048 characters.
- **No permission leak** in any variant, and none in the answers.
- **Answers cite the gold source almost always (91 of 96 answered)** and refuse the unanswerable questions (50 of 57; all 24 off-corpus ones).

What they show is weak:

- **Too many wrongful refusals.** Only 96 of 143 answerable questions were answered (67%); for the drafted content questions the right decision was made on 55 of 94. A question about a PEP that was retrieved and packed was still refused. The likely causes are the rerank score floor of 2.0 and the claim verifier dropping every claim; neither was tuned here.
- **Only 70% of judged answers had every claim supported by the cited quotes** (the AI judge's reading), and 81% were judged correct, 12 partial.
- **Latency columns include queueing** (4 searches at once, throttled Bedrock retries) and are not a service level.

Limits of the evidence: questions are AI-drafted (content questions were checked for a verbatim quote; header and unanswerable questions are made by code); one run; one gold PEP per question although some PEPs overlap in topic, which understates recall; the judge is an AI model whose labels a person has not yet checked (`judge_review_sheet.csv` is ready for that, `scripts/realbench_review.py` compares).

Next steps this points to: try a lower rerank floor and a looser verifier on the dev split only, then report the test split; try dense-only plus rerank as the default.
