"""Unit tests for cairn_core.model_family (free-tier FT-24, Requirement 16.7)."""

from __future__ import annotations

import pytest

from cairn_core.model_family import FAMILY_PREFIXES, UnknownModelFamily, model_family


@pytest.mark.parametrize(
    ("provider", "model", "family"),
    [
        ("workers_ai", "@cf/google/gemma-4-26b-a4b-it", "google"),
        ("workers_ai", "@cf/zai-org/glm-4.7-flash", "zhipu"),
        ("workers_ai", "@cf/meta/llama-3.1-8b-instruct", "meta"),
        ("workers_ai", "@cf/qwen/qwen3-30b-a3b-fp8", "alibaba"),
        ("workers_ai", "@cf/mistralai/mistral-small-3.1-24b-instruct", "mistral"),
        ("workers_ai", "@cf/nvidia/nemotron", "nvidia"),
        ("workers_ai", "@cf/openai/gpt-oss-20b", "openai"),
        ("workers_ai", "@cf/deepseek-ai/deepseek-r1-distill-qwen-32b", "deepseek"),
        ("workers_ai", "@cf/ibm-granite/granite-4.0-h-micro", "ibm"),
        ("anthropic", "claude-sonnet-4", "anthropic"),
        ("openai", "gpt-4o", "openai"),
        ("openai", "o3-mini", "openai"),
    ],
)
def test_known_models_map_to_their_family(provider: str, model: str, family: str) -> None:
    assert model_family(provider, model) == family


@pytest.mark.parametrize("model", ["fake-llm-v1", "@cf/google/gemma-4-26b-a4b-it", "anything"])
def test_the_fake_provider_is_always_the_fake_family(model: str) -> None:
    assert model_family("fake", model) == "fake"


def test_the_default_answer_and_judge_models_differ() -> None:
    assert model_family("workers_ai", "@cf/google/gemma-4-26b-a4b-it") != model_family(
        "workers_ai", "@cf/zai-org/glm-4.7-flash"
    )


@pytest.mark.parametrize("provider", ["workers_ai", "anthropic"])
def test_the_bare_o_prefix_is_for_openai_only(provider: str) -> None:
    with pytest.raises(UnknownModelFamily):
        model_family(provider, "o3-mini")


@pytest.mark.parametrize(
    ("provider", "model"),
    [("workers_ai", "@cf/newvendor/model-x"), ("anthropic", "mystery"), ("openai", "x1")],
)
def test_unknown_models_raise(provider: str, model: str) -> None:
    with pytest.raises(UnknownModelFamily) as exc:
        model_family(provider, model)
    assert exc.value.provider == provider
    assert exc.value.model == model
    assert isinstance(exc.value, ValueError)


def test_prefix_table_has_no_shadowed_entries() -> None:
    # A later prefix that starts with an earlier one could never match.
    prefixes = [p for p, _ in FAMILY_PREFIXES]
    for i, later in enumerate(prefixes):
        for earlier in prefixes[:i]:
            assert not later.startswith(earlier), (earlier, later)
