"""Live Workers AI check: one tiny embed, rerank and text call (design §13, FU8).

These tests call the real Cloudflare API with the user's token. They run only through
``make workers-ai-live-check``, which sets ``CAIRN_LIVE_CHECK=1`` and is user-run: it is in no
gate and not in CI (Requirement 16.11). Without both ``CAIRN_LIVE_CHECK=1`` and a
``CLOUDFLARE_API_TOKEN`` (environment or ``.env``) every test skips with a message that says how
to enable them (Requirement 16.10). The whole run reserves under 100 neurons of the 10,000 a day.

Nothing here prints the token, the account id, the URL or any response body: assertions look at
shapes and counts only.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

import pytest

from cairn_core.config import WorkersAiSettings
from cairn_retrieval.cloudflare.budget import InMemoryUsageStore, NeuronBudget
from cairn_retrieval.cloudflare.client import WorkersAiClient
from cairn_retrieval.llm import LlmRequest, WorkersAiLlm
from cairn_retrieval.models import WorkersAiEmbedder, WorkersAiReranker

pytestmark = pytest.mark.live

HOW_TO_ENABLE = (
    "live Workers AI checks are off. To run them, put CLOUDFLARE_ACCOUNT_ID and "
    "CLOUDFLARE_API_TOKEN in .env, then run `make workers-ai-live-check` "
    "(it sets CAIRN_LIVE_CHECK=1; the check makes three small real calls)"
)

EMBED_MODEL = "@cf/baai/bge-small-en-v1.5"
RERANK_MODEL = "@cf/baai/bge-reranker-base"
ANSWER_MODEL = "@cf/google/gemma-4-26b-a4b-it"
QUERY_PREFIX = "Represent this sentence for searching relevant passages: "


@pytest.fixture
async def client() -> AsyncIterator[WorkersAiClient]:
    if os.environ.get("CAIRN_LIVE_CHECK") != "1":
        pytest.skip(HOW_TO_ENABLE)
    settings = WorkersAiSettings()  # the environment first, then .env in the working directory
    if settings.api_token is None or settings.account_id is None:
        pytest.skip(HOW_TO_ENABLE)
    budget = NeuronBudget(InMemoryUsageStore(), settings.daily_neurons)
    async with WorkersAiClient(settings, budget) as live:
        yield live


async def test_live_embed_returns_one_vector_of_the_configured_dimension(
    client: WorkersAiClient,
) -> None:
    embedder = WorkersAiEmbedder(client, EMBED_MODEL, 384, QUERY_PREFIX)
    (document,) = await embedder.embed_documents(["The Kestrel gateway restarts nightly."])
    query = await embedder.embed_query("when does the gateway restart")
    assert len(document) == len(query) == 384
    assert any(x != 0.0 for x in document)


async def test_live_rerank_scores_every_passage(client: WorkersAiClient) -> None:
    reranker = WorkersAiReranker(client, RERANK_MODEL)
    passages = ["The gateway restarts nightly at 02:00.", "Payroll runs on the last day."]
    scores = await reranker.score("when does the gateway restart", passages)
    assert len(scores) == 2
    assert scores[0] > scores[1]


async def test_live_text_call_returns_an_answer_and_token_counts(client: WorkersAiClient) -> None:
    # The bound covers the reasoning tokens and the answer (FV9); too small a bound would give an
    # empty answer, which the adapter reports as bad_response.
    llm = WorkersAiLlm(client, ANSWER_MODEL)
    out = await llm.complete(
        LlmRequest(system="Reply with one short word.", user="Say ok.", max_tokens=1024)
    )
    assert out.text.strip()
    assert out.tokens_in > 0
    assert out.tokens_out > 0
    assert (out.provider, out.model) == ("workers_ai", ANSWER_MODEL)
