"""Property tests for the combined process's jobs runner (free-tier design Property 9).

Feature: cairn-free-tier-retarget, Property 9: The jobs runner retries with bounded backoff until
launch succeeds or stop is requested

**Validates: Requirements 8.3**

For any number of failing launch attempts k (0 to 10), any cap between 1 and 600 seconds and any
stop point, the runner calls launch k + 1 times when not stopped, calls destroy once after each
failure, waits exactly ``min(cap, 2 ** (i - 1))`` seconds after failure i, reports ``launched``
true only after a successful launch, reports ``attempts`` equal to the number of launch calls, and
makes no further launch call after stop is requested.

The runner is driven by calling ``run()`` on the test thread with a fake launch, a fake destroy
and a fake wait that advances a fake clock and never sleeps. No DBOS and no database are used.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from cairn_combined.app import JobsRunner, backoff_schedule
from cairn_core.config import CombinedSettings, env_keys

PBT = settings(deadline=None)

CAPS = st.floats(min_value=1.0, max_value=600.0, allow_nan=False, allow_infinity=False)
FAILURES = st.integers(min_value=0, max_value=10)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for key in env_keys(CombinedSettings):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.chdir(tmp_path)  # no .env here


def make_settings() -> CombinedSettings:
    return CombinedSettings.model_validate(
        {"CAIRN_APP_DB_PASSWORD": "pw-app", "CAIRN_DBOS_DB_PASSWORD": "pw-dbos"}
    )


def expected_delay(attempt: int, cap: float) -> float:
    return min(cap, float(2 ** (attempt - 1)))


class Harness:
    """Fake launch, destroy, clock and wait around one runner.

    Launch number ``i`` (1-based) fails while ``i <= failures`` (always, when ``failures`` is
    None) and succeeds afterwards. The wait with number ``stop_at`` asks the runner to stop while
    it waits, the way ``stop()`` interrupts ``Event.wait`` in production.
    """

    def __init__(self, cap: float, failures: int | None, stop_at: int | None) -> None:
        self.failures = failures
        self.stop_at = stop_at
        self.calls: list[str] = []
        self.waits: list[float] = []
        self.clock = 0.0
        self.launched_seen_during_launch: list[bool] = []
        self.runner = JobsRunner(
            make_settings(),
            launch=self.launch,
            destroy=self.destroy,
            wait=self.wait,
            max_backoff_s=cap,
        )

    def launch(self, _settings: CombinedSettings) -> None:
        self.calls.append("launch")
        self.launched_seen_during_launch.append(self.runner.status().launched)
        number = self.calls.count("launch")
        if self.failures is None or number <= self.failures:
            raise RuntimeError("boom")

    def destroy(self) -> None:
        self.calls.append("destroy")

    def wait(self, seconds: float) -> bool:
        self.waits.append(seconds)
        if len(self.waits) == self.stop_at:
            self.runner.stop()  # called on the runner's own thread: sets the event, no join
            return True
        self.clock += seconds
        return False

    @property
    def launches(self) -> int:
        return self.calls.count("launch")

    @property
    def destroys(self) -> int:
        return self.calls.count("destroy")


# --- backoff_schedule ---------------------------------------------------------------------


@PBT
@given(attempt=st.integers(min_value=1, max_value=200), cap=CAPS)
def test_backoff_is_the_capped_power_of_two(attempt: int, cap: float) -> None:
    delay = backoff_schedule(attempt, cap)
    assert delay == expected_delay(attempt, cap)
    assert 1.0 <= delay <= cap  # cap >= 1, so even the first delay (1 s) fits


@PBT
@given(cap=CAPS, attempts=st.integers(min_value=2, max_value=200))
def test_backoff_never_decreases_and_never_exceeds_the_cap(cap: float, attempts: int) -> None:
    delays = [backoff_schedule(n, cap) for n in range(1, attempts + 1)]
    assert delays == sorted(delays)
    assert max(delays) <= cap
    assert delays[0] == 1.0


# --- JobsRunner ---------------------------------------------------------------------------


@PBT
@given(failures=FAILURES, cap=CAPS)
def test_runner_retries_until_the_first_success(failures: int, cap: float) -> None:
    h = Harness(cap, failures, stop_at=None)
    h.runner.run()

    # k failures, each followed by one destroy, then exactly one successful launch
    assert h.calls == ["launch", "destroy"] * failures + ["launch"]
    assert h.waits == [expected_delay(i, cap) for i in range(1, failures + 1)]
    assert h.clock == pytest.approx(sum(h.waits))
    status = h.runner.status()
    assert status.launched is True
    assert status.attempts == failures + 1
    # launched was false at every launch call, so it is set only after the success
    assert h.launched_seen_during_launch == [False] * (failures + 1)


@PBT
@given(failures=st.one_of(FAILURES, st.none()), cap=CAPS, stop_at=st.integers(1, 12))
def test_runner_stops_during_a_wait_without_another_launch(
    failures: int | None, cap: float, stop_at: int
) -> None:
    h = Harness(cap, failures, stop_at=stop_at)
    h.runner.run()

    reached_wait = failures is None or stop_at <= failures
    status = h.runner.status()
    if reached_wait:
        # the stop arrived in wait number stop_at; that was the last thing the runner did
        assert h.launches == stop_at
        assert h.destroys == stop_at
        assert h.calls == ["launch", "destroy"] * stop_at
        assert h.waits == [expected_delay(i, cap) for i in range(1, stop_at + 1)]
        assert status.launched is False
        assert status.attempts == stop_at
    else:
        # launch succeeded before the stop point was reached
        assert h.launches == (failures or 0) + 1
        assert h.destroys == failures
        assert len(h.waits) == failures
        assert status.launched is True
        assert status.attempts == h.launches


@PBT
@given(failures=FAILURES, cap=CAPS)
def test_stop_after_success_destroys_once_and_a_stopped_runner_never_launches(
    failures: int, cap: float
) -> None:
    h = Harness(cap, failures, stop_at=None)
    h.runner.run()
    destroys_before = h.destroys

    h.runner.stop()
    h.runner.stop()  # idempotent
    assert h.destroys == destroys_before + 1
    assert h.runner.status().launched is False

    launches = h.launches
    h.runner.run()  # stop was requested: the loop must not launch again
    assert h.launches == launches
    assert h.runner.status().attempts == launches
