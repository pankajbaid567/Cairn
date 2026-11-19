"""Property test for the neuron budget cap (free-tier retarget design, Requirement 15.2).

**Validates: Requirements 15.2**

For any cap from 0 to 20,000 and any sequence of reservation requests (sizes 1 to 5,000)
spread over one or more UTC days, ``NeuronBudget`` over the in-memory store must satisfy, for
every request:

- it is admitted if and only if the day's running total plus its size is at most the cap;
- an admitted request returns the new total for that day;
- a refused request raises ``BudgetExhausted`` and leaves that day's total unchanged;
- no day's total ever passes the cap;
- a day not seen before starts at zero, and other days' totals are untouched.

A reference model (a plain dict of per-day totals) predicts every outcome. The budget reads the
current day from a mutable cell, so each request picks its own UTC day. The async code runs
through ``asyncio.run`` inside a synchronous test because Hypothesis does not reset
function-scoped fixtures between examples; each example builds a fresh store.
"""
# Feature: cairn-free-tier-retarget, Property 19: The neuron budget never admits past the cap

from __future__ import annotations

import asyncio
from datetime import date, timedelta

import pytest
from hypothesis import example, given
from hypothesis import strategies as st

from cairn_retrieval.cloudflare.budget import BudgetExhausted, InMemoryUsageStore, NeuronBudget

FIRST_DAY = date(2026, 11, 3)
PROVIDER = "workers_ai"

_caps = st.integers(min_value=0, max_value=20_000)
# (day offset from FIRST_DAY, reservation size); few days so that totals build up per day.
_requests = st.lists(
    st.tuples(st.integers(min_value=0, max_value=3), st.integers(min_value=1, max_value=5_000)),
    max_size=60,
)


async def _run(cap: int, requests: list[tuple[int, int]]) -> None:
    store = InMemoryUsageStore()
    current = [FIRST_DAY]
    budget = NeuronBudget(store, cap, provider=PROVIDER, today=lambda: current[0])
    model: dict[date, int] = {}

    for offset, size in requests:
        day = FIRST_DAY + timedelta(days=offset)
        current[0] = day
        before = model.get(day, 0)
        snapshot = dict(model)

        if before + size <= cap:
            total = await budget.reserve(size)
            model[day] = before + size
            assert total == model[day]
            assert total <= cap
        else:
            with pytest.raises(BudgetExhausted) as info:
                await budget.reserve(size)
            assert info.value.requested == size
            assert info.value.cap == cap
            assert model == snapshot  # a refusal changes nothing in the model ...

        # ... and the store agrees with the model for every day seen so far, never past the cap.
        for seen in {FIRST_DAY + timedelta(days=o) for o, _ in requests}:
            current[0] = seen
            used = await budget.used()
            assert used == model.get(seen, 0)
            assert used <= cap
        current[0] = day


@given(cap=_caps, requests=_requests)
@example(cap=0, requests=[(0, 1)])  # a zero cap admits nothing
@example(cap=5_000, requests=[(0, 5_000), (0, 1), (1, 5_000)])  # exact fit, refusal, new day
@example(cap=9_000, requests=[(0, 4_500), (0, 4_500), (0, 1)])  # the cap itself is admitted
def test_budget_never_admits_past_the_cap(cap: int, requests: list[tuple[int, int]]) -> None:
    asyncio.run(_run(cap, requests))
