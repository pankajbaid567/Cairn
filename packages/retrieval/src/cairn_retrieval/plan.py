"""Neuron estimates for live runs, checked before the first provider call (PD1-27).

Live runners (importer ``--embed-inline``, eval, S3, model pick) compute an upper bound from the
uncached inputs and exit 3 before any call when it passes the remaining allocation.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from cairn_retrieval.cloudflare.budget import (
    NeuronBudget,
    estimate_input_tokens,
    estimate_neurons,
)
from cairn_retrieval.models.cache import ModelCache

EXIT_INSUFFICIENT_BUDGET = 3
EMBED_BATCH = 50


def estimate_units(model: str, tokens_in: int, max_tokens_out: int = 0) -> int:
    """Budget units of one call: neurons for a Workers AI model, micro-dollars for a Bedrock one.

    Both budgets count in the same ``cairn_meta.provider_usage`` table, so a run is checked against
    whichever budget its provider uses. A model with no price raises (no call without a price)."""
    from cairn_retrieval.bedrock.pricing import BEDROCK_RATES, estimate_micro_usd

    if model in BEDROCK_RATES:
        return estimate_micro_usd(model, tokens_in, max_tokens_out)
    return estimate_neurons(model, tokens_in, max_tokens_out)


@dataclass(frozen=True)
class RunEstimate:
    calls: int
    neurons: int
    cached_inputs: int
    uncached_inputs: int


class InsufficientBudget(Exception):
    def __init__(self, estimate: RunEstimate, remaining: int) -> None:
        super().__init__(f"estimate {estimate.neurons} neurons exceeds the {remaining} remaining")
        self.estimate = estimate
        self.remaining = remaining


def estimate_embed(
    model: str,
    texts: Sequence[str],
    cache: ModelCache | None,
    *,
    kind: str = "doc",
    cache_key: str | None = None,
) -> RunEstimate:
    uncached = list(texts)
    if cache is not None:
        found = cache.get_vectors(cache_key or model, kind, texts)
        uncached = [t for t, v in zip(texts, found, strict=True) if v is None]
    neurons = 0
    calls = 0
    for i in range(0, len(uncached), EMBED_BATCH):
        batch = uncached[i : i + EMBED_BATCH]
        neurons += estimate_units(model, estimate_input_tokens(batch))
        calls += 1
    return RunEstimate(calls, neurons, len(texts) - len(uncached), len(uncached))


def estimate_rerank(
    model: str,
    queries: Sequence[str],
    passages_per_query: int,
    passage_tokens: int,
    cache: ModelCache | None,
) -> RunEstimate:
    n = len(queries)
    tokens = passages_per_query * passage_tokens
    return RunEstimate(n, n * estimate_units(model, tokens), 0, n)


def estimate_answers(model: str, n: int, tokens_in: int, max_tokens_out: int) -> RunEstimate:
    return RunEstimate(n, n * estimate_units(model, tokens_in, max_tokens_out), 0, n)


async def require_budget(budget: NeuronBudget, estimate: RunEstimate) -> None:
    remaining = budget.cap - await budget.used()
    if estimate.neurons > remaining:
        raise InsufficientBudget(estimate, max(0, remaining))
