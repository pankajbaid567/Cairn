"""Neuron estimator, in-memory usage store and budget guard (Requirements 15.1 to 15.4)."""

from __future__ import annotations

import asyncio
from datetime import date, timedelta

import pytest

from cairn_retrieval.cloudflare.budget import (
    NEURON_RATES,
    BudgetExhausted,
    InMemoryUsageStore,
    NeuronBudget,
    UnpricedModel,
    estimate_input_tokens,
    estimate_neurons,
    estimate_tokens,
)

DAY = date(2026, 11, 3)
EMBED = "@cf/baai/bge-small-en-v1.5"
GEMMA = "@cf/google/gemma-4-26b-a4b-it"
GLM = "@cf/zai-org/glm-4.7-flash"


# --- estimator -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "tokens"),
    [
        ("", 1),  # never zero
        ("a", 1),
        ("abc", 1),
        ("abcd", 2),
        ("é", 1),  # 2 bytes
        ("ééé", 2),  # 6 bytes
        ("日本", 2),  # 6 bytes
        ("x" * 300, 100),
    ],
)
def test_estimate_tokens_is_utf8_bytes_over_three_rounded_up(text: str, tokens: int) -> None:
    assert estimate_tokens(text) == tokens


def test_input_tokens_sum_the_per_text_estimates() -> None:
    assert estimate_input_tokens(["", "abcd", "x" * 300]) == 1 + 2 + 100
    assert estimate_input_tokens([]) == 0


@pytest.mark.parametrize(
    ("model", "tokens_in", "tokens_out", "neurons"),
    [
        (EMBED, 120_000, 0, 221),  # 120,000 * 1841 / 1e6 = 220.92, rounded up
        (EMBED, 1_000_000, 0, 1841),  # an exact whole number is not rounded up
        (EMBED, 0, 0, 1),  # at least one neuron
        (EMBED, 1, 0, 1),
        (GEMMA, 2_500, 400, 34),  # 22.73 + 10.91 = 33.64
        (GLM, 3_000, 200, 24),  # 16.5 + 7.28 = 23.78
        ("@cf/baai/bge-reranker-base", 10_500, 0, 3),  # 2.97
    ],
)
def test_estimate_neurons_follows_the_formula(
    model: str, tokens_in: int, tokens_out: int, neurons: int
) -> None:
    assert estimate_neurons(model, tokens_in, tokens_out) == neurons


def test_estimate_neurons_grows_with_input_and_output() -> None:
    assert estimate_neurons(GEMMA, 10_000) < estimate_neurons(GEMMA, 20_000)
    assert estimate_neurons(GEMMA, 1_000, 100) < estimate_neurons(GEMMA, 1_000, 5_000)


def test_every_default_model_has_a_rate() -> None:
    for model in (EMBED, "@cf/baai/bge-reranker-base", GEMMA, GLM):
        assert model in NEURON_RATES


def test_an_unpriced_model_raises_before_any_call() -> None:
    with pytest.raises(UnpricedModel) as raised:
        estimate_neurons("@cf/acme/unknown-model", 10)
    assert raised.value.model == "@cf/acme/unknown-model"


def test_negative_counts_are_rejected() -> None:
    with pytest.raises(ValueError, match="negative"):
        estimate_neurons(EMBED, -1)
    with pytest.raises(ValueError, match="negative"):
        estimate_neurons(GEMMA, 1, -1)


# --- in-memory store -------------------------------------------------------------------------


async def test_the_store_accepts_up_to_the_cap_and_refuses_past_it() -> None:
    store = InMemoryUsageStore()
    assert await store.try_reserve(DAY, "workers_ai", 60, 100) == 60
    assert await store.try_reserve(DAY, "workers_ai", 40, 100) == 100  # exactly the cap
    assert await store.try_reserve(DAY, "workers_ai", 1, 100) is None
    assert await store.used(DAY, "workers_ai") == 100  # the refusal changed nothing


async def test_a_single_reservation_above_the_cap_is_refused_on_an_empty_day() -> None:
    store = InMemoryUsageStore()
    assert await store.try_reserve(DAY, "workers_ai", 101, 100) is None
    assert await store.used(DAY, "workers_ai") == 0


async def test_days_and_providers_are_counted_separately() -> None:
    store = InMemoryUsageStore()
    await store.try_reserve(DAY, "workers_ai", 100, 100)
    assert await store.try_reserve(DAY + timedelta(days=1), "workers_ai", 100, 100) == 100
    assert await store.try_reserve(DAY, "other", 100, 100) == 100
    assert await store.used(DAY, "workers_ai") == 100


@pytest.mark.parametrize("provider", ["", "Workers", "1abc", "a-b", "x" * 33])
async def test_the_store_applies_the_same_provider_rule_as_the_table(provider: str) -> None:
    with pytest.raises(ValueError, match="provider"):
        await InMemoryUsageStore().try_reserve(DAY, provider, 1, 10)


async def test_the_store_rejects_negative_numbers() -> None:
    store = InMemoryUsageStore()
    with pytest.raises(ValueError, match="neurons"):
        await store.try_reserve(DAY, "workers_ai", -1, 10)
    with pytest.raises(ValueError, match="cap"):
        await store.try_reserve(DAY, "workers_ai", 1, -1)


async def test_concurrent_reservations_never_pass_the_cap() -> None:
    store = InMemoryUsageStore()
    results = await asyncio.gather(
        *(store.try_reserve(DAY, "workers_ai", 7, 100) for _ in range(40))
    )
    accepted = [r for r in results if r is not None]
    assert len(accepted) == 14  # 14 * 7 = 98; the 15th would make 105
    assert await store.used(DAY, "workers_ai") == 98


# --- the guard -------------------------------------------------------------------------------


async def test_reserve_returns_the_running_total_and_refuses_at_the_cap() -> None:
    budget = NeuronBudget(InMemoryUsageStore(), 10, today=lambda: DAY)
    assert await budget.reserve(4) == 4
    assert await budget.reserve(6) == 10
    with pytest.raises(BudgetExhausted) as raised:
        await budget.reserve(1)
    assert (raised.value.requested, raised.value.cap) == (1, 10)
    assert await budget.used() == 10


async def test_the_budget_follows_the_utc_day() -> None:
    today = [DAY]
    budget = NeuronBudget(InMemoryUsageStore(), 10, today=lambda: today[0])
    await budget.reserve(10)
    with pytest.raises(BudgetExhausted):
        await budget.reserve(1)
    today[0] = DAY + timedelta(days=1)
    assert await budget.reserve(1) == 1  # a new day starts at zero
    assert await budget.used() == 1


async def test_a_cap_of_zero_refuses_every_call() -> None:
    budget = NeuronBudget(InMemoryUsageStore(), 0, today=lambda: DAY)
    with pytest.raises(BudgetExhausted):
        await budget.reserve(1)


def test_the_guard_rejects_a_negative_cap_and_a_bad_provider() -> None:
    with pytest.raises(ValueError, match="cap"):
        NeuronBudget(InMemoryUsageStore(), -1)
    with pytest.raises(ValueError, match="provider"):
        NeuronBudget(InMemoryUsageStore(), 1, provider="Bad Name")
