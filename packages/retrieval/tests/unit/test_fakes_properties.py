"""Property test for the fakes (task 8.14).

Feature: cairn-free-tier-retarget
Property 22: The fakes are deterministic and well-formed

**Validates: Requirements 16.3, 16.4, 16.5**

For any texts and any configured dimension, the fake embedder returns the same vector on every
call, of the configured length and unit L2 norm, and ``embed_query`` equals ``embed_documents``
for the same text (the fake has no query prefix). For any query and passage list, the fake
reranker returns one finite, non-negative score per passage, and each score is the same wherever
the passage sits in the list and whatever else is in the list. For any request, the fake LLM
returns the same text and token counts on every call, the text is ``fake-answer:`` plus 16 hex
digits, the counts follow ``estimate_tokens``, and ``evidence_ids``, ``temperature`` and
``max_tokens`` do not change the answer.

Texts mix arbitrary Unicode (surrogates excluded, since the fakes encode to UTF-8) with words
drawn from a small vocabulary, so the reranker sees shared tokens often. The fakes are async, so
each case runs them with ``asyncio.run``; no socket, clock or file is involved.
"""

from __future__ import annotations

import asyncio
import math
import re

from hypothesis import given
from hypothesis import strategies as st

from cairn_retrieval.cloudflare.budget import estimate_tokens
from cairn_retrieval.llm.fake import FAKE_LLM_MODEL, FAKE_LLM_PROVIDER, FakeLlm
from cairn_retrieval.llm.types import LlmRequest
from cairn_retrieval.models.fake import (
    FAKE_EMBED_KEY,
    FAKE_RERANK_KEY,
    FakeEmbedder,
    FakeReranker,
)

VOCABULARY = ["alpha", "beta", "gamma", "delta", "the", "of", "Alpha", "2026", "x_1"]

free_text = st.text(alphabet=st.characters(blacklist_categories=["Cs"]), max_size=60)
word_text = st.lists(st.sampled_from(VOCABULARY), max_size=12).map(" ".join)
texts = st.one_of(free_text, word_text, st.just(""), st.just(" ,.;!"))
dims = st.one_of(st.integers(min_value=1, max_value=64), st.just(384))

ANSWER = re.compile(r"fake-answer:[0-9a-f]{16}")


@given(dim=dims, batch=st.lists(texts, max_size=6))
def test_fake_embedder_is_deterministic_and_unit_length(dim: int, batch: list[str]) -> None:
    embedder = FakeEmbedder(dim=dim)
    first = asyncio.run(embedder.embed_documents(batch))
    second = asyncio.run(FakeEmbedder(dim=dim).embed_documents(batch))

    assert first == second
    assert len(first) == len(batch)
    assert embedder.model_key == FAKE_EMBED_KEY
    assert embedder.dim == dim
    for text, vector in zip(batch, first, strict=True):
        assert len(vector) == dim
        assert all(math.isfinite(x) for x in vector)
        assert math.isclose(math.fsum(x * x for x in vector), 1.0, abs_tol=1e-9)
        assert asyncio.run(embedder.embed_query(text)) == vector
        assert asyncio.run(embedder.embed_documents([text])) == [vector]


@given(dim=dims, text=st.sampled_from(["", " ", "...", "!?"]))
def test_fake_embedder_maps_wordless_text_to_e0(dim: int, text: str) -> None:
    vector = asyncio.run(FakeEmbedder(dim=dim).embed_query(text))
    assert vector == [1.0] + [0.0] * (dim - 1)


@given(
    query=texts,
    passages=st.lists(texts, max_size=8),
    data=st.data(),
)
def test_fake_reranker_scores_ignore_position_and_neighbours(
    query: str, passages: list[str], data: st.DataObject
) -> None:
    reranker = FakeReranker()
    scores = asyncio.run(reranker.score(query, passages))

    assert reranker.model_key == FAKE_RERANK_KEY
    assert len(scores) == len(passages)
    assert all(math.isfinite(s) and s >= 0.0 for s in scores)
    assert asyncio.run(reranker.score(query, passages)) == scores

    order = data.draw(st.permutations(range(len(passages))))
    shuffled = asyncio.run(reranker.score(query, [passages[i] for i in order]))
    for position, original in enumerate(order):
        assert shuffled[position] == scores[original]
    for passage, score in zip(passages, scores, strict=True):
        assert asyncio.run(reranker.score(query, [passage])) == [score]


requests = st.builds(
    LlmRequest,
    system=texts,
    user=texts,
    max_tokens=st.integers(min_value=1, max_value=10_000),
    temperature=st.floats(min_value=0.0, max_value=2.0, allow_nan=False),
    evidence_ids=st.lists(st.text(max_size=8), max_size=4).map(tuple),
)


@given(req=requests, other=requests)
def test_fake_llm_is_deterministic_and_well_formed(req: LlmRequest, other: LlmRequest) -> None:
    llm = FakeLlm()
    first = asyncio.run(llm.complete(req))
    second = asyncio.run(FakeLlm().complete(req))

    assert (first.text, first.tokens_in, first.tokens_out) == (
        second.text,
        second.tokens_in,
        second.tokens_out,
    )
    assert ANSWER.fullmatch(first.text)
    assert first.tokens_in == estimate_tokens(req.system) + estimate_tokens(req.user)
    assert first.tokens_out == estimate_tokens(first.text)
    assert first.provider == FAKE_LLM_PROVIDER == llm.provider
    assert first.model == FAKE_LLM_MODEL == llm.model
    assert first.latency_ms == 0.0

    # Only system + user decide the text; the other request fields do not.
    variant = LlmRequest(
        system=req.system,
        user=req.user,
        max_tokens=other.max_tokens,
        temperature=other.temperature,
        evidence_ids=other.evidence_ids,
    )
    assert asyncio.run(llm.complete(variant)).text == first.text
