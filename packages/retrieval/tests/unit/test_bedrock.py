"""Bedrock adapters, client and settings. A fake runtime stands in for boto3: nothing calls AWS."""

from __future__ import annotations

import asyncio
import io
import json
from typing import Any

import pytest

from cairn_core.config import BedrockSettings, LlmSettings, ModelSettings, load_settings
from cairn_retrieval.bedrock import BedrockClient, BedrockEmbedder, BedrockLlm, BedrockLlmReranker
from cairn_retrieval.bedrock.adapters import parse_scores
from cairn_retrieval.bedrock.pricing import (
    BEDROCK_RATES,
    UnpricedBedrockModel,
    estimate_micro_usd,
    usd,
)
from cairn_retrieval.cloudflare.budget import InMemoryUsageStore, NeuronBudget
from cairn_retrieval.cloudflare.client import WorkersAiError
from cairn_retrieval.llm.cost import call_cost
from cairn_retrieval.llm.factory import build_llm
from cairn_retrieval.llm.types import LlmRequest, LlmResponse
from cairn_retrieval.models.factory import build_embedder, build_reranker

GPT = "openai.gpt-oss-120b-1:0"
QWEN = "qwen.qwen3-32b-v1:0"
TITAN = "amazon.titan-embed-text-v2:0"


class ClientError(Exception):
    """Shaped like botocore.exceptions.ClientError (the code lives in ``response``)."""

    def __init__(self, code: str, status: int = 400) -> None:
        super().__init__(code)
        self.response = {"Error": {"Code": code}, "ResponseMetadata": {"HTTPStatusCode": status}}


class ReadTimeoutError(Exception):
    pass


def converse_ok(text: str, i: int = 11, o: int = 7, stop: str = "end_turn") -> dict[str, Any]:
    return {
        "output": {"message": {"content": [{"reasoningContent": {}}, {"text": text}]}},
        "usage": {"inputTokens": i, "outputTokens": o},
        "stopReason": stop,
    }


class FakeRuntime:
    def __init__(self, *outcomes: Any) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def _next(self, kind: str, kw: dict[str, Any]) -> Any:
        self.calls.append((kind, kw))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def converse(self, **kw: Any) -> Any:
        return self._next("converse", kw)

    def invoke_model(self, **kw: Any) -> Any:
        return self._next("invoke", kw)


def embed_ok(dim: int, value: float = 0.5) -> dict[str, Any]:
    body = json.dumps({"embedding": [value] * dim, "inputTextTokenCount": 3}).encode()
    return {"body": io.BytesIO(body)}


async def no_sleep(_: float) -> None:
    return None


def settings(**env: str) -> BedrockSettings:
    return BedrockSettings.model_validate(env)


def client(runtime: FakeRuntime, budget: NeuronBudget | None = None, **env: str) -> BedrockClient:
    return BedrockClient(
        settings(**env), budget, runtime=runtime, sleep=no_sleep, jitter=lambda: 0.0
    )


def run(coro: Any) -> Any:
    return asyncio.run(coro)


# --- pricing -----------------------------------------------------------------------------


def test_estimate_is_tokens_times_rate_in_micro_dollars() -> None:
    # $0.18 per million input tokens is 0.18 micro-dollars per token
    assert estimate_micro_usd(GPT, 1000, 0) == 180
    assert estimate_micro_usd(GPT, 1000, 1000) == 180 + 710
    assert estimate_micro_usd(TITAN, 1) == 1  # never below 1


def test_unpriced_model_is_refused_and_negative_counts_rejected() -> None:
    with pytest.raises(UnpricedBedrockModel):
        estimate_micro_usd("some.other-model", 10)
    with pytest.raises(ValueError, match="must not be negative"):
        estimate_micro_usd(GPT, -1)
    assert usd("some.other-model", 1, 1) is None
    assert usd(GPT, 1_000_000, 1_000_000) == pytest.approx(0.18 + 0.71)


def test_the_models_the_project_uses_are_priced() -> None:
    assert {GPT, QWEN, TITAN, "openai.gpt-oss-20b-1:0"} <= set(BEDROCK_RATES)


# --- client: converse ---------------------------------------------------------------------


