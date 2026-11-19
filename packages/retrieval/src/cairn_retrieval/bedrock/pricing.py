"""Bedrock prices and cost estimates, in micro-dollars (1,000,000 = 1 USD).

A price in USD per million tokens is numerically the same as micro-dollars per token, so
``estimate_micro_usd`` is ``tokens_in * input_rate + max_tokens_out * output_rate``. A model with
no entry here is refused before any call (``UnpricedBedrockModel``): no call without a price.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Final

from cairn_retrieval.cloudflare.budget import estimate_tokens

__all__ = ["BEDROCK_RATES", "BedrockRate", "UnpricedBedrockModel", "estimate_micro_usd", "usd"]


@dataclass(frozen=True)
class BedrockRate:
    """USD per million tokens. Embedding models have no output price."""

    input_per_m: float
    output_per_m: float = 0.0


BEDROCK_RATES: Final[dict[str, BedrockRate]] = {
    # AWS Price List API, Asia Pacific (Mumbai), on-demand, read 2026-11-19.
    "openai.gpt-oss-120b-1:0": BedrockRate(0.18, 0.71),
    "openai.gpt-oss-20b-1:0": BedrockRate(0.08, 0.35),
    "qwen.qwen3-32b-v1:0": BedrockRate(0.18, 0.355),
    "zai.glm-4.7-flash": BedrockRate(0.08, 0.48),
    "zai.glm-4.7": BedrockRate(0.72, 2.64),
    # Titan Text Embeddings V2: public list price, not re-read from the Mumbai price list.
    "amazon.titan-embed-text-v2:0": BedrockRate(0.02),
}


class UnpricedBedrockModel(Exception):
    def __init__(self, model: str) -> None:
        super().__init__(f"no Bedrock rate for model {model!r}")
        self.model = model


def estimate_micro_usd(model: str, tokens_in: int, max_tokens_out: int = 0) -> int:
    """Upper-leaning estimate of one call, at least 1. Raises ``UnpricedBedrockModel``."""
    rate = BEDROCK_RATES.get(model)
    if rate is None:
        raise UnpricedBedrockModel(model)
    if tokens_in < 0 or max_tokens_out < 0:
        raise ValueError("token counts must not be negative")
    return max(1, math.ceil(tokens_in * rate.input_per_m + max_tokens_out * rate.output_per_m))


def usd(model: str, tokens_in: int, tokens_out: int) -> float | None:
    """The cost of a finished call in USD (actual token counts), ``None`` when unpriced."""
    rate = BEDROCK_RATES.get(model)
    if rate is None:
        return None
    return (tokens_in * rate.input_per_m + tokens_out * rate.output_per_m) / 1_000_000


def text_tokens(text: str) -> int:
    """The estimate used for an input text (same rule as the Workers AI budget)."""
    return estimate_tokens(text)
