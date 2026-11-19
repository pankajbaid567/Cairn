"""Estimated cost per query (plan 5.8).

Neurons per call follow ``NEURON_RATES`` (Workers AI's published per-million-token rates as
recorded in the code, marked Verify) applied to token estimates: the same ``bytes / 3`` rule the
budget guard uses, which leans high. Estimates are not measurements: a live run records the
neurons actually reserved next to them, and the tables show both. Dollars use the Workers AI paid
rate of USD 0.011 per 1,000 neurons (Verify against the current pricing page).
"""

from __future__ import annotations

from typing import Final

from cairn_core.config import SearchSettings
from cairn_retrieval.cloudflare.budget import NEURON_RATES, estimate_tokens

USD_PER_1K_NEURONS: Final = 0.011  # Verify: Workers AI paid rate
ANSWER_OUT_TOKENS: Final = 250  # a typical cited answer; the budget guard reserves more


def _default(name: str) -> str:
    return str(SearchSettings.model_fields[name].default)


def neurons(model: str, tokens_in: int, tokens_out: int = 0) -> float:
    """Fractional neurons for one call; 0.0 for a model with no rate (fakes, paid providers)."""
    rate = NEURON_RATES.get(model)
    if rate is None:
        return 0.0
    return (rate.input_per_m * tokens_in + rate.output_per_m * tokens_out) / 1_000_000


def query_neurons(
    query: str,
    passages: list[str],
    *,
    answer: bool,
    answer_prompt: str = "",
    out_tokens: int = ANSWER_OUT_TOKENS,
) -> float:
    """Embed the query, rerank ``passages`` against it, and (for B3) compose an answer."""
    total = neurons(_default("embed_model"), estimate_tokens(query))
    if passages:
        total += neurons(
            _default("rerank_model"), sum(estimate_tokens(query + p) for p in passages)
        )
    if answer:
        total += neurons(_default("answer_model"), estimate_tokens(answer_prompt), out_tokens)
    return total


def dollars(neuron_count: float) -> float:
    return neuron_count / 1000 * USD_PER_1K_NEURONS
