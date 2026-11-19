"""Property test for backoff delays (task 8.9).

Feature: cairn-free-tier-retarget, Property 15: Backoff delays are bounded and respect Retry-After

**Validates: Requirements 14.5**

For any attempt number from 1 to 5, any ``u`` in [0, 1) and any Retry-After value (absent, 0,
positive, very large), ``backoff_delay`` returns ``min(retry_after, 30)`` when Retry-After is
present, and otherwise ``u`` times ``min(8, 0.5 * 2 ** (attempt - 1))``, a value between 0 and
that bound.

The oracle is written from the requirement with the default constants (0.5, 8, 30) spelled out,
so a change to a default in ``RetryPolicy`` shows up here. Further properties cover:

- the same rules for any policy (the bounds come from the policy's own fields);
- attempt numbers far beyond 5 (no overflow, the cap holds);
- Retry-After values that cannot be used (negative, NaN, infinite), which fall back to the backoff;
- the header path: a ``Retry-After`` header of whole seconds goes through ``parse_retry_after`` and
  ``backoff_delay`` to ``min(seconds, 30)``, and one that cannot be parsed falls back to the
  backoff.

No transport, sleep or socket is involved; both functions are pure.
"""

from __future__ import annotations

import math

import pytest
from hypothesis import example, given
from hypothesis import strategies as st

from cairn_retrieval.cloudflare.client import RetryPolicy, backoff_delay, parse_retry_after

DEFAULT_BASE_S = 0.5
DEFAULT_CAP_S = 8.0
DEFAULT_RETRY_AFTER_CAP_S = 30.0

attempts = st.integers(min_value=1, max_value=5)
many_attempts = st.integers(min_value=1, max_value=10_000)
unit = st.floats(min_value=0.0, max_value=1.0, exclude_max=True)
# Finite, non-negative: zero, small, around the cap, and very large values all occur.
usable_retry_after = st.floats(min_value=0.0, allow_nan=False, allow_infinity=False)
unusable_retry_after = st.one_of(
    st.floats(max_value=-1e-9, allow_nan=False, allow_infinity=False),
    st.just(float("-inf")),
    st.just(float("inf")),
    st.just(float("nan")),
)
policies = st.builds(
    RetryPolicy,
    base_s=st.floats(min_value=0.001, max_value=10.0),
    cap_s=st.floats(min_value=0.001, max_value=120.0),
    retry_after_cap_s=st.floats(min_value=0.0, max_value=600.0),
)


def oracle_bound(attempt: int) -> float:
    """The requirement's upper bound on the random delay, with the default constants."""
    return min(DEFAULT_CAP_S, DEFAULT_BASE_S * 2.0 ** (attempt - 1))


@given(attempt=attempts, u=unit, retry_after=st.none() | usable_retry_after)
@example(attempt=1, u=0.0, retry_after=None)
@example(attempt=5, u=0.9999999999999999, retry_after=None)
@example(attempt=3, u=0.5, retry_after=0.0)
@example(attempt=3, u=0.5, retry_after=30.0)
@example(attempt=3, u=0.5, retry_after=1e300)
def test_default_policy_follows_the_requirement(
    attempt: int, u: float, retry_after: float | None
) -> None:
    delay = backoff_delay(attempt, RetryPolicy(), retry_after, u)
    if retry_after is not None:
        assert delay == min(retry_after, DEFAULT_RETRY_AFTER_CAP_S)
    else:
        bound = oracle_bound(attempt)
        assert 0.0 <= delay <= bound
        assert delay == u * bound


@given(attempt=attempts, u=unit, retry_after=usable_retry_after)
def test_retry_after_ignores_attempt_and_jitter(attempt: int, u: float, retry_after: float) -> None:
    policy = RetryPolicy()
    reference = backoff_delay(1, policy, retry_after, 0.0)
    assert backoff_delay(attempt, policy, retry_after, u) == reference
    assert 0.0 <= reference <= DEFAULT_RETRY_AFTER_CAP_S


@given(attempt=many_attempts, u=unit)
def test_cap_holds_for_any_attempt_number(attempt: int, u: float) -> None:
    delay = backoff_delay(attempt, RetryPolicy(), None, u)
    assert math.isfinite(delay)
    assert 0.0 <= delay <= DEFAULT_CAP_S
    if attempt >= 5:  # 0.5 * 2 ** 4 = 8: the cap applies from the fifth attempt on
        assert delay == u * DEFAULT_CAP_S


@given(attempt=attempts, u=unit)
def test_random_delay_does_not_shrink_with_later_attempts(attempt: int, u: float) -> None:
    policy = RetryPolicy()
    assert backoff_delay(attempt + 1, policy, None, u) >= backoff_delay(attempt, policy, None, u)


@given(
    policy=policies,
    attempt=many_attempts,
    u=unit,
    retry_after=st.none() | usable_retry_after,
)
def test_any_policy_uses_its_own_bounds(
    policy: RetryPolicy, attempt: int, u: float, retry_after: float | None
) -> None:
    delay = backoff_delay(attempt, policy, retry_after, u)
    if retry_after is not None:
        assert delay == min(retry_after, policy.retry_after_cap_s)
    else:
        bound = min(policy.cap_s, policy.base_s * 2.0 ** min(attempt - 1, 1000))
        assert 0.0 <= delay <= bound
        assert delay == pytest.approx(u * bound, rel=1e-12, abs=0.0)


@given(attempt=attempts, u=unit, retry_after=unusable_retry_after)
def test_unusable_retry_after_falls_back_to_the_backoff(
    attempt: int, u: float, retry_after: float
) -> None:
    expected = backoff_delay(attempt, RetryPolicy(), None, u)
    assert backoff_delay(attempt, RetryPolicy(), retry_after, u) == expected
    assert expected == u * oracle_bound(attempt)


@given(
    attempt=attempts,
    u=unit,
    seconds=st.integers(min_value=0, max_value=10**15),
    fraction=st.none() | st.integers(min_value=0, max_value=999),
)
def test_numeric_header_is_capped_at_thirty_seconds(
    attempt: int, u: float, seconds: int, fraction: int | None
) -> None:
    header = str(seconds) if fraction is None else f"{seconds}.{fraction:03d}"
    parsed = parse_retry_after(header)
    assert parsed is not None
    assert parsed == pytest.approx(float(header))
    delay = backoff_delay(attempt, RetryPolicy(), parsed, u)
    assert delay == min(parsed, DEFAULT_RETRY_AFTER_CAP_S)
    assert 0.0 <= delay <= DEFAULT_RETRY_AFTER_CAP_S


@given(
    attempt=attempts,
    u=unit,
    header=st.one_of(
        st.none(),
        st.just(""),
        st.just("Wed, 21 Oct 2026 07:28:00 GMT"),
        st.integers(min_value=1, max_value=10**6).map(lambda n: f"-{n}"),
        st.text(alphabet="abcxyz ,:;", max_size=12),
    ),
)
def test_unparsable_header_falls_back_to_the_backoff(
    attempt: int, u: float, header: str | None
) -> None:
    delay = backoff_delay(attempt, RetryPolicy(), parse_retry_after(header), u)
    assert delay == u * oracle_bound(attempt)
