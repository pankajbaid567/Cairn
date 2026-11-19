"""Provider protocols, Workers AI adapters, fakes and builders (design §13).

Requirements 16.1 to 16.5. The adapters run through ``httpx.MockTransport`` over the contract
fixtures in ``data/workers_ai/``; nothing opens a socket and nothing calls Cloudflare.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from datetime import date
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from cairn_core.config import LlmSettings, ModelSettings, WorkersAiSettings, env_keys
from cairn_retrieval import llm as llm_package
from cairn_retrieval.cloudflare.budget import (
    InMemoryUsageStore,
    NeuronBudget,
    estimate_input_tokens,
    estimate_neurons,
    estimate_tokens,
)
from cairn_retrieval.cloudflare.client import WorkersAiClient, WorkersAiError
from cairn_retrieval.llm.extractive import FakeProvider
from cairn_retrieval.llm.factory import build_llm
from cairn_retrieval.llm.fake import FakeLlm
from cairn_retrieval.llm.family import model_family
from cairn_retrieval.llm.types import LlmProvider, LlmRequest
from cairn_retrieval.llm.workers_ai import WorkersAiLlm
from cairn_retrieval.models import (
    EMBED_BATCH_SIZE,
    Embedder,
    FakeEmbedder,
    FakeReranker,
    Reranker,
    WorkersAiEmbedder,
    WorkersAiReranker,
    build_embedder,
    build_reranker,
)

DATA = Path(__file__).parent / "data" / "workers_ai"
ACCOUNT = "0123456789abcdef0123456789abcdef"
TOKEN = "cf-test-token-SECRET-0002"  # fake by construction
DAY = date(2026, 11, 3)
EMBED = "@cf/baai/bge-small-en-v1.5"
RERANK = "@cf/baai/bge-reranker-base"
GEMMA = "@cf/google/gemma-4-26b-a4b-it"
GLM = "@cf/zai-org/glm-4.7-flash"
PREFIX = "Represent this sentence for searching relevant passages: "


@pytest.fixture(autouse=True)
def _clean_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for cls in (WorkersAiSettings, ModelSettings, LlmSettings):
        for key in env_keys(cls):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.chdir(tmp_path)  # no .env here


def fixture_response(name: str) -> httpx.Response:
    return httpx.Response(
        200,
        content=(DATA / f"{name}.json").read_bytes(),
        headers={"content-type": "application/json"},
    )


def envelope(result: dict[str, Any]) -> httpx.Response:
    return httpx.Response(
        200, json={"result": result, "success": True, "errors": [], "messages": []}
    )


class Recorder:
    """A transport that answers every request with ``respond`` and keeps the requests."""

    def __init__(self, respond: Callable[[httpx.Request], httpx.Response]) -> None:
        self.respond = respond
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.respond(request)

    def bodies(self) -> list[dict[str, Any]]:
        return [json.loads(r.content) for r in self.requests]


def echo_length_embeddings(request: httpx.Request) -> httpx.Response:
    """One vector per text: ``[len(text), 0, 0, 0]``, so the order of results can be checked."""
    texts = json.loads(request.content)["text"]
    data = [[float(len(t)), 0.0, 0.0, 0.0] for t in texts]
    return envelope({"shape": [len(texts), 4], "data": data})


Factory = Callable[[Callable[[httpx.Request], httpx.Response]], tuple[WorkersAiClient, Recorder]]


@pytest.fixture
async def make_client() -> AsyncIterator[tuple[Factory, NeuronBudget]]:
    budget = NeuronBudget(InMemoryUsageStore(), 100_000, today=lambda: DAY)
    clients: list[WorkersAiClient] = []

    def factory(
        respond: Callable[[httpx.Request], httpx.Response],
    ) -> tuple[WorkersAiClient, Recorder]:
        recorder = Recorder(respond)
        cfg = WorkersAiSettings(
            _env_file=None, CLOUDFLARE_ACCOUNT_ID=ACCOUNT, CLOUDFLARE_API_TOKEN=SecretStr(TOKEN)
        )
        client = WorkersAiClient(cfg, budget, transport=httpx.MockTransport(recorder))
        clients.append(client)
        return client, recorder

    yield factory, budget
    for client in clients:
        await client.aclose()


# --- Workers AI embedder ----------------------------------------------------------------------


async def test_embed_documents_batches_at_50_and_keeps_input_order(
    make_client: tuple[Factory, NeuronBudget],
) -> None:
    factory, _ = make_client
    client, recorder = factory(echo_length_embeddings)
    embedder = WorkersAiEmbedder(client, EMBED, 4, PREFIX)
    texts = ["x" * (i + 1) for i in range(120)]
    vectors = await embedder.embed_documents(texts)
    assert EMBED_BATCH_SIZE == 50
    assert [len(b["text"]) for b in recorder.bodies()] == [50, 50, 20]
    assert [v[0] for v in vectors] == [float(i + 1) for i in range(120)]
    assert all(r.url.path.endswith(f"/ai/run/{EMBED}") for r in recorder.requests)


@pytest.mark.parametrize(("count", "calls"), [(0, 0), (1, 1), (50, 1), (51, 2), (100, 2)])
async def test_embed_documents_call_count(
    make_client: tuple[Factory, NeuronBudget], count: int, calls: int
) -> None:
    factory, budget = make_client
    client, recorder = factory(echo_length_embeddings)
    out = await WorkersAiEmbedder(client, EMBED, 4).embed_documents(["t"] * count)
    assert len(out) == count
    assert len(recorder.requests) == calls
    assert (await budget.used() > 0) == (calls > 0)


async def test_documents_carry_no_prefix_and_the_query_carries_it(
    make_client: tuple[Factory, NeuronBudget],
) -> None:
    factory, _ = make_client
    client, recorder = factory(echo_length_embeddings)
    embedder = WorkersAiEmbedder(client, EMBED, 4, PREFIX)
    await embedder.embed_documents(["gateway"])
    vector = await embedder.embed_query("gateway")
    assert recorder.bodies()[0] == {"text": ["gateway"]}
    assert recorder.bodies()[1] == {"text": [PREFIX + "gateway"]}
    assert vector == [float(len(PREFIX + "gateway")), 0.0, 0.0, 0.0]
    assert embedder.model_key == f"workers-ai:{EMBED}"
    assert embedder.dim == 4


async def test_embedder_reserves_the_estimate_for_the_texts_it_sends(
    make_client: tuple[Factory, NeuronBudget],
) -> None:
    factory, budget = make_client
    client, _ = factory(echo_length_embeddings)
    texts = ["a" * 900, "b" * 600]
    await WorkersAiEmbedder(client, EMBED, 4).embed_documents(texts)
    assert await budget.used() == estimate_neurons(EMBED, estimate_input_tokens(texts))


async def test_empty_text_is_refused_before_any_call(
    make_client: tuple[Factory, NeuronBudget],
) -> None:
    factory, budget = make_client
    client, recorder = factory(echo_length_embeddings)
    embedder = WorkersAiEmbedder(client, EMBED, 4)
    with pytest.raises(ValueError, match="empty"):
        await embedder.embed_documents(["fine", ""])
    with pytest.raises(ValueError, match="empty"):
        await embedder.embed_query("")
    assert recorder.requests == []
    assert await budget.used() == 0


async def test_an_embedding_result_of_the_wrong_shape_is_a_bad_response(
    make_client: tuple[Factory, NeuronBudget],
) -> None:
    factory, _ = make_client
    client, _ = factory(lambda _r: fixture_response("malformed_embeddings"))
    with pytest.raises(WorkersAiError) as exc:
        await WorkersAiEmbedder(client, EMBED, 4).embed_documents(["a", "b"])
    assert exc.value.code == "bad_response"
    assert str(exc.value) == f"bad_response model={EMBED} status=200 attempts=1"


async def test_an_embedding_dimension_other_than_configured_is_a_bad_response(
    make_client: tuple[Factory, NeuronBudget],
) -> None:
    factory, _ = make_client
    client, _ = factory(lambda _r: fixture_response("embeddings"))  # 2 vectors of 4
    with pytest.raises(WorkersAiError) as exc:
        await WorkersAiEmbedder(client, EMBED, 384).embed_documents(["a", "b"])
    assert exc.value.code == "bad_response"


async def test_the_fixture_embeddings_come_back_as_floats(
    make_client: tuple[Factory, NeuronBudget],
) -> None:
    factory, _ = make_client
    client, _ = factory(lambda _r: fixture_response("embeddings"))
    out = await WorkersAiEmbedder(client, EMBED, 4).embed_documents(["a", "b"])
    assert out == [[0.25, -0.5, 0.125, 0.0625], [0.5, 0.5, -0.25, 1.0]]


# --- Workers AI reranker ----------------------------------------------------------------------


async def test_rerank_sends_one_call_and_returns_scores_in_input_order(
    make_client: tuple[Factory, NeuronBudget],
) -> None:
    factory, budget = make_client
    client, recorder = factory(lambda _r: fixture_response("rerank"))  # ids listed 2, 0, 1
    reranker = WorkersAiReranker(client, RERANK)
    passages = ["alpha passage", "beta passage", "gamma passage"]
    scores = await reranker.score("which gateway", passages)
    assert scores == [-1.25, 0.75, 4.5]
    (body,) = recorder.bodies()
    assert body == {
        "query": "which gateway",
        "contexts": [{"text": p} for p in passages],
    }
    per_pair = sum(estimate_tokens("which gateway") + estimate_tokens(p) for p in passages)
    assert await budget.used() == estimate_neurons(RERANK, per_pair)
    assert reranker.model_key == f"workers-ai:{RERANK}"


async def test_rerank_with_no_passages_makes_no_call(
    make_client: tuple[Factory, NeuronBudget],
) -> None:
    factory, budget = make_client
    client, recorder = factory(lambda _r: fixture_response("rerank"))
    assert await WorkersAiReranker(client, RERANK).score("q", []) == []
    assert recorder.requests == []
    assert await budget.used() == 0


async def test_rerank_refuses_an_empty_query_or_passage(
    make_client: tuple[Factory, NeuronBudget],
) -> None:
    factory, _ = make_client
    client, recorder = factory(lambda _r: fixture_response("rerank"))
    reranker = WorkersAiReranker(client, RERANK)
    with pytest.raises(ValueError, match="empty"):
        await reranker.score("", ["a"])
    with pytest.raises(ValueError, match="empty"):
        await reranker.score("q", ["a", ""])
    assert recorder.requests == []


async def test_rerank_result_that_misses_a_passage_is_a_bad_response(
    make_client: tuple[Factory, NeuronBudget],
) -> None:
    factory, _ = make_client
    client, _ = factory(lambda _r: fixture_response("rerank"))  # scores 3 passages
    with pytest.raises(WorkersAiError) as exc:
        await WorkersAiReranker(client, RERANK).score("q", ["a", "b"])
    assert exc.value.code == "bad_response"


# --- Workers AI LLM ---------------------------------------------------------------------------

REQUEST = LlmRequest(
    system="Answer from the evidence.", user="When does it restart?", max_tokens=512
)


async def test_llm_request_body_and_token_counts(
    make_client: tuple[Factory, NeuronBudget],
) -> None:
    factory, budget = make_client
    client, recorder = factory(lambda _r: fixture_response("text_openai"))
    llm = WorkersAiLlm(client, GEMMA)
    out = await llm.complete(REQUEST)
    (body,) = recorder.bodies()
    assert body == {
        "messages": [
            {"role": "system", "content": "Answer from the evidence."},
            {"role": "user", "content": "When does it restart?"},
        ],
        "max_completion_tokens": 512,
        "temperature": 0.0,
    }
    assert out.text == "The Kestrel gateway restarts nightly."
    assert (out.tokens_in, out.tokens_out) == (120, 48)  # reasoning tokens are inside the 48
    assert (out.provider, out.model, out.finish_reason) == ("workers_ai", GEMMA, "stop")
    assert out.latency_ms >= 0.0
    tokens_in = estimate_tokens(REQUEST.system) + estimate_tokens(REQUEST.user)
    assert await budget.used() == estimate_neurons(GEMMA, tokens_in, 512)
    assert (llm.provider, llm.model) == ("workers_ai", GEMMA)


async def test_llm_call_uses_the_longer_read_timeout(
    make_client: tuple[Factory, NeuronBudget],
) -> None:
    factory, _ = make_client
    client, recorder = factory(lambda _r: fixture_response("text_openai"))
    await WorkersAiLlm(client, GEMMA).complete(REQUEST)
    assert recorder.requests[0].extensions["timeout"]["read"] == 60.0


async def test_llm_accepts_the_legacy_response_shape(
    make_client: tuple[Factory, NeuronBudget],
) -> None:
    factory, _ = make_client
    client, _ = factory(lambda _r: fixture_response("text_response_legacy"))
    out = await WorkersAiLlm(client, GEMMA).complete(REQUEST)
    assert (out.text, out.tokens_in, out.tokens_out, out.finish_reason) == (
        "The Kestrel gateway restarts nightly.",
        64,
        12,
        None,
    )


async def test_llm_reasoning_that_uses_the_whole_bound_is_a_failure(
    make_client: tuple[Factory, NeuronBudget],
) -> None:
    """FV9: null content with finish_reason ``length`` is a failure, not an empty answer."""
    factory, _ = make_client
    client, _ = factory(lambda _r: fixture_response("text_openai_length"))
    with pytest.raises(WorkersAiError) as exc:
        await WorkersAiLlm(client, GLM).complete(REQUEST)
    assert exc.value.code == "bad_response"
    assert exc.value.attempts == 1


@pytest.mark.parametrize("blank", ["", "  \n"])
async def test_llm_empty_text_with_finish_reason_length_is_a_failure(
    make_client: tuple[Factory, NeuronBudget], blank: str
) -> None:
    factory, _ = make_client
    choice = {"message": {"role": "assistant", "content": blank}, "finish_reason": "length"}
    usage = {"prompt_tokens": 10, "completion_tokens": 512}
    client, _ = factory(lambda _r: envelope({"choices": [choice], "usage": usage}))
    with pytest.raises(WorkersAiError) as exc:
        await WorkersAiLlm(client, GLM).complete(REQUEST)
    assert exc.value.code == "bad_response"


async def test_llm_truncated_but_non_empty_text_is_returned_with_its_finish_reason(
    make_client: tuple[Factory, NeuronBudget],
) -> None:
    factory, _ = make_client
    choice = {
        "message": {"role": "assistant", "content": "The gateway re"},
        "finish_reason": "length",
    }
    usage = {"prompt_tokens": 10, "completion_tokens": 512}
    client, _ = factory(lambda _r: envelope({"choices": [choice], "usage": usage}))
    out = await WorkersAiLlm(client, GEMMA).complete(REQUEST)
    assert (out.text, out.finish_reason) == ("The gateway re", "length")


async def test_llm_model_without_a_price_makes_no_call(
    make_client: tuple[Factory, NeuronBudget],
) -> None:
    from cairn_retrieval.cloudflare.budget import UnpricedModel

    factory, _ = make_client
    client, recorder = factory(lambda _r: fixture_response("text_openai"))
    with pytest.raises(UnpricedModel):
        await WorkersAiLlm(client, "@cf/acme/unpriced-1").complete(REQUEST)
    assert recorder.requests == []


def test_llm_request_validates_its_bounds() -> None:
    with pytest.raises(ValueError, match="max_tokens"):
        LlmRequest(system="s", user="u", max_tokens=0)
    with pytest.raises(ValueError, match="temperature"):
        LlmRequest(system="s", user="u", max_tokens=1, temperature=3.0)


# --- fakes --------------------------------------------------------------------------------------


async def test_fake_embedder_is_deterministic_unit_length_and_of_the_configured_dim() -> None:
    embedder = FakeEmbedder(32)
    texts = ["The Kestrel gateway restarts nightly.", "gateway", "!!!", ""]
    first = await embedder.embed_documents(texts)
    second = await embedder.embed_documents(texts)
    assert first == second
    assert all(len(v) == 32 for v in first)
    assert all(abs(sum(x * x for x in v) - 1.0) < 1e-9 for v in first)
    assert first[2] == first[3] == [1.0] + [0.0] * 31  # no word token: e0
    assert await embedder.embed_query("gateway") == first[1]
    assert embedder.model_key == "fake-embed-v1"


async def test_fake_embedder_separates_unrelated_texts_and_ignores_case_and_punctuation() -> None:
    embedder = FakeEmbedder(384)
    a, b, c = await embedder.embed_documents(["Gateway restarts.", "gateway RESTARTS", "payroll"])
    assert a == b
    assert sum(x * y for x, y in zip(a, c, strict=True)) < 0.5


async def test_fake_reranker_scores_each_passage_on_its_own() -> None:
    reranker = FakeReranker()
    passages = ["the gateway restarts nightly", "payroll runs monthly", "gateway"]
    scores = await reranker.score("when does the gateway restart", passages)
    assert len(scores) == 3
    assert scores[1] == 0.0
    assert scores[0] == 1 / (1 + 4)  # shares only "gateway" ("restart" differs from "restarts")
    assert scores[2] == 1 / (1 + 1)
    reversed_scores = await reranker.score("when does the gateway restart", passages[::-1])
    assert reversed_scores == scores[::-1]
    assert await reranker.score("anything", []) == []
    assert reranker.model_key == "fake-rerank-v1"


async def test_fake_llm_is_deterministic_and_offline() -> None:
    fake = FakeLlm()
    first = await fake.complete(REQUEST)
    second = await fake.complete(REQUEST.model_copy(update={"evidence_ids": ("c1",)}))
    assert first.text == second.text
    assert first.text.startswith("fake-answer:")
    assert len(first.text) == len("fake-answer:") + 16
    assert (first.provider, first.model) == ("fake", "fake-llm-v1")
    assert first.tokens_in == estimate_tokens(REQUEST.system) + estimate_tokens(REQUEST.user)
    assert first.tokens_out == estimate_tokens(first.text)
    other = await fake.complete(REQUEST.model_copy(update={"user": "something else"}))
    assert other.text != first.text


def test_fakes_and_adapters_satisfy_the_protocols() -> None:
    assert isinstance(FakeEmbedder(8), Embedder)
    assert isinstance(FakeReranker(), Reranker)
    assert isinstance(FakeLlm(), LlmProvider)


# --- builders -----------------------------------------------------------------------------------


def model_settings(**values: str) -> ModelSettings:
    return ModelSettings(_env_file=None, **values)  # type: ignore[arg-type]


def llm_settings(**values: str) -> LlmSettings:
    return LlmSettings(_env_file=None, **values)  # type: ignore[arg-type]


async def test_build_embedder_and_reranker_from_fake_settings() -> None:
    models = model_settings(CAIRN_EMBEDDER="fake", CAIRN_RERANKER="fake", CAIRN_EMBED_DIM="16")
    embedder = build_embedder(models, None)
    assert isinstance(embedder, FakeEmbedder)
    assert embedder.dim == 16
    assert isinstance(build_reranker(models, None), FakeReranker)


def test_build_reranker_none_gives_no_reranker() -> None:
    assert (
        build_reranker(model_settings(CAIRN_EMBEDDER="fake", CAIRN_RERANKER="none"), None) is None
    )


async def test_build_workers_ai_adapters_use_the_configured_models(
    make_client: tuple[Factory, NeuronBudget],
) -> None:
    factory, _ = make_client
    client, _ = factory(lambda _r: fixture_response("embeddings"))
    models = model_settings(CLOUDFLARE_API_TOKEN=TOKEN)  # defaults select workers_ai
    embedder = build_embedder(models, client)
    reranker = build_reranker(models, client)
    assert isinstance(embedder, WorkersAiEmbedder)
    assert embedder.model_key == f"workers-ai:{EMBED}"
    assert embedder.dim == 384
    assert isinstance(reranker, WorkersAiReranker)
    assert reranker.model_key == f"workers-ai:{RERANK}"


def test_workers_ai_builders_need_a_client() -> None:
    models = model_settings(CLOUDFLARE_API_TOKEN=TOKEN)
    with pytest.raises(ValueError, match="CAIRN_EMBEDDER"):
        build_embedder(models, None)
    with pytest.raises(ValueError, match="CAIRN_RERANKER"):
        build_reranker(models, None)


async def test_build_llm_picks_the_provider_and_model_of_the_role(
    make_client: tuple[Factory, NeuronBudget],
) -> None:
    factory, _ = make_client
    client, _ = factory(lambda _r: fixture_response("text_openai"))
    settings = llm_settings(CLOUDFLARE_API_TOKEN=TOKEN)  # gemma answers, glm judges
    answer = build_llm(settings, "answer", client)
    judge = build_llm(settings, "judge", client)
    assert isinstance(answer, WorkersAiLlm)
    assert isinstance(judge, WorkersAiLlm)
    assert (answer.model, judge.model) == (GEMMA, GLM)
    fake = llm_settings(CAIRN_ANSWER_PROVIDER="fake", CAIRN_JUDGE_PROVIDER="fake")
    assert isinstance(build_llm(fake, "answer", None), FakeProvider)  # extractive (Phase 1)
    assert isinstance(build_llm(fake, "judge", None), FakeLlm)


def test_build_llm_workers_ai_needs_a_client() -> None:
    settings = llm_settings(CLOUDFLARE_API_TOKEN=TOKEN)
    with pytest.raises(ValueError, match="CAIRN_ANSWER_PROVIDER"):
        build_llm(settings, "answer", None)
    with pytest.raises(ValueError, match="CAIRN_JUDGE_PROVIDER"):
        build_llm(settings, "judge", None)


@pytest.mark.parametrize(
    ("provider", "key", "model"),
    [
        ("anthropic", "ANTHROPIC_API_KEY", "claude-test-1"),
        ("openai", "OPENAI_API_KEY", "gpt-test-1"),
    ],
)
def test_build_llm_builds_the_optional_anthropic_and_openai_adapters(
    provider: str, key: str, model: str
) -> None:
    settings = llm_settings(
        CAIRN_ANSWER_PROVIDER=provider,
        CAIRN_ANSWER_MODEL=model,
        CAIRN_JUDGE_PROVIDER="fake",
        **{key: "provider-test-key"},
    )
    built = build_llm(settings, "answer", None)
    assert (built.provider, built.model) == (provider, model)


def test_the_family_rule_is_re_exported_from_the_llm_package() -> None:
    assert model_family("workers_ai", GEMMA) == "google"
    assert model_family("workers_ai", GLM) == "zhipu"
    assert llm_package.LlmRequest is LlmRequest