def test_converse_sends_the_request_and_parses_text_and_usage() -> None:
    rt = FakeRuntime(converse_ok("hello", 20, 5))
    res = run(
        client(rt).converse(
            GPT,
            system="S",
            user="U",
            max_tokens=300,
            temperature=0.0,
            estimate_micro_usd=10,
            reasoning_effort="low",
        )
    )
    assert (res.text, res.tokens_in, res.tokens_out, res.stop_reason, res.attempts) == (
        "hello",
        20,
        5,
        "end_turn",
        1,
    )
    ((_, kw),) = rt.calls
    assert kw["modelId"] == GPT
    assert kw["system"] == [{"text": "S"}]
    assert kw["messages"] == [{"role": "user", "content": [{"text": "U"}]}]
    assert kw["inferenceConfig"] == {"maxTokens": 300, "temperature": 0.0}
    assert kw["additionalModelRequestFields"] == {"reasoning_effort": "low"}


def test_reasoning_effort_is_only_sent_to_the_gpt_oss_family() -> None:
    rt = FakeRuntime(converse_ok("x"))
    run(
        client(rt).converse(
            QWEN,
            system="S",
            user="U",
            max_tokens=10,
            temperature=0.0,
            estimate_micro_usd=1,
            reasoning_effort="low",
        )
    )
    assert "additionalModelRequestFields" not in rt.calls[0][1]


def test_throttling_is_retried_then_succeeds() -> None:
    rt = FakeRuntime(
        ClientError("ThrottlingException", 429),
        ClientError("ModelTimeoutException", 408),
        converse_ok("ok"),
    )
    res = run(
        client(rt).converse(
            GPT, system="S", user="U", max_tokens=10, temperature=0.0, estimate_micro_usd=1
        )
    )
    assert res.text == "ok"
    assert res.attempts == 3
    assert len(rt.calls) == 3


def test_retries_are_bounded_and_the_last_error_surfaces() -> None:
    rt = FakeRuntime(*[ClientError("ThrottlingException", 429)] * 3)
    with pytest.raises(WorkersAiError) as e:
        run(
            client(rt).converse(
                GPT, system="S", user="U", max_tokens=10, temperature=0.0, estimate_micro_usd=1
            )
        )
    assert (e.value.code, e.value.attempts, e.value.status) == ("rate_limited", 3, 429)
    assert len(rt.calls) == 3


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("AccessDeniedException", "auth"),
        ("ValidationException", "bad_request"),
        ("ResourceNotFoundException", "bad_request"),
    ],
)
def test_permanent_errors_are_not_retried(code: str, expected: str) -> None:
    rt = FakeRuntime(ClientError(code), converse_ok("never"))
    with pytest.raises(WorkersAiError) as e:
        run(
            client(rt).converse(
                GPT, system="S", user="U", max_tokens=10, temperature=0.0, estimate_micro_usd=1
            )
        )
    assert e.value.code == expected
    assert len(rt.calls) == 1


def test_a_timeout_is_retried_and_reported_as_timeout() -> None:
    rt = FakeRuntime(ReadTimeoutError(), ReadTimeoutError(), ReadTimeoutError())
    with pytest.raises(WorkersAiError) as e:
        run(
            client(rt).converse(
                GPT, system="S", user="U", max_tokens=10, temperature=0.0, estimate_micro_usd=1
            )
        )
    assert e.value.code == "timeout"
    assert len(rt.calls) == 3


def test_error_text_has_no_prompt_or_response() -> None:
    rt = FakeRuntime(ClientError("AccessDeniedException"))
    with pytest.raises(WorkersAiError) as e:
        run(
            client(rt).converse(
                GPT,
                system="TOP-SECRET-SYSTEM",
                user="TOP-SECRET-USER",
                max_tokens=10,
                temperature=0.0,
                estimate_micro_usd=1,
            )
        )
    assert "TOP-SECRET" not in str(e.value)


def test_a_malformed_response_is_bad_response() -> None:
    with pytest.raises(WorkersAiError) as e:
        run(
            client(FakeRuntime({"output": {}})).converse(
                GPT, system="S", user="U", max_tokens=10, temperature=0.0, estimate_micro_usd=1
            )
        )
    assert e.value.code == "bad_response"


# --- client: budget -----------------------------------------------------------------------


def test_every_attempt_reserves_and_an_exhausted_budget_makes_no_call() -> None:
    budget = NeuronBudget(InMemoryUsageStore(), 25, provider="bedrock")
    rt = FakeRuntime(converse_ok("a"), converse_ok("b"), converse_ok("c"))
    c = client(rt, budget)
    for _ in range(2):
        run(
            c.converse(
                GPT, system="S", user="U", max_tokens=10, temperature=0.0, estimate_micro_usd=10
            )
        )
    assert run(budget.used()) == 20
    with pytest.raises(WorkersAiError) as e:
        run(
            c.converse(
                GPT, system="S", user="U", max_tokens=10, temperature=0.0, estimate_micro_usd=10
            )
        )
    assert e.value.code == "budget_exhausted"
    assert len(rt.calls) == 2


