"""Property test for Workers AI retries and reservations (free-tier retarget design, Property 14).

**Validates: Requirements 14.4, 14.6, 15.1**

For any scripted sequence of three attempt outcomes drawn from: a 2xx with a valid envelope; a 429
with the daily-limit code, with a capacity code and with no code; 400, 401, 403, 404; 500, 502,
503, 504; a connect timeout; a read timeout; and a transport error, and a budget that is large
enough, ``WorkersAiClient.run`` must:

- make attempts until the first success, the first non-retryable outcome, or 3 attempts, whichever
  comes first, and no more;
- return the result on success, and otherwise raise the error code (and status) of the last
  outcome it saw, with ``attempts`` equal to the attempts made;
- reserve the estimate exactly once before each attempt (the event log reads
  ``reserve, request, reserve, request, ...``) and nothing else: the day's total is
  ``attempts * estimate``;
- never make another attempt after the daily-limit code;
- wait between attempts only (``attempts - 1`` sleeps).

The expected result comes from a small model written from the requirements (the table below), not
from ``classify``. The transport is ``httpx.MockTransport``; the sleep and the random source are
injected, so nothing waits and nothing touches the network. The async code runs through
``asyncio.run`` inside a synchronous test because Hypothesis does not reset function-scoped
fixtures between examples; each example builds a fresh client, store and budget.
"""
# Feature: cairn-free-tier-retarget, Property 14: Retries follow the outcome sequence, and each
# attempt is reserved first

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import date
from typing import Any

import httpx
from hypothesis import example, given
from hypothesis import strategies as st

from cairn_core.config import WorkersAiSettings
from cairn_retrieval.cloudflare.budget import InMemoryUsageStore, NeuronBudget
from cairn_retrieval.cloudflare.client import WorkersAiClient, WorkersAiError

DAY = date(2026, 11, 3)
ACCOUNT = "0123456789abcdef0123456789abcdef"
TOKEN = "cf-test-token-SECRET-0001"  # fake by construction
MODEL = "@cf/baai/bge-small-en-v1.5"
PAYLOAD = {"text": ["alpha", "beta"]}
MAX_ATTEMPTS = 3
RESULT = {"shape": [2, 4], "data": [[0.1, 0.2, 0.3, 0.4], [0.5, 0.6, 0.7, 0.8]]}
CAP = 10_000_000  # "a budget large enough": never reached by 3 attempts of at most 5,000 neurons


@dataclass(frozen=True)
class Spec:
    """One scripted outcome and what the requirements say about it."""

    status: int | None  # HTTP status; ``None`` when the attempt fails before any response
    cloudflare_code: int | None  # the code in the error envelope, if any
    failure: Exception | None  # the transport exception raised instead of a response
    error: str | None  # the error code of the final failure; ``None`` for a success
    retryable: bool


# Requirement 14.4: retry on 429, 500, 502, 503, 504 and on connect or read timeouts. 14.6: never
# after the daily-limit code. Anything else (400, 401, 403, 404, transport errors) fails at once.
OUTCOMES: dict[str, Spec] = {
    "ok": Spec(200, None, None, None, False),
    "429": Spec(429, None, None, "rate_limited", True),
    "429_capacity": Spec(429, 3040, None, "rate_limited", True),
    "429_daily": Spec(429, 3036, None, "daily_limit", False),
    "400": Spec(400, 5004, None, "bad_request", False),
    "401": Spec(401, 10000, None, "auth", False),
    "403": Spec(403, 5035, None, "auth", False),
    "404": Spec(404, 3042, None, "bad_request", False),
    "500": Spec(500, None, None, "server", True),
    "502": Spec(502, None, None, "server", True),
    "503": Spec(503, None, None, "server", True),
    "504": Spec(504, None, None, "server", True),
    "connect_timeout": Spec(None, None, httpx.ConnectTimeout("connect"), "timeout", True),
    "read_timeout": Spec(None, None, httpx.ReadTimeout("read"), "timeout", True),
    "transport": Spec(None, None, httpx.ConnectError("refused"), "transport", False),
}


def _response(spec: Spec) -> httpx.Response:
    if spec.status == 200:
        body: dict[str, Any] = {"success": True, "result": RESULT, "errors": [], "messages": []}
    elif spec.cloudflare_code is None:
        body = {"success": False, "result": None, "errors": [], "messages": []}
    else:
        errors = [{"code": spec.cloudflare_code, "message": "scripted"}]
        body = {"success": False, "result": None, "errors": errors, "messages": []}
    assert spec.status is not None
    return httpx.Response(
        spec.status, content=json.dumps(body).encode(), headers={"content-type": "application/json"}
    )


