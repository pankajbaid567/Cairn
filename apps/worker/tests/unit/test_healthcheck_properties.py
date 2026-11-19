"""Property test for the worker healthcheck (task 6.2, requirement 8.6).

Feature: cairn-phase-0-foundations, Property 8: Worker health follows heartbeat freshness

**Validates: Requirements 8.6**

For any heartbeat file state (missing, or present with any age) the healthcheck succeeds if and
only if the file exists and is younger than 30 seconds. Two levels are checked: the pure
decision ``is_fresh`` over a wide range of ages and a stamp that may be absent, and ``main``
against real files whose modification time is set with ``os.utime``.

Hypothesis does not reset function-scoped fixtures between examples, so the file-based
property uses no fixtures: each example makes its own temporary directory. Ages are whole
milliseconds, so the boundary (exactly 30.000 s) is hit exactly and float rounding of the
mtime (about 2e-7 s at this epoch) cannot cross it. ``max_examples`` comes from the profiles
in the root ``conftest.py``.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from hypothesis import given, settings
from hypothesis import strategies as st

from cairn_worker.healthcheck import MAX_AGE_S, is_fresh, main

# Feature: cairn-phase-0-foundations, Property 8: Worker health follows heartbeat freshness

NOW = 1_800_000_000.0
LIMIT_MS = int(MAX_AGE_S * 1000)

# Whole milliseconds from 60 s in the future (clock skew) to a day in the past, with the
# boundary and its neighbours always in the mix.
age_ms = st.one_of(
    st.integers(-60_000, 86_400_000),
    st.integers(LIMIT_MS - 3, LIMIT_MS + 3),
    st.sampled_from([0, 1, LIMIT_MS - 1, LIMIT_MS, LIMIT_MS + 1]),
)
# A file state: None for a missing file, else its age in milliseconds.
file_state = st.one_of(st.none(), age_ms)


def _expected(state: int | None) -> bool:
    """The property's right-hand side, in integer arithmetic."""
    return state is not None and state < LIMIT_MS


@given(now=st.integers(10**9, 4 * 10**9).map(float), state=file_state)
def test_is_fresh_iff_the_stamp_exists_and_is_younger_than_30_seconds(
    now: float, state: int | None
) -> None:
    """Feature: cairn-phase-0-foundations, Property 8: Worker health follows heartbeat
    freshness (pure decision)."""
    stamp = None if state is None else now - state / 1000.0
    assert is_fresh(now, stamp) is _expected(state)


@settings(deadline=None)
@given(state=file_state)
def test_main_exits_zero_iff_the_file_exists_and_is_younger_than_30_seconds(
    state: int | None,
) -> None:
    """Feature: cairn-phase-0-foundations, Property 8: Worker health follows heartbeat
    freshness (real file, exit status)."""
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "cairn-worker" / "alive"
        if state is not None:
            path.parent.mkdir(parents=True)
            path.touch()
            stamp = NOW - state / 1000.0
            os.utime(path, (stamp, stamp))
        status = main(path=path, clock=lambda: NOW)
    assert status == (0 if _expected(state) else 1)