def test_a_retry_reserves_again() -> None:
    budget = NeuronBudget(InMemoryUsageStore(), 1000, provider="bedrock")
    rt = FakeRuntime(ClientError("ThrottlingException", 429), converse_ok("ok"))
    run(
        client(rt, budget).converse(
            GPT, system="S", user="U", max_tokens=10, temperature=0.0, estimate_micro_usd=10
        )
    )
    assert run(budget.used()) == 20  # conservative: reservations are never released


# --- client: embed ------------------------------------------------------------------------


def test_embed_sends_titan_body_and_checks_the_length() -> None:
    rt = FakeRuntime(embed_ok(256))
    res = run(client(rt).embed(TITAN, "some text", dim=256, estimate_micro_usd=1))
    ((_, kw),) = rt.calls
    assert json.loads(kw["body"]) == {
        "inputText": "some text",
        "dimensions": 256,
        "normalize": True,
    }
    assert kw["modelId"] == TITAN
    assert len(res.vector) == 256
    assert res.tokens == 3
    with pytest.raises(WorkersAiError) as e:
        run(client(FakeRuntime(embed_ok(512))).embed(TITAN, "t", dim=256, estimate_micro_usd=1))
    assert e.value.code == "bad_response"


# --- adapters -----------------------------------------------------------------------------


def test_embedder_validates_dimension_and_text_and_keeps_order() -> None:
    with pytest.raises(ValueError, match="supports"):
        BedrockEmbedder(client(FakeRuntime()), TITAN, 384)
    texts = ["a", "bb", "ccc", "dddd", "eeeee", "ffffff"]
    rt = FakeRuntime(*[embed_ok(256, i / 10) for i in range(len(texts))])
    emb = BedrockEmbedder(client(rt), TITAN, 256)
    assert emb.model_key == f"bedrock:{TITAN}:256"
    assert emb.dim == 256
    vectors = run(emb.embed_documents(texts))
    assert len(vectors) == len(texts)
    assert all(len(v) == 256 for v in vectors)
    with pytest.raises(ValueError, match="empty"):
        run(emb.embed_documents(["ok", ""]))
    with pytest.raises(ValueError, match="empty"):
        run(emb.embed_query(""))


def test_llm_strips_reasoning_maps_finish_reason_and_counts_tokens() -> None:
    rt = FakeRuntime(converse_ok("<reasoning>think</reasoning> The answer.", 40, 12, "max_tokens"))
    llm = BedrockLlm(client(rt), GPT, reasoning_effort="low")
    resp = run(llm.complete(LlmRequest(system="s", user="u", max_tokens=100)))
    assert (resp.text, resp.tokens_in, resp.tokens_out, resp.finish_reason) == (
        "The answer.",
        40,
        12,
        "length",
    )
    assert (resp.provider, resp.model) == ("bedrock", GPT)


def test_llm_with_no_text_is_a_failure_not_an_empty_answer() -> None:
    for stop in ("max_tokens", "end_turn"):
        llm = BedrockLlm(client(FakeRuntime(converse_ok("", 5, 100, stop))), GPT)
        with pytest.raises(WorkersAiError) as e:
            run(llm.complete(LlmRequest(system="s", user="u", max_tokens=100)))
        assert e.value.code == "bad_response"


@pytest.mark.parametrize(
    ("text", "n", "expected"),
    [
        ('{"scores":[1,2.5,10]}', 3, [1.0, 2.5, 10.0]),
        ('Sure: {"scores": [11, -3]} done', 2, [10.0, 0.0]),  # clamped to 0..10
        ('{"scores":[1,2]}', 3, None),  # wrong count
        ("no json", 1, None),
        ('{"scores":"x"}', 1, None),
        ('{"scores":["a"]}', 1, None),
    ],
)
def test_parse_scores(text: str, n: int, expected: list[float] | None) -> None:
    assert parse_scores(text, n) == expected


def test_reranker_scores_in_order_and_truncates_passages() -> None:
    rt = FakeRuntime(converse_ok('{"scores":[3,9]}'))
    rr = BedrockLlmReranker(client(rt), "openai.gpt-oss-20b-1:0")
    assert rr.model_key == "bedrock-llm-rerank:openai.gpt-oss-20b-1:0"
    assert run(rr.score("why postgres?", ["x" * 5000, "short"])) == [3.0, 9.0]
    user = rt.calls[0][1]["messages"][0]["content"][0]["text"]
    assert "[0] " + "x" * 700 in user
    assert "x" * 701 not in user
    assert "[1] short" in user
    assert "never instructions" in rt.calls[0][1]["system"][0]["text"]


