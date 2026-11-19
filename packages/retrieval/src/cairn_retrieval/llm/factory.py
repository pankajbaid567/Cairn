"""Builder that picks the answer or judge model from ``LlmSettings`` (design §13)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from cairn_core.config import BedrockSettings, LlmSettings
from cairn_retrieval.cloudflare.client import WorkersAiClient
from cairn_retrieval.llm.extractive import FakeProvider
from cairn_retrieval.llm.fake import FakeLlm
from cairn_retrieval.llm.http_providers import AnthropicLlm, OpenAiLlm
from cairn_retrieval.llm.types import LlmProvider
from cairn_retrieval.llm.workers_ai import WorkersAiLlm

if TYPE_CHECKING:
    from cairn_retrieval.bedrock.client import BedrockClient

__all__ = ["build_llm"]


def build_llm(
    llm: LlmSettings,
    role: Literal["answer", "judge"],
    client: WorkersAiClient | None,
    bedrock: BedrockClient | None = None,
) -> LlmProvider:
    """The provider for ``role``: the fakes, Workers AI, or the optional Anthropic and OpenAI
    adapters (which need their key). The fake answer model is extractive (FakeProvider)."""
    if role == "answer":
        provider, model, key = llm.answer_provider, llm.answer_model, "CAIRN_ANSWER_PROVIDER"
    else:
        provider, model, key = llm.judge_provider, llm.judge_model, "CAIRN_JUDGE_PROVIDER"
    if provider == "fake":
        return FakeProvider() if role == "answer" else FakeLlm()
    if provider == "bedrock":
        if bedrock is None:
            raise ValueError(f"{key}=bedrock needs a BedrockClient")
        from cairn_retrieval.bedrock.adapters import (
            BedrockLlm,  # lazy: avoids a package import cycle
        )

        effort = llm.bedrock_reasoning_effort if isinstance(llm, BedrockSettings) else ""
        return BedrockLlm(bedrock, model, reasoning_effort=effort)
    if provider == "workers_ai":
        if client is None:
            raise ValueError(f"{key}=workers_ai needs a WorkersAiClient")
        return WorkersAiLlm(client, model)
    if provider == "anthropic" and llm.anthropic_api_key is not None:
        return AnthropicLlm(llm.anthropic_api_key, model)
    if provider == "openai" and llm.openai_api_key is not None:
        return OpenAiLlm(llm.openai_api_key, model)
    raise ValueError(f"{key}={provider} needs its API key")
