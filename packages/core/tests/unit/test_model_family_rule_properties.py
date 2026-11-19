"""Property test for the answer and judge model family rule.

Feature: cairn-free-tier-retarget, Property 23: Answer and judge must come from different
model families

Validates: Requirements 16.7

For any pair of (provider, model) choices drawn from a catalog of the Workers AI chat models,
Anthropic and OpenAI model names, and the fake provider, ``LlmSettings`` loads if and only if
one side is the fake provider or ``model_family`` differs between the two sides. When it
refuses, the process exits with status 2 and the message names ``CAIRN_ANSWER_MODEL`` and
``CAIRN_JUDGE_MODEL``, the code ``same_model_family``, and no model name, key or credential.

Hypothesis does not reset function-scoped fixtures between examples, so this module uses no
fixtures: each example patches ``os.environ`` (every key ``LlmSettings`` reads is cleared
first) and changes into a fresh temporary directory (no ``.env``), then restores both.

The catalog is written out here, not imported from the code under test. Every example holds
all three credentials, so only the family rule can refuse.
"""

from __future__ import annotations

import contextlib
import io
import os
import tempfile
from collections.abc import Iterator
from unittest.mock import patch

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from cairn_core.config import CONFIG_EXIT_STATUS, LlmSettings, env_keys, load_settings
from cairn_core.model_family import model_family

PBT = settings(deadline=None)

# Distinctive fake credentials, so a leak is found by grepping for them.
CF_TOKEN = "cf-token-FAKE-7f3a91c2d4e5"
ANTHROPIC_KEY = "anthropic-key-FAKE-7f3a91c2d4e5"
OPENAI_KEY = "openai-key-FAKE-7f3a91c2d4e5"

CATALOG: tuple[tuple[str, str], ...] = (
    ("workers_ai", "@cf/google/gemma-4-26b-a4b-it"),
    ("workers_ai", "@cf/google/gemma-3-12b-it"),
    ("workers_ai", "@cf/zai-org/glm-4.7-flash"),
    ("workers_ai", "@cf/meta/llama-3.1-8b-instruct"),
    ("workers_ai", "@cf/meta/llama-3.3-70b-instruct-fp8-fast"),
    ("workers_ai", "@cf/qwen/qwen3-30b-a3b-fp8"),
    ("workers_ai", "@cf/mistralai/mistral-small-3.1-24b-instruct"),
    ("workers_ai", "@cf/nvidia/nemotron-3-120b-a12b"),
    ("workers_ai", "@cf/openai/gpt-oss-20b"),
    ("workers_ai", "@cf/deepseek-ai/deepseek-r1-distill-qwen-32b"),
    ("workers_ai", "@cf/ibm-granite/granite-4.0-h-micro"),
    ("anthropic", "claude-sonnet-4"),
    ("anthropic", "claude-haiku-4-5"),
    ("openai", "gpt-4o"),
    ("openai", "gpt-4o-mini"),
    ("openai", "o3-mini"),
    ("fake", "fake-llm-v1"),
    ("fake", "@cf/google/gemma-4-26b-a4b-it"),
)

choices = st.sampled_from(CATALOG)


@contextlib.contextmanager
def isolated_environment(values: dict[str, str]) -> Iterator[None]:
    """``os.environ`` with ``values`` set for the keys ``LlmSettings`` reads, in an empty cwd."""
    previous_cwd = os.getcwd()
    with tempfile.TemporaryDirectory() as tmp:
        cleared = {k: v for k, v in os.environ.items() if k not in env_keys(LlmSettings)}
        with patch.dict(os.environ, {**cleared, **values}, clear=True):
            os.chdir(tmp)
            try:
                yield
            finally:
                os.chdir(previous_cwd)


def environment(answer: tuple[str, str], judge: tuple[str, str]) -> dict[str, str]:
    return {
        "CAIRN_ANSWER_PROVIDER": answer[0],
        "CAIRN_ANSWER_MODEL": answer[1],
        "CAIRN_JUDGE_PROVIDER": judge[0],
        "CAIRN_JUDGE_MODEL": judge[1],
        "CLOUDFLARE_API_TOKEN": CF_TOKEN,
        "ANTHROPIC_API_KEY": ANTHROPIC_KEY,
        "OPENAI_API_KEY": OPENAI_KEY,
    }


def expected_valid(answer: tuple[str, str], judge: tuple[str, str]) -> bool:
    """The rule restated from the requirement, using the family function it names."""
    if "fake" in (answer[0], judge[0]):
        return True
    return model_family(*answer) != model_family(*judge)


@PBT
@given(answer=choices, judge=choices)
def test_llm_settings_validate_iff_one_side_is_fake_or_families_differ(
    answer: tuple[str, str], judge: tuple[str, str]
) -> None:
    stderr = io.StringIO()
    with isolated_environment(environment(answer, judge)), contextlib.redirect_stderr(stderr):
        if expected_valid(answer, judge):
            loaded = load_settings(LlmSettings)
            assert (loaded.answer_provider, loaded.answer_model) == answer
            assert (loaded.judge_provider, loaded.judge_model) == judge
            assert stderr.getvalue() == ""
            return
        with pytest.raises(SystemExit) as refused:
            load_settings(LlmSettings)
        assert refused.value.code == CONFIG_EXIT_STATUS

    message = stderr.getvalue()
    assert "CAIRN_ANSWER_MODEL" in message
    assert "CAIRN_JUDGE_MODEL" in message
    assert "same_model_family" in message
    for value in (answer[1], judge[1], CF_TOKEN, ANTHROPIC_KEY, OPENAI_KEY):
        assert value not in message