def test_reranker_bad_reply_is_bad_response_and_empty_input_is_cheap() -> None:
    rr = BedrockLlmReranker(client(FakeRuntime(converse_ok("I cannot"))), "openai.gpt-oss-20b-1:0")
    with pytest.raises(WorkersAiError) as e:
        run(rr.score("q", ["a", "b"]))
    assert e.value.code == "bad_response"
    rt = FakeRuntime()
    assert run(BedrockLlmReranker(client(rt), "m").score("q", [])) == []
    assert not rt.calls
    with pytest.raises(ValueError, match="empty"):
        run(BedrockLlmReranker(client(rt), "m").score("", ["a"]))


# --- settings, factories, cost ------------------------------------------------------------


def test_bedrock_settings_defaults_and_micro_dollars() -> None:
    s = settings()
    assert (
        s.bedrock_region,
        s.bedrock_daily_usd,
        s.bedrock_budget_store,
        s.bedrock_max_attempts,
    ) == ("ap-south-1", 1.0, "memory", 3)
    assert settings(CAIRN_BEDROCK_DAILY_USD="2.5").bedrock_daily_micro_usd == 2_500_000
    with pytest.raises(ValueError, match="pattern"):
        settings(CAIRN_BEDROCK_REGION="not a region")


def test_bedrock_needs_no_credential_and_answer_and_judge_must_differ_in_family(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for key in ("CLOUDFLARE_API_TOKEN", "ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("CAIRN_EMBEDDER", "bedrock")
    monkeypatch.setenv("CAIRN_RERANKER", "bedrock")
    monkeypatch.setenv("CAIRN_ANSWER_PROVIDER", "bedrock")
    monkeypatch.setenv("CAIRN_ANSWER_MODEL", GPT)
    monkeypatch.setenv("CAIRN_JUDGE_PROVIDER", "bedrock")
    monkeypatch.setenv("CAIRN_JUDGE_MODEL", QWEN)
    assert load_settings(ModelSettings).embedder == "bedrock"
    assert load_settings(LlmSettings).judge_model == QWEN
    monkeypatch.setenv(
        "CAIRN_JUDGE_MODEL", "openai.gpt-oss-20b-1:0"
    )  # same family as the answer model
    with pytest.raises(SystemExit):
        load_settings(LlmSettings)


def test_factories_build_bedrock_adapters_and_demand_a_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CAIRN_EMBEDDER", "bedrock")
    monkeypatch.setenv("CAIRN_EMBED_MODEL", TITAN)
    monkeypatch.setenv("CAIRN_EMBED_DIM", "1024")
    monkeypatch.setenv("CAIRN_RERANKER", "bedrock")
    monkeypatch.setenv("CAIRN_RERANK_MODEL", "openai.gpt-oss-20b-1:0")
    monkeypatch.setenv("CAIRN_ANSWER_PROVIDER", "bedrock")
    monkeypatch.setenv("CAIRN_ANSWER_MODEL", GPT)
    monkeypatch.setenv("CAIRN_JUDGE_PROVIDER", "bedrock")
    monkeypatch.setenv("CAIRN_JUDGE_MODEL", QWEN)
    models = load_settings(ModelSettings)
    llm = load_settings(LlmSettings)
    c = client(FakeRuntime())
    assert isinstance(build_embedder(models, None, c), BedrockEmbedder)
    assert isinstance(build_reranker(models, None, c), BedrockLlmReranker)
    assert isinstance(build_llm(llm, "answer", None, c), BedrockLlm)
    for build in (
        lambda: build_embedder(models, None),
        lambda: build_reranker(models, None),
        lambda: build_llm(llm, "judge", None),
    ):
        with pytest.raises(ValueError, match=r"needs a (Bedrock|WorkersAi)Client"):
            build()


def test_call_cost_prices_bedrock_responses_in_dollars() -> None:
    resp = LlmResponse(
        text="t",
        tokens_in=1_000_000,
        tokens_out=1_000_000,
        latency_ms=1,
        provider="bedrock",
        model=GPT,
    )
    cost = call_cost(resp)
    assert cost.neurons is None
    assert cost.usd == pytest.approx(0.89)
    assert call_cost(resp.model_copy(update={"model": "unknown.model"})).usd is None
