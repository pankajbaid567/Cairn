"""Property test for neuron estimates (task 8.5).

Feature: cairn-free-tier-retarget, Property 20: Neuron estimates follow the formula and grow with
input

**Validates: Requirements 15.3**

For any list of input texts, any ``max_tokens_out`` from 0 to 4,000 and any priced model:

- ``estimate_neurons`` equals ``max(1, ceil((tokens_in * input_rate + max_tokens_out *
  output_rate) / 1,000,000))`` with ``tokens_in`` the sum of ``max(1, ceil(utf8_bytes / 3))`` per
  text;
- the estimate never decreases when a text is lengthened, a text is added, or ``max_tokens_out``
  grows.

The oracle is written from the requirement: it counts UTF-8 bytes itself and does the division
with integer arithmetic on the rate numerators (rates are whole numbers in the table), so a
float rounding slip or a change in the token rule in the code shows up here. The rates are read
from ``NEURON_RATES`` because the price table is data (FV9), not the formula under test. No
database is involved.
"""

from __future__ import annotations

from fractions import Fraction

from hypothesis import given
from hypothesis import strategies as st

from cairn_retrieval.cloudflare.budget import (
    NEURON_RATES,
    estimate_input_tokens,
    estimate_neurons,
    estimate_tokens,
)

MILLION = 1_000_000

models = st.sampled_from(sorted(NEURON_RATES))
# Mixed scripts so UTF-8 byte counts differ from character counts; empty texts are allowed.
texts = st.text(max_size=200)
text_lists = st.lists(texts, max_size=8)
tokens_out = st.integers(min_value=0, max_value=4_000)


def oracle_tokens(text: str) -> int:
    n_bytes = len(text.encode("utf-8"))
    return max(1, -(-n_bytes // 3))


def oracle_neurons(model: str, inputs: list[str], max_out: int) -> int:
    rate = NEURON_RATES[model]
    tokens_in = sum(oracle_tokens(t) for t in inputs)
    total = Fraction(rate.input_per_m) * tokens_in + Fraction(rate.output_per_m) * max_out
    # ceil(total / 1e6) without floats
    ceiling = -((-total.numerator) // (total.denominator * MILLION))
    return max(1, ceiling)


def estimate(model: str, inputs: list[str], max_out: int) -> int:
    return estimate_neurons(model, estimate_input_tokens(inputs), max_out)


@given(text=texts)
def test_token_estimate_is_utf8_bytes_over_three_rounded_up(text: str) -> None:
    assert estimate_tokens(text) == oracle_tokens(text)


@given(model=models, inputs=text_lists, max_out=tokens_out)
def test_estimate_follows_the_formula(model: str, inputs: list[str], max_out: int) -> None:
    assert estimate_input_tokens(inputs) == sum(oracle_tokens(t) for t in inputs)
    assert estimate(model, inputs, max_out) == oracle_neurons(model, inputs, max_out)


@given(model=models, inputs=text_lists, max_out=tokens_out)
def test_estimate_is_at_least_one(model: str, inputs: list[str], max_out: int) -> None:
    assert estimate(model, inputs, max_out) >= 1


@given(
    model=models,
    inputs=st.lists(texts, min_size=1, max_size=8),
    data=st.data(),
    suffix=texts,
    max_out=tokens_out,
)
def test_estimate_does_not_decrease_when_a_text_is_lengthened(
    model: str, inputs: list[str], data: st.DataObject, suffix: str, max_out: int
) -> None:
    index = data.draw(st.integers(min_value=0, max_value=len(inputs) - 1))
    longer = list(inputs)
    longer[index] = inputs[index] + suffix
    assert estimate(model, longer, max_out) >= estimate(model, inputs, max_out)


@given(model=models, inputs=text_lists, extra=texts, max_out=tokens_out)
def test_estimate_does_not_decrease_when_a_text_is_added(
    model: str, inputs: list[str], extra: str, max_out: int
) -> None:
    assert estimate(model, [*inputs, extra], max_out) >= estimate(model, inputs, max_out)


@given(
    model=models,
    inputs=text_lists,
    low=tokens_out,
    high=tokens_out,
)
def test_estimate_does_not_decrease_when_max_tokens_out_grows(
    model: str, inputs: list[str], low: int, high: int
) -> None:
    low, high = sorted((low, high))
    assert estimate(model, inputs, high) >= estimate(model, inputs, low)
