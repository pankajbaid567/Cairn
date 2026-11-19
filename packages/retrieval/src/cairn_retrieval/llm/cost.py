"""What a model call cost, in neurons (Workers AI) or dollars (paid providers) — design §16."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from cairn_retrieval.bedrock.pricing import usd as bedrock_usd
from cairn_retrieval.cloudflare.budget import NEURON_RATES, estimate_neurons
from cairn_retrieval.llm.types import LlmResponse

# USD per million tokens (input, output). User-supplied candidates; Verify V20 on the day.
USD_PER_MILLION: Final[dict[str, tuple[float, float]]] = {
    "claude-haiku-5-5": (1.0, 5.0),
    "claude-sonnet-5-5": (3.0, 15.0),
    "gpt-5-mini": (0.25, 2.0),
}


@dataclass(frozen=True)
class CallCost:
    neurons: int | None
    usd: float | None


def call_cost(resp: LlmResponse) -> CallCost:
    if resp.provider == "workers_ai" and resp.model in NEURON_RATES:
        return CallCost(estimate_neurons(resp.model, resp.tokens_in, resp.tokens_out), None)
    if resp.provider == "bedrock":
        return CallCost(None, bedrock_usd(resp.model, resp.tokens_in, resp.tokens_out))
    price = USD_PER_MILLION.get(resp.model)
    if price is not None:
        return CallCost(None, (resp.tokens_in * price[0] + resp.tokens_out * price[1]) / 1_000_000)
    return CallCost(None, None)