@dataclass(frozen=True)
class Expected:
    attempts: int
    error: str | None
    status: int | None


def _model(names: list[str]) -> Expected:
    """What the requirements predict for a script of outcomes."""
    for attempt, name in enumerate(names, start=1):
        spec = OUTCOMES[name]
        if spec.error is None:
            return Expected(attempt, None, 200)
        if not spec.retryable or attempt == MAX_ATTEMPTS:
            return Expected(attempt, spec.error, spec.status)
    raise AssertionError("a script must have at least MAX_ATTEMPTS outcomes")


class RecordingStore(InMemoryUsageStore):
    """The in-memory store, noting each reservation in the shared event log."""

    def __init__(self, events: list[str]) -> None:
        super().__init__()
        self._events = events

    async def try_reserve(self, day: date, provider: str, neurons: int, cap: int) -> int | None:
        self._events.append("reserve")
        return await super().try_reserve(day, provider, neurons, cap)


def _settings() -> WorkersAiSettings:
    # Every key is given, so the process environment cannot change the bounds under test.
    values: dict[str, Any] = {
        "CLOUDFLARE_ACCOUNT_ID": ACCOUNT,
        "CLOUDFLARE_API_TOKEN": TOKEN,
        "CAIRN_WORKERS_AI_BASE_URL": "https://api.cloudflare.com/client/v4",
        "CAIRN_WORKERS_AI_MAX_ATTEMPTS": MAX_ATTEMPTS,
        "CAIRN_WORKERS_AI_CONNECT_TIMEOUT_S": 5,
        "CAIRN_WORKERS_AI_READ_TIMEOUT_S": 30,
    }
    return WorkersAiSettings(_env_file=None, **values)


async def _run(names: list[str], estimate: int) -> None:
    events: list[str] = []
    sleeps: list[float] = []
    script = [OUTCOMES[name] for name in names]

    def handler(request: httpx.Request) -> httpx.Response:
        events.append("request")
        assert len(events) // 2 <= len(script), "the client made more attempts than the script has"
        spec = script[(len(events) - 1) // 2]
        if spec.failure is not None:
            raise spec.failure
        return _response(spec)

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)

    budget = NeuronBudget(RecordingStore(events), CAP, today=lambda: DAY)
    expected = _model(names)

    async with WorkersAiClient(
        _settings(),
        budget,
        transport=httpx.MockTransport(handler),
        sleep=sleep,
        uniform=lambda: 0.5,
    ) as client:
        error: WorkersAiError | None = None
        result = None
        try:
            result = await client.run(MODEL, PAYLOAD, estimate_neurons=estimate)
        except WorkersAiError as exc:
            error = exc

    # Attempts follow the outcome sequence; each one is reserved first, exactly once.
    assert events == ["reserve", "request"] * expected.attempts
    assert len(sleeps) == expected.attempts - 1
    assert await budget.used() == expected.attempts * estimate

    if expected.error is None:
        assert error is None
        assert result is not None
        assert result.attempts == expected.attempts
        assert dict(result.result) == RESULT
    else:
        assert result is None
        assert error is not None
        assert error.code == expected.error
        assert error.status == expected.status
        assert error.attempts == expected.attempts
        if names[expected.attempts - 1] == "429_daily":
            assert error.code == "daily_limit"  # and the event log above shows no further attempt


_outcomes = st.lists(
    st.sampled_from(sorted(OUTCOMES)), min_size=MAX_ATTEMPTS, max_size=MAX_ATTEMPTS
)
_estimates = st.integers(min_value=1, max_value=5_000)


@given(names=_outcomes, estimate=_estimates)
@example(names=["ok", "500", "500"], estimate=5)  # first-try success, nothing else is touched
@example(names=["503", "ok", "500"], estimate=5)  # a retry that succeeds
@example(names=["429_daily", "ok", "ok"], estimate=5)  # never retried after the daily limit
@example(names=["429_capacity", "429_daily", "ok"], estimate=5)  # a retry then the daily limit
@example(names=["500", "502", "504"], estimate=5)  # three attempts, then the last outcome's code
@example(names=["connect_timeout", "read_timeout", "read_timeout"], estimate=5)
@example(names=["read_timeout", "transport", "ok"], estimate=5)  # transport is not retried
@example(names=["401", "ok", "ok"], estimate=5)
def test_retries_follow_the_outcome_sequence_and_each_attempt_is_reserved_first(
    names: list[str], estimate: int
) -> None:
    asyncio.run(_run(names, estimate))
